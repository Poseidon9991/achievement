"""github_achievements — earn GitHub profile achievements on your own repos.

Single-file, stdlib-only CLI. Task 1 provides the core plumbing:
``GitHubClient`` (REST + GraphQL over ``urllib`` with politeness delays and
Retry-After backoff), token loading (``--token`` > ``GITHUB_TOKEN`` > ``.env``),
an activity log that never writes the token, and shared constants/helpers
(``TIER_TARGETS``, ``format_coauthor_trailer``) used by the badge tasks.
Task 2 adds ``cmd_doctor`` (token health check) and ``cmd_status`` (scrapes
the public profile page for earned achievements via ``parse_profile_badges``).
Task 3 adds ``cmd_publish`` (create-or-reuse the playground repo, add the
``origin`` remote, and push ``main``). Task 4 adds the PR engine —
``count_merged_prs`` plus the ``pr_cycle`` create-branch/commit/PR/merge/
cleanup pipeline — and the three badge drivers built on it:
``badge_quickdraw``, ``badge_yolo``, and ``badge_pull_shark``.
Task 5 adds ``badge_galaxy_brain`` — Discussions Q&A over GraphQL
(createDiscussion -> addDiscussionComment -> markDiscussionCommentAsAnswer)
with a clean fallback when GitHub disallows self-marking answers.
Task 6 adds ``badge_pair_extraordinaire`` — merged PRs whose commits
carry a ``Co-authored-by`` trailer, validated by ``parse_coauthor``.
Task 7 wires everything together: ``main()`` with the subcommands
``doctor|publish|status|unlock|manual``, the ``unlock`` orchestration
(publish -> quickdraw -> yolo -> pull-shark -> galaxy-brain ->
pair-extraordinaire, each failure caught and reported in a summary
table), the ownership guard that restricts writes to repos you own,
``--dry-run`` planning without a live client, and ``manual`` steps for
the badges that cannot be automated.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

LOG_PATH = "achievement_run.log"
ENV_PATH = ".env"
API_BASE = "https://api.github.com"
PAT_URL = "https://github.com/settings/tokens"
MAX_RETRIES = 3
BACKOFF_CAP_SECONDS = 120
REQUEST_TIMEOUT_SECONDS = 30
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Bronze-tier targets for the five solo-earnable achievements.
TIER_TARGETS = {
    "quickdraw": 1,
    "yolo": 1,
    "pull_shark": 16,
    "galaxy_brain": 8,
    "pair_extraordinaire": 10,
}

# Every achievement display name the profile parser recognizes — including
# the retired one-off event badges (Arctic/Mars), which still render on the
# profiles of users who earned them.
KNOWN_BADGES = (
    "Quickdraw",
    "Pull Shark",
    "Galaxy Brain",
    "YOLO",
    "Pair Extraordinaire",
    "Starstruck",
    "Public Sponsor",
    "Arctic Code Vault Contributor",
    "Mars 2020 Contributor",
    "Heart On Your Sleeve",
    "Open Sourcerer",
)

# The 9 earnable badges shown by `status`, mapped to their Bronze-tier
# target ("manual" = cannot be driven by this tool; "n/a" = never released
# or rolled back, shown for completeness of the profile display).
STATUS_BADGE_TARGETS = {
    "Quickdraw": TIER_TARGETS["quickdraw"],
    "Pull Shark": TIER_TARGETS["pull_shark"],
    "Galaxy Brain": TIER_TARGETS["galaxy_brain"],
    "YOLO": TIER_TARGETS["yolo"],
    "Pair Extraordinaire": TIER_TARGETS["pair_extraordinaire"],
    "Starstruck": "manual",
    "Public Sponsor": "manual",
    "Heart On Your Sleeve": "n/a",
    "Open Sourcerer": "n/a",
}

# Achievement slug -> display name. Slugs appear in profile-page hrefs as
# `{profile}?achievement=<slug>&tab=achievements` — a URL pattern unique to
# GitHub's own achievements section.
SLUG_TO_NAME = {
    "quickdraw": "Quickdraw",
    "pull-shark": "Pull Shark",
    "galaxy-brain": "Galaxy Brain",
    "yolo": "YOLO",
    "pair-extraordinaire": "Pair Extraordinaire",
    "starstruck": "Starstruck",
    "public-sponsor": "Public Sponsor",
    "arctic-code-vault-contributor": "Arctic Code Vault Contributor",
    "mars-2020-helicopter-contributor": "Mars 2020 Contributor",
    "heart-on-your-sleeve": "Heart On Your Sleeve",
    "open-sourcerer": "Open Sourcerer",
}

_ACHIEVEMENT_SLUG_RE = re.compile(
    r"""href\s*=\s*["'][^"']*[?&]achievement=([a-zA-Z0-9-]+)""",
    re.IGNORECASE)
_BADGE_ATTR_RE = re.compile(
    r"""(?:alt|aria-label)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_ACHIEVEMENT_PREFIX_RE = re.compile(r"^\s*achievement\s*:\s*", re.IGNORECASE)
_TIER_SUFFIX_RE = re.compile(r"\s+x\d+$", re.IGNORECASE)
_ACHIEVEMENTS_SECTION_MARK = "Achievements"
# Sections that render below Achievements on a profile page; the fallback
# slice ends at the earliest one so badge-looking alt text in later content
# (README, footer, orgs) cannot false-positive.
_POST_ACHIEVEMENTS_LANDMARKS = (
    "Contribution activity",
    "Organizations",
    "Popular repositories",
    "Public contributions",
)
# Upper bound when no landmark is found — generous for ~a dozen badges'
# worth of section markup.
_ACHIEVEMENTS_WINDOW = 30_000
_KNOWN_BADGES_FOLDED = {name.casefold(): name for name in KNOWN_BADGES}


class GHError(Exception):
    """GitHub API failure. ``retry_after`` is seconds from the Retry-After
    header, or ``None`` when the server did not supply one."""

    def __init__(self, status: int, message: str,
                 retry_after: int | None = None) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.retry_after = retry_after


def _dotenv_token() -> str | None:
    """Read ``GITHUB_TOKEN`` from the ``.env`` file, if present.

    The file is read line-by-line; only a non-empty ``GITHUB_TOKEN=...``
    entry counts. Returns ``None`` when the file is missing, unreadable,
    or has no usable entry — this helper never prints or exits.
    """
    try:
        with open(ENV_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() != "GITHUB_TOKEN":
                    continue
                value = value.strip().strip('"').strip("'").strip()
                if value:
                    return value
    except OSError:
        pass
    return None


def load_token(cli_token: str | None) -> str:
    """Resolve the GitHub token: CLI flag > ``GITHUB_TOKEN`` env var > ``.env``.

    ``.env`` is read line-by-line; only a ``GITHUB_TOKEN=...`` entry counts.
    Prints PAT instructions and raises ``SystemExit(1)`` if nothing is found.
    """
    if cli_token:
        return cli_token
    env_token = os.environ.get("GITHUB_TOKEN")
    if env_token:
        return env_token
    dotenv_token = _dotenv_token()
    if dotenv_token:
        return dotenv_token
    print(
        "No GitHub token found. Provide one via --token, the GITHUB_TOKEN "
        "environment variable, or a .env file containing GITHUB_TOKEN=<token>.\n"
        f"Create a personal access token at {PAT_URL} "
        "(scopes: repo, read:discussion, write:discussion).",
        file=sys.stderr,
    )
    raise SystemExit(1)


def format_coauthor_trailer(name: str, email: str) -> str:
    """Return the git ``Co-authored-by`` trailer for a commit message."""
    return f"Co-authored-by: {name} <{email}>"


_COAUTHOR_RE = re.compile(r"^\s*(.+?)\s*<([^<>]*)>\s*$")


def parse_coauthor(raw: str) -> tuple[str, str]:
    """Validate ``"Name <email>"`` and return ``(name, email)``.

    Raises ``ValueError`` — with the ``--coauthor "Name <email>"`` usage
    example in the message — when the angle brackets or the email are
    missing, or when either part is empty after trimming.
    """
    match = _COAUTHOR_RE.match(raw or "")
    if match:
        name = match.group(1).strip()
        email = match.group(2).strip()
        if name and email:
            return name, email
    raise ValueError(
        f"invalid co-author {raw!r}: use "
        '--coauthor "Name <email>" '
        '(e.g. --coauthor "Ada Lovelace <ada@x.io>")')


def _retry_after_seconds(raw: str | None) -> int | None:
    """Parse a Retry-After header value; non-numeric, missing, or
    negative -> ``None`` (a negative value must never become a negative
    ``time.sleep``; the caller falls back to its default backoff)."""
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _error_message(raw_body: bytes) -> str:
    """Best-effort extraction of GitHub's ``message`` field from an error body."""
    try:
        payload = json.loads(raw_body.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return raw_body.decode("utf-8", errors="replace").strip() or "HTTP error"
    if isinstance(payload, dict) and payload.get("message"):
        return str(payload["message"])
    return json.dumps(payload)[:500] if payload else "HTTP error"


class GitHubClient:
    """Thin REST + GraphQL wrapper over ``urllib.request``.

    - Sends the token only in the ``Authorization`` header; it is scrubbed from
      every log line (``log`` replaces any occurrence with ``***``).
    - Sleeps ``delay`` seconds before each mutating call (politeness).
    - On HTTP 403/429 it honors ``Retry-After`` with exponential backoff
      (``min(2**attempt * int(retry_after or 1), 120)``), retrying at most
      ``MAX_RETRIES`` times before raising ``GHError``.
    - ``dry_run=True`` prints and logs the intended call, returns ``{}`` —
      zero writes.
    """

    GHError = GHError

    def __init__(self, token: str, dry_run: bool = False,
                 delay: float = 2.5) -> None:
        self.token = token
        self.dry_run = dry_run
        self.delay = delay

    def log(self, msg: str) -> None:
        """Append a timestamped line to ``LOG_PATH``, scrubbing the token."""
        if self.token:
            msg = msg.replace(self.token, "***")
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {msg}\n")

    def rest(self, method: str, path: str, body: dict | None = None) -> dict:
        """Call ``{method} https://api.github.com{path}`` with a JSON body.

        Returns the parsed JSON, or ``{}`` for 204/empty responses. Raises
        ``GHError`` on HTTP errors once retries are exhausted.
        """
        method = method.upper()
        if self.dry_run:
            line = f"DRY-RUN {method} {path} body={json.dumps(body)}"
            print(line)
            self.log(line)
            return {}
        if method in MUTATING_METHODS:
            time.sleep(self.delay)
        url = f"{API_BASE}{path}"
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        for attempt in range(MAX_RETRIES + 1):
            request = urllib.request.Request(url, data=payload, method=method)
            request.add_header("Authorization", f"Bearer {self.token}")
            request.add_header("Accept", "application/vnd.github+json")
            request.add_header("X-GitHub-Api-Version", "2022-11-28")
            if payload is not None:
                request.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(
                        request, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
                    status = resp.status
                    raw = resp.read()
            except urllib.error.HTTPError as exc:
                status = exc.code
                raw = exc.read() or b""
                retry_after = _retry_after_seconds(
                    exc.headers.get("Retry-After") if exc.headers else None)
                message = _error_message(raw) or str(exc.reason or "HTTP error")
                error = GHError(status, message, retry_after)
                if status in (403, 429) and attempt < MAX_RETRIES:
                    wait = min(2 ** attempt * int(retry_after or 1),
                               BACKOFF_CAP_SECONDS)
                    if method not in ("GET", "HEAD"):
                        # keep the politeness floor between mutating calls
                        wait = max(wait, self.delay)
                    self.log(f"{method} {path} -> {status}; "
                             f"retry {attempt + 1}/{MAX_RETRIES} in {wait}s")
                    time.sleep(wait)
                    continue
                self.log(f"{method} {path} -> {status}: {message}")
                raise error
            except urllib.error.URLError as exc:
                self.log(f"{method} {path} -> network error: {exc.reason}")
                raise GHError(0, f"network error: {exc.reason}") from exc
            self.log(f"{method} {path} -> {status}")
            if status == 204 or not raw:
                return {}
            try:
                return json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                # A 2xx body that is not JSON must surface as GHError, not
                # an escaping ValueError that kills the caller's flow.
                message = f"non-JSON response from {method} {path}"
                self.log(f"{method} {path} -> {status}: {message}")
                raise GHError(status, message) from None
        raise GHError(0, f"{method} {path}: exhausted retries")

    def graphql(self, query: str, variables: dict | None = None) -> dict:
        """POST ``/graphql``. Returns the ``data`` object; raises ``GHError``
        when the response contains an ``errors`` key."""
        payload: dict = {"query": query}
        if variables:
            payload["variables"] = variables
        result = self.rest("POST", "/graphql", payload)
        errors = result.get("errors")
        if errors:
            message = "; ".join(
                str(e.get("message", e)) if isinstance(e, dict) else str(e)
                for e in errors)
            raise GHError(200, f"GraphQL errors: {message}")
        data = result.get("data")
        return data if isinstance(data, dict) else result


def _canonical_badge_name(raw: str) -> str | None:
    """Normalize an extracted badge label to its canonical display name.

    Strips an ``"Achievement: "`` prefix and a tier suffix like ``" x3"``,
    then matches case-insensitively against the known-badge allowlist.
    Returns ``None`` for anything unrecognized.
    """
    name = _ACHIEVEMENT_PREFIX_RE.sub("", raw).strip()
    name = _TIER_SUFFIX_RE.sub("", name).strip()
    return _KNOWN_BADGES_FOLDED.get(name.casefold())


def parse_profile_badges(html: str) -> list[str]:
    """Extract achievement names from a GitHub profile page's achievements
    section.

    Primary extraction matches ``achievement=<slug>`` inside href values —
    the ``?achievement=...&tab=achievements`` URL pattern is unique to
    GitHub's own achievements section, so it is safe page-wide. Only when
    no slug matches does the fallback run: the HTML is sliced from the
    ``Achievements`` heading to the earliest post-section landmark (or a
    fixed window), and badge ``alt``/``aria-label`` values are matched
    inside that slice alone (README content elsewhere on the page could
    otherwise spoof badge alt text). No achievements section means ``[]``
    — the whole document is never scanned. Results are deduped in
    first-seen (document) order.
    """
    earned: list[str] = []

    def _add(name: str | None) -> None:
        if name and name not in earned:
            earned.append(name)

    for match in _ACHIEVEMENT_SLUG_RE.finditer(html):
        _add(SLUG_TO_NAME.get(match.group(1).lower()))

    if not earned:
        section = html.find(_ACHIEVEMENTS_SECTION_MARK)
        if section == -1:
            return []
        # Bound the slice on both ends: start at the Achievements heading,
        # end at the earliest post-section landmark (or the fixed window).
        end = section + _ACHIEVEMENTS_WINDOW
        after_mark = section + len(_ACHIEVEMENTS_SECTION_MARK)
        for landmark in _POST_ACHIEVEMENTS_LANDMARKS:
            idx = html.find(landmark, after_mark)
            if idx != -1:
                end = min(end, idx)
        for match in _BADGE_ATTR_RE.finditer(html[section:end]):
            _add(_canonical_badge_name(match.group(1)))
    return earned


def cmd_doctor(client: GitHubClient) -> None:
    """Check token health via ``GET /user``; print login and plan summary.

    On HTTP 401 prints the PAT fallback instructions (URL, scopes) to stderr
    and raises ``SystemExit(1)``; any other ``GHError`` is reported and also
    exits 1. The token is never printed.
    """
    try:
        user = client.rest("GET", "/user")
    except GHError as exc:
        if exc.status == 401:
            print(
                "Token rejected by GitHub (401 Unauthorized).\n"
                f"Create a new personal access token at {PAT_URL} "
                "(scopes: repo, read:discussion, write:discussion), then "
                "retry via --token, GITHUB_TOKEN, or .env.",
                file=sys.stderr,
            )
        else:
            print(f"GitHub check failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
    login = user.get("login", "<unknown>")
    plan = user.get("plan") or {}
    plan_name = plan.get("name", "unknown")
    print(f"Token OK. Authenticated as {login} (plan: {plan_name}).")
    client.log(f"doctor: authenticated as {login} (plan: {plan_name})")


def cmd_status(client: GitHubClient) -> None:
    """Scrape ``https://github.com/{login}`` for earned achievements and
    print a target/earned table for the 9 earnable badges.

    The profile fetch goes through bare ``urllib`` with no ``Authorization``
    header — the achievements section is public. ``GET /user`` (via the
    authed client) supplies the login; API/network failures exit 1.
    """
    try:
        user = client.rest("GET", "/user")
    except GHError as exc:
        print(f"GitHub check failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
    login = user.get("login")
    if not login:
        print("Could not resolve the authenticated user's login.",
              file=sys.stderr)
        raise SystemExit(1)
    url = f"https://github.com/{login}"
    request = urllib.request.Request(url)
    request.add_header("Accept", "text/html")
    request.add_header("User-Agent", "github-achievements-cli")
    try:
        with urllib.request.urlopen(
                request, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as exc:
        print(f"Could not fetch {url}: {exc}", file=sys.stderr)
        raise SystemExit(1)
    earned = set(parse_profile_badges(html))
    print(f"Achievements for {url} "
          "(may lag real progress by up to 24-48h)\n")
    name_w = max(len(name) for name in STATUS_BADGE_TARGETS)
    print(f"{'Badge':<{name_w}}  {'Target':<8}  Earned")
    for name, target in STATUS_BADGE_TARGETS.items():
        mark = "yes" if name in earned else "no"
        print(f"{name:<{name_w}}  {str(target):<8}  {mark}")
    client.log(f"status: {login} earned="
               f"{','.join(sorted(earned)) or 'none'}")


def _run_git(client: GitHubClient, args: list[str]) -> bool:
    """Run ``git *args``, echoing and logging captured output.

    Uses ``subprocess.run(check=False, capture_output=True, text=True)``:
    git failures are reported, never raised — the caller decides how to
    react via the returned boolean (True on exit code 0). In dry-run mode
    the command line is printed/logged as ``DRY-RUN git ...`` and nothing
    is executed.
    """
    cmdline = "git " + " ".join(args)
    if client.dry_run:
        line = f"DRY-RUN {cmdline}"
        print(line)
        client.log(line)
        return True
    try:
        result = subprocess.run(["git", *args], check=False,
                                capture_output=True, text=True)
    except OSError as exc:
        # git not on PATH, etc. — same contract as a non-zero exit
        print(f"{cmdline}: could not run git: {exc}", file=sys.stderr)
        client.log(f"{cmdline} -> failed to start: {exc}")
        return False
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if stdout:
        print(stdout)
        client.log(f"{cmdline} stdout: {stdout}")
    if stderr:
        print(stderr, file=sys.stderr)
        client.log(f"{cmdline} stderr: {stderr}")
    client.log(f"{cmdline} -> exit {result.returncode}")
    return result.returncode == 0


def cmd_publish(client: GitHubClient, name: str) -> dict:
    """Ensure ``{login}/{name}`` exists on GitHub and push ``main`` to it.

    The login is resolved from ``GET /user`` at runtime (never hardcoded).
    ``GET /repos/{login}/{name}`` decides whether the repo already exists;
    a 404 triggers ``POST /user/repos`` with
    ``{"name": name, "private": False, "auto_init": False}``. Any other
    ``GHError`` propagates to the caller. An existing repo that is a fork
    is rejected with ``GHError`` — the playground must be standalone.
    Otherwise both paths run ``git remote add origin
    https://github.com/{login}/{name}.git`` and ``git push -u origin main``
    via ``_run_git`` — so re-running publish on an existing repo simply
    pushes again (idempotent). A failed ``remote add`` (origin already
    configured) falls back to ``git remote set-url`` so the remote is
    repointed rather than fatal. In dry-run mode the repo's existence is
    unknown (dry-run GETs return ``{}``), so the plan prints the
    conditional create step instead of claiming either state.

    Returns ``{"name", "owner", "full_name", "html_url", "remote_url",
    "created", "pushed"}`` — ``pushed`` is False when the push exited
    non-zero (the failure is logged, not raised).
    """
    user = client.rest("GET", "/user")
    login = user.get("login")
    if not login:
        if client.dry_run:
            login = "<login>"  # dry-run GETs return {}; keep printing plan
        else:
            raise GHError(
                0, "could not resolve the authenticated user's login")
    remote_url = f"https://github.com/{login}/{name}.git"
    created = False
    try:
        repo = client.rest("GET", f"/repos/{login}/{name}")
    except GHError as exc:
        if exc.status != 404:
            raise
        print(f"Repo {login}/{name} not found; creating it.")
        repo = client.rest("POST", "/user/repos",
                           {"name": name,
                            "private": False,
                            "auto_init": False})
        created = True
        print(f"Created {login}/{name}.")
    else:
        if repo.get("fork"):
            raise GHError(
                0, f"{login}/{name} is a fork; the playground repo must "
                   "be standalone")
        if client.dry_run:
            # dry-run GETs return {}, so existence is unknown — show the
            # conditional create step rather than asserting either state
            body = {"name": name,
                    "private": False,
                    "auto_init": False}
            line = (f"DRY-RUN (if 404) POST /user/repos "
                    f"body={json.dumps(body)}")
            print(line)
            client.log(line)
        else:
            print(f"Repo {login}/{name} already exists; skipping creation.")
    if client.dry_run:
        client.log(f"publish: {login}/{name} existence unknown (dry-run)")
    else:
        client.log(f"publish: repo {login}/{name} "
                   f"{'created' if created else 'already existed'}")
    if not _run_git(client, ["remote", "add", "origin", remote_url]):
        _run_git(client, ["remote", "set-url", "origin", remote_url])
    pushed = _run_git(client, ["push", "-u", "origin", "main"])
    if not pushed:
        print(f"git push to {remote_url} failed; see {LOG_PATH} for details.",
              file=sys.stderr)
    html_url = (repo.get("html_url") if isinstance(repo, dict) else None) \
        or f"https://github.com/{login}/{name}"
    return {
        "name": name,
        "owner": login,
        "full_name": f"{login}/{name}",
        "html_url": html_url,
        "remote_url": remote_url,
        "created": created,
        "pushed": pushed,
    }


def count_merged_prs(client: GitHubClient, owner: str, repo: str,
                     login: str | None = None) -> int:
    """Count merged PRs authored by ``login`` in ``owner/repo``.

    Fetches ``GET /repos/{owner}/{repo}/pulls?state=closed&per_page=100``
    and counts the closed PRs whose ``merged_at`` is non-null and whose
    author (``pr["user"]["login"]``) matches ``login`` — GitHub's Pull
    Shark rule counts PRs the user authored and merged, including PRs on
    their own repos. When ``login`` is omitted it is resolved once via
    ``GET /user``; callers doing repeated work should fetch it once and
    pass it in. A non-list response (e.g. dry-run ``{}``) counts as zero.
    """
    if login is None:
        login = client.rest("GET", "/user").get("login")
    pulls = client.rest(
        "GET", f"/repos/{owner}/{repo}/pulls?state=closed&per_page=100")
    if not isinstance(pulls, list):
        return 0
    return sum(
        1 for pr in pulls
        if isinstance(pr, dict)
        and pr.get("merged_at") is not None
        and (pr.get("user") or {}).get("login") == login)


def pr_cycle(client: GitHubClient, owner: str, repo: str,
             label: str, message: str) -> int:
    """Create a branch, commit a note file, open a PR, merge it, and
    delete the branch — returning the PR number.

    Steps, all over REST:

    1. ``GET /repos/{o}/{r}`` for the default branch, then
       ``GET /repos/{o}/{r}/git/refs/heads/{branch}`` for its HEAD sha.
    2. ``POST /git/refs`` creates ``refs/heads/shark-pr-{label}-{ns}``
       (a nanosecond stamp keeps the branch and file unique across
       cycles and re-runs).
    3. ``PUT /contents/notes/{label}-{ns}.md`` commits a unique file on
       the new branch (Contents API takes base64 content).
    4. ``POST /pulls`` opens the PR (``title``/``head``/``base``/``body``).
    5. ``PUT /pulls/{n}/merge`` merges it with ``{"merge_method":
       "merge"}`` — no review is ever requested, which is what makes a
       plain merge count for YOLO.
    6. ``DELETE /git/refs/heads/{branch}`` cleans up; a delete failure
       is tolerated (logged warning, not raised) since the merged PR —
       the thing that counts — already exists.
    """
    base = f"/repos/{owner}/{repo}"
    info = client.rest("GET", base)
    default_branch = info.get("default_branch") or "main"
    head_ref = client.rest("GET", f"{base}/git/refs/heads/{default_branch}")
    sha = (head_ref.get("object") or {}).get("sha")
    if not sha:
        if client.dry_run:
            sha = "0" * 40  # dry-run GETs return {}; keep printing the plan
        else:
            # fail fast: never POST /git/refs with a fabricated sha
            raise GHError(
                0, f"no commit sha for heads/{default_branch} in the ref "
                   "response; refusing to create a branch")

    stamp = time.time_ns()
    branch = f"shark-pr-{label}-{stamp}"
    client.rest("POST", f"{base}/git/refs",
                {"ref": f"refs/heads/{branch}", "sha": sha})

    note = f"# {label}\n\n{message}\n\nGenerated by github_achievements.\n"
    content = base64.b64encode(note.encode("utf-8")).decode("ascii")
    client.rest("PUT", f"{base}/contents/notes/{label}-{stamp}.md",
                {"message": message, "content": content, "branch": branch})

    pr = client.rest("POST", f"{base}/pulls",
                     {"title": message,
                      "head": branch,
                      "base": default_branch,
                      "body": f"Automated PR for {label}; merged "
                              "immediately by github_achievements."})
    number = pr.get("number")
    if not number:
        if client.dry_run:
            number = 0  # dry-run POSTs return {}; keep printing the plan
        else:
            # fail fast: never PUT /pulls/0/merge on a malformed response
            raise GHError(
                0, "no PR number in the create response; refusing to merge")
    client.rest("PUT", f"{base}/pulls/{number}/merge",
                {"merge_method": "merge"})

    try:
        client.rest("DELETE", f"{base}/git/refs/heads/{branch}")
    except GHError as exc:
        client.log(f"pr_cycle: delete refs/heads/{branch} -> {exc}")
        print(f"warning: could not delete branch {branch}: {exc}",
              file=sys.stderr)
    client.log(f"pr_cycle: {owner}/{repo} PR #{number} merged "
               f"(label={label}, branch={branch})")
    return number


def badge_quickdraw(client: GitHubClient, owner: str, repo: str) -> bool:
    """Open an issue and close it immediately — well under Quickdraw's
    5-minute window. Returns True on success; API failures are logged,
    reported on stderr, and return False."""
    base = f"/repos/{owner}/{repo}"
    try:
        issue = client.rest(
            "POST", f"{base}/issues",
            {"title": "Quickdraw test issue",
             "body": "Opened and closed immediately to earn the "
                     "Quickdraw achievement."})
        number = issue.get("number")
        if not number:
            if client.dry_run:
                number = 0  # dry-run POSTs return {}; keep printing the plan
            else:
                # fail fast: never PATCH /issues/0 on a malformed response
                raise GHError(
                    0, "no issue number in the create response; refusing "
                       "to close a fabricated issue")
        client.rest("PATCH", f"{base}/issues/{number}",
                    {"state": "closed"})
    except GHError as exc:
        client.log(f"quickdraw: {owner}/{repo} failed: {exc}")
        print(f"Quickdraw attempt failed: {exc}", file=sys.stderr)
        return False
    client.log(f"quickdraw: {owner}/{repo} issue #{number} "
               "opened+closed")
    print(f"Quickdraw: opened and closed issue #{number}.")
    return True


def badge_yolo(client: GitHubClient, owner: str, repo: str) -> bool:
    """Merge one PR without requesting a review — a plain merge already
    earns YOLO per community reports. Returns True on success."""
    try:
        number = pr_cycle(client, owner, repo, "yolo",
                          "YOLO: merged without review")
    except GHError as exc:
        client.log(f"yolo: {owner}/{repo} failed: {exc}")
        print(f"YOLO attempt failed: {exc}", file=sys.stderr)
        return False
    print(f"YOLO: merged PR #{number} with no review.")
    return True


def badge_pull_shark(client: GitHubClient, owner: str, repo: str,
                     target: int) -> int:
    """Merge PRs until ``target`` merged authored PRs exist in
    ``owner/repo``; returns how many cycles ran this invocation.

    Idempotent: ``remaining = max(0, target - count_merged_prs(...))``,
    so re-running after earning the badge (or after a partial run)
    performs only the cycles still needed. The login is fetched once
    and shared with ``count_merged_prs``.
    """
    login = client.rest("GET", "/user").get("login")
    merged = count_merged_prs(client, owner, repo, login)
    remaining = max(0, target - merged)
    if remaining == 0:
        print(f"Pull Shark: already at {merged}/{target} merged PRs; "
              "nothing to do.")
        client.log(f"pull_shark: {owner}/{repo} already at "
                   f"{merged}/{target}")
        return 0
    print(f"Pull Shark: {merged}/{target} merged; running "
          f"{remaining} PR cycle(s).")
    done = 0
    for i in range(remaining):
        number = pr_cycle(
            client, owner, repo,
            f"pull-shark-{merged + i + 1}",
            f"Pull Shark PR {merged + i + 1}/{target}")
        done += 1
        print(f"  merged PR #{number} ({done}/{remaining})")
    client.log(f"pull_shark: {owner}/{repo} ran {done} cycle(s) "
               f"toward {target}")
    return done


def badge_galaxy_brain(client: GitHubClient, owner: str, repo: str,
                       target: int) -> int:
    """Post self-answered Discussions until ``target`` accepted answers
    exist in ``owner/repo``; returns how many answers were created this
    invocation, ``0`` when already at target, or ``-1`` when GitHub
    disallows self-marking answers.

    Steps:

    1. ``PATCH /repos/{o}/{r}`` ``{"has_discussions": true}`` — enabling
       Discussions is idempotent, and on first enable GitHub seeds
       default categories including an answerable Q&A one.
    2. One GraphQL ``repository`` query fetches the repo node ``id``,
       ``discussionCategories(first: 20)`` and ``discussions(first: 50)``
       — the latter counts prior progress: discussions whose
       ``answer.author.login`` equals the authenticated login already
       count toward the target (idempotent re-runs).
    3. The first ``isAnswerable`` category is used; when none exists a
       ``createDiscussionCategory`` mutation makes a ``"Q&A"`` category
       with ``format: QUESTIONS_ANSWERS`` and its id is used instead.
    4. Per remaining answer: ``createDiscussion`` (a question),
       ``addDiscussionComment`` (the answer), then — only when the
       returned comment's ``viewerCanMarkAsAnswer`` is true —
       ``markDiscussionCommentAsAnswer`` with the *comment* node id.

    Self-marking may be disallowed: the moment a comment reports
    ``viewerCanMarkAsAnswer: false`` the badge stops, prints the fallback
    note (a partner marks the answers, or 2 manual "Select as answer"
    clicks per discussion in the repo UI), and returns ``-1`` — no mark
    mutation and no further discussions are attempted. In dry-run mode
    every call prints as ``DRY-RUN`` and returns ``{}``; the badge then
    just prints how many discussion/comment/mark steps it would run and
    returns ``0`` without touching the missing response fields.
    """
    base = f"/repos/{owner}/{repo}"
    login = client.rest("GET", "/user").get("login")
    client.rest("PATCH", base, {"has_discussions": True})

    data = client.graphql(
        "query($owner:String!,$name:String!){"
        " repository(owner:$owner,name:$name){"
        "  id"
        "  discussionCategories(first:20){nodes{id name isAnswerable}}"
        "  discussions(first:50){nodes{answer{author{login}}}}"
        " }"
        "}",
        {"owner": owner, "name": repo})
    repository = data.get("repository") or {}
    repo_id = repository.get("id")

    categories = ((repository.get("discussionCategories") or {})
                  .get("nodes") or [])
    category_id = next(
        (c.get("id") for c in categories
         if isinstance(c, dict) and c.get("isAnswerable") and c.get("id")),
        None)
    if category_id is None and repo_id:
        created = client.graphql(
            "mutation($repoId:ID!){"
            " createDiscussionCategory(input:{repositoryId:$repoId,"
            "name:\"Q&A\",format:QUESTIONS_ANSWERS}){"
            "  discussionCategory{id}"
            " }"
            "}",
            {"repoId": repo_id})
        category_id = ((created.get("createDiscussionCategory") or {})
                       .get("discussionCategory") or {}).get("id")

    discussions = ((repository.get("discussions") or {})
                   .get("nodes") or [])
    answered = sum(
        1 for d in discussions
        if isinstance(d, dict)
        and ((d.get("answer") or {}).get("author") or {})
            .get("login") == login)
    remaining = max(0, target - answered)

    if client.dry_run:
        print(f"Galaxy Brain: would create {remaining} discussion(s) "
              f"with one answer comment each and mark each comment as "
              f"the answer ({answered}/{target} already accepted).")
        client.log(f"galaxy_brain: {owner}/{repo} dry-run plan: "
                   f"{remaining} discussion+comment+mark cycle(s)")
        return 0
    if remaining == 0:
        print(f"Galaxy Brain: already at {answered}/{target} accepted "
              "answers; nothing to do.")
        client.log(f"galaxy_brain: {owner}/{repo} already at "
                   f"{answered}/{target}")
        return 0
    print(f"Galaxy Brain: {answered}/{target} accepted answers; posting "
          f"{remaining} self-answered discussion(s).")

    done = 0
    for i in range(remaining):
        n = answered + i + 1
        created = client.graphql(
            "mutation($repoId:ID!,$categoryId:ID!,$title:String!,"
            "$body:String!){"
            " createDiscussion(input:{repositoryId:$repoId,"
            "categoryId:$categoryId,title:$title,body:$body}){"
            "  discussion{id number}"
            " }"
            "}",
            {"repoId": repo_id, "categoryId": category_id,
             "title": f"Galaxy Brain Q&A {n}/{target}",
             "body": "Question posted automatically by "
                     "github_achievements for the Galaxy Brain "
                     "achievement."})
        discussion = ((created.get("createDiscussion") or {})
                      .get("discussion") or {})

        commented = client.graphql(
            "mutation($discussionId:ID!,$body:String!){"
            " addDiscussionComment(input:{discussionId:$discussionId,"
            "body:$body}){"
            "  comment{id viewerCanMarkAsAnswer}"
            " }"
            "}",
            {"discussionId": discussion.get("id"),
             "body": "The answer — posted and marked automatically by "
                     "github_achievements."})
        comment = ((commented.get("addDiscussionComment") or {})
                   .get("comment") or {})
        if not comment.get("viewerCanMarkAsAnswer"):
            print(
                "Galaxy Brain: GitHub will not let this account mark its "
                "own comment as the answer (viewerCanMarkAsAnswer="
                "false); stopping this badge.\n"
                "Fallback: have a partner account mark your comments as "
                "answers, or open each discussion under "
                f"https://github.com/{owner}/{repo}/discussions and "
                "click \"Select as answer\" yourself (2 clicks per "
                "discussion).")
            client.log(f"galaxy_brain: {owner}/{repo} self-mark "
                       "disallowed; stopped for manual/partner fallback")
            return -1
        client.graphql(
            "mutation($commentId:ID!){"
            " markDiscussionCommentAsAnswer(input:{id:$commentId}){"
            "  discussion{id}"
            " }"
            "}",
            {"commentId": comment.get("id")})
        done += 1
        number = discussion.get("number")
        print(f"  answered discussion "
              f"#{number if number is not None else '?'} "
              f"({done}/{remaining})")
    client.log(f"galaxy_brain: {owner}/{repo} ran {done} answer(s) "
               f"toward {target}")
    return done


def badge_pair_extraordinaire(client: GitHubClient, owner: str, repo: str,
                              coauthor: str, target: int) -> int:
    """Merge co-authored PRs until ``target`` merged PRs with a
    ``Co-authored-by`` trailer exist in ``owner/repo``; returns how many
    cycles ran this invocation.

    ``coauthor`` is validated by ``parse_coauthor`` first — invalid
    input fails fast before any API call is made. Each cycle is a plain
    ``pr_cycle`` whose commit message ends with
    ``"\\n\\n" + format_coauthor_trailer(name, email)``, so the co-author
    credit lands in the commit trailer where Pair Extraordinaire looks
    for it.

    Idempotency: the closed-PR list (same shape as ``count_merged_prs``
    — merged, authored by the authenticated user) is scanned, fetching
    each merged PR's commits via ``GET /pulls/{n}/commits?per_page=10``
    and counting PRs where ANY commit message contains
    ``Co-authored-by:``. The scan is bounded to the first 50 merged PRs;
    ``remaining = max(0, target - counted)`` cycles run.
    """
    name, email = parse_coauthor(coauthor)  # fail fast, before any API call
    trailer = format_coauthor_trailer(name, email)

    login = client.rest("GET", "/user").get("login")
    pulls = client.rest(
        "GET", f"/repos/{owner}/{repo}/pulls?state=closed&per_page=100")
    coauthored = 0
    scanned = 0
    if isinstance(pulls, list):
        for pr in pulls:
            if scanned >= 50:
                break  # bounded scan: first 50 merged PRs only
            if not isinstance(pr, dict) or pr.get("merged_at") is None:
                continue
            if (pr.get("user") or {}).get("login") != login:
                continue
            scanned += 1
            commits = client.rest(
                "GET",
                f"/repos/{owner}/{repo}/pulls/{pr.get('number')}"
                f"/commits?per_page=10")
            if not isinstance(commits, list):
                continue
            if any(
                "Co-authored-by:" in
                ((c.get("commit") or {}).get("message") or "")
                for c in commits if isinstance(c, dict)
            ):
                coauthored += 1

    remaining = max(0, target - coauthored)
    if remaining == 0:
        print(f"Pair Extraordinaire: already at {coauthored}/{target} "
              "co-authored merged PRs; nothing to do.")
        client.log(f"pair_extraordinaire: {owner}/{repo} already at "
                   f"{coauthored}/{target}")
        return 0
    print(f"Pair Extraordinaire: {coauthored}/{target} co-authored "
          f"merged PRs; running {remaining} PR cycle(s).")
    done = 0
    for i in range(remaining):
        n = coauthored + i + 1
        number = pr_cycle(
            client, owner, repo,
            f"pair-extraordinaire-{n}",
            f"Pair Extraordinaire PR {n}/{target}\n\n{trailer}")
        done += 1
        print(f"  merged PR #{number} ({done}/{remaining})")
    client.log(f"pair_extraordinaire: {owner}/{repo} ran {done} "
               f"cycle(s) toward {target}")
    return done


# ---------------------------------------------------------------------------
# Task 7: CLI wiring — main(), unlock orchestration, manual steps.

DEFAULT_REPO_NAME = "achievement"

# ``--tier base`` runs one cycle per badge (kickstart progress); the
# default ``--tier bronze`` runs every badge to its full Bronze target
# from TIER_TARGETS.
TIER_BASE_TARGETS = {slug: 1 for slug in TIER_TARGETS}


def cmd_manual() -> int:
    """Print the exact steps for the two badges that cannot be automated
    and return 0. Public Sponsor needs a real $1 payment via
    https://github.com/sponsors; Starstruck needs 16 stars from other
    people (self-stars don't count)."""
    print(
        "Manual steps for the badges this tool cannot automate\n"
        "\n"
        "Public Sponsor (Bronze: sponsor 1 developer)\n"
        "  1. Go to https://github.com/sponsors and pick any sponsorable\n"
        "     developer (browse the directory, or open a profile that\n"
        "     shows a Sponsor button).\n"
        "  2. Complete a $1 sponsorship. This needs a real payment - a\n"
        "     credit card or PayPal on file - there is no API-only path.\n"
        "  3. The badge is awarded once the payment goes through.\n"
        "\n"
        "Starstruck (Bronze: 16 stars)\n"
        "  1. Stars must come from OTHER people - starring your own repo\n"
        "     does not count, and neither do second accounts you control\n"
        "     (that violates GitHub's terms of service).\n"
        "  2. Share your playground repo (the one this tool published)\n"
        "     with friends, colleagues, or communities and ask them to\n"
        "     star it.\n"
        "  3. Once 16 different people have starred it, the badge is\n"
        "     awarded automatically.\n"
        "\n"
        "Note: achievements may take up to 24-48 hours to render on your\n"
        "profile after the qualifying activity.\n")
    return 0


def _resolve_repo_target(client: GitHubClient,
                         repo_arg: str | None) -> tuple[str, str, str]:
    """Resolve ``--repo`` into ``(login, owner, name)`` for a write command.

    ``--repo`` accepts ``OWNER/NAME`` or a bare ``NAME`` (the authenticated
    user's repo); the default is the playground repo
    ``{login}/{DEFAULT_REPO_NAME}``.

    Ownership guard — the "writes only against owned repos" rule: in live
    mode the login is resolved via ``GET /user`` and the target repo is
    fetched via ``GET /repos/{owner}/{name}`` to verify
    ``owner.login == login``. A missing repo (404) is tolerated only when
    the requested owner is the login (publish creates it there); a repo
    owned by anyone else raises ``GHError`` before a single write runs.
    In dry-run mode the network check is skipped and the ``OWNER/NAME``
    format is trusted.

    Raises ``GHError`` on an invalid argument or an ownership violation.
    """
    if repo_arg:
        parts = repo_arg.split("/")
        if len(parts) == 2 and all(part.strip() for part in parts):
            owner, name = parts[0].strip(), parts[1].strip()
        elif len(parts) == 1 and repo_arg.strip():
            owner, name = None, repo_arg.strip()
        else:
            raise GHError(
                0, f"invalid --repo {repo_arg!r}: expected OWNER/NAME "
                   "(e.g. octocat/achievement) or a bare NAME")
    else:
        owner, name = None, DEFAULT_REPO_NAME
    user = client.rest("GET", "/user")
    login = user.get("login")
    if not login:
        if client.dry_run:
            login = "<login>"  # dry-run GETs return {}; keep printing plan
        else:
            raise GHError(0, "could not resolve the authenticated user's "
                            "login")
    if owner is None:
        owner = login
    if not client.dry_run:
        try:
            info = client.rest("GET", f"/repos/{owner}/{name}")
        except GHError as exc:
            if exc.status == 404 and owner.casefold() == login.casefold():
                info = None  # own repo, not created yet: publish creates it
            else:
                raise
        else:
            actual = (info.get("owner") or {}).get("login") or ""
            if actual.casefold() != login.casefold():
                raise GHError(
                    0, f"{owner}/{name} is owned by {actual!r}, not by you "
                       f"({login}); this tool only writes to repositories "
                       "you own")
    return login, owner, name


def _cli_publish(client: GitHubClient,
                 args: argparse.Namespace) -> int:
    """``publish`` subcommand: resolve the target (ownership guard), then
    create-or-reuse the playground repo and push ``main``. Returns 0 only
    when the push succeeded."""
    try:
        _, _, name = _resolve_repo_target(client, args.repo)
        info = cmd_publish(client, name)
    except GHError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not info.get("pushed"):
        return 1
    print(f"publish: {info.get('full_name')} is ready at "
          f"{info.get('html_url')}")
    return 0


def _cli_unlock(client: GitHubClient,
                args: argparse.Namespace) -> int:
    """``unlock`` subcommand: publish, then run every automatable badge
    (quickdraw -> yolo -> pull-shark -> galaxy-brain ->
    pair-extraordinaire).

    Each step is wrapped so a ``GHError`` — or any unexpected exception
    — is reported without stopping the others; a final summary table
    lists every step's status. Galaxy Brain
    returning ``-1`` (self-marking blocked) is a WARNING, not a failure.
    Without ``--coauthor``, Pair Extraordinaire is skipped with a notice
    explaining how to pass it. Returns 1 when any step FAILED, else 0.
    """
    try:
        login, owner, name = _resolve_repo_target(client, args.repo)
    except GHError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    targets = (TIER_TARGETS if args.tier == "bronze"
               else TIER_BASE_TARGETS)
    plan = ["publish (setup)", "Quickdraw", "YOLO", "Pull Shark",
            "Galaxy Brain"]
    if args.coauthor:
        plan.append("Pair Extraordinaire")
    print(f"Unlock plan ({args.tier} tier) for {owner}/{name}:")
    print("  " + " -> ".join(plan))
    if client.dry_run:
        print("  targets: " + ", ".join(
            f"{slug}={targets[slug]}" for slug in TIER_TARGETS))

    results: list[tuple[str, str, str]] = []  # (step, status, detail)

    try:
        info = cmd_publish(client, name)
        if info.get("pushed"):
            results.append(("publish", "OK",
                            f"pushed {info.get('full_name')}"))
        else:
            results.append(("publish", "FAILED", "git push did not succeed"))
    except GHError as exc:
        results.append(("publish", "FAILED", str(exc)))
        print(f"publish failed: {exc}", file=sys.stderr)
    except Exception as exc:
        detail = f"unexpected error: {exc!r}"
        results.append(("publish", "FAILED", detail))
        client.log(f"unlock: publish {detail}")
        print(f"publish {detail}", file=sys.stderr)

    try:
        if badge_quickdraw(client, owner, name):
            results.append(("Quickdraw", "OK", "issue opened and closed"))
        else:
            results.append(("Quickdraw", "FAILED",
                            "attempt failed (see messages above)"))
    except GHError as exc:
        results.append(("Quickdraw", "FAILED", str(exc)))
        print(f"Quickdraw failed: {exc}", file=sys.stderr)
    except Exception as exc:
        detail = f"unexpected error: {exc!r}"
        results.append(("Quickdraw", "FAILED", detail))
        client.log(f"unlock: Quickdraw {detail}")
        print(f"Quickdraw {detail}", file=sys.stderr)

    try:
        if badge_yolo(client, owner, name):
            results.append(("YOLO", "OK", "PR merged with no review"))
        else:
            results.append(("YOLO", "FAILED",
                            "attempt failed (see messages above)"))
    except GHError as exc:
        results.append(("YOLO", "FAILED", str(exc)))
        print(f"YOLO failed: {exc}", file=sys.stderr)
    except Exception as exc:
        detail = f"unexpected error: {exc!r}"
        results.append(("YOLO", "FAILED", detail))
        client.log(f"unlock: YOLO {detail}")
        print(f"YOLO {detail}", file=sys.stderr)

    try:
        done = badge_pull_shark(client, owner, name,
                                targets["pull_shark"])
        results.append(("Pull Shark", "OK",
                        f"{done} PR cycle(s) this run"))
    except GHError as exc:
        results.append(("Pull Shark", "FAILED", str(exc)))
        print(f"Pull Shark failed: {exc}", file=sys.stderr)
    except Exception as exc:
        detail = f"unexpected error: {exc!r}"
        results.append(("Pull Shark", "FAILED", detail))
        client.log(f"unlock: Pull Shark {detail}")
        print(f"Pull Shark {detail}", file=sys.stderr)

    try:
        done = badge_galaxy_brain(client, owner, name,
                                  targets["galaxy_brain"])
        if done == -1:
            # self-mark blocked: not a failure — the fallback note printed
            # by the badge explains the partner/manual path
            results.append(("Galaxy Brain", "WARNING",
                            "self-marking blocked; follow the fallback "
                            "note printed above"))
        else:
            results.append(("Galaxy Brain", "OK",
                            f"{done} answer(s) this run"))
    except GHError as exc:
        results.append(("Galaxy Brain", "FAILED", str(exc)))
        print(f"Galaxy Brain failed: {exc}", file=sys.stderr)
    except Exception as exc:
        detail = f"unexpected error: {exc!r}"
        results.append(("Galaxy Brain", "FAILED", detail))
        client.log(f"unlock: Galaxy Brain {detail}")
        print(f"Galaxy Brain {detail}", file=sys.stderr)

    if not args.coauthor:
        print('Pair Extraordinaire: skipped - pass '
              '--coauthor "Name <email>" to earn this badge.')
        results.append(("Pair Extraordinaire", "SKIPPED",
                        'needs --coauthor "Name <email>"'))
    else:
        try:
            done = badge_pair_extraordinaire(
                client, owner, name, args.coauthor,
                targets["pair_extraordinaire"])
            results.append(("Pair Extraordinaire", "OK",
                            f"{done} PR cycle(s) this run"))
        except GHError as exc:
            results.append(("Pair Extraordinaire", "FAILED", str(exc)))
            print(f"Pair Extraordinaire failed: {exc}", file=sys.stderr)
        except ValueError as exc:
            # parse_coauthor's usage-hint message (with the
            # --coauthor "Name <email>" example) is preserved verbatim
            results.append(("Pair Extraordinaire", "FAILED", str(exc)))
            print(f"Pair Extraordinaire failed: {exc}", file=sys.stderr)
        except Exception as exc:
            detail = f"unexpected error: {exc!r}"
            results.append(("Pair Extraordinaire", "FAILED", detail))
            client.log(f"unlock: Pair Extraordinaire {detail}")
            print(f"Pair Extraordinaire {detail}", file=sys.stderr)

    width = max(len(step) for step, _, _ in results)
    print("\nUnlock summary:")
    print(f"{'Step':<{width}}  {'Status':<8}  Detail")
    print("-" * (width + 40))
    for step, status, detail in results:
        print(f"{step:<{width}}  {status:<8}  {detail}")
    failed = [step for step, status, _ in results if status == "FAILED"]
    if failed:
        print(f"\n{len(failed)} step(s) failed: "
              + ", ".join(failed), file=sys.stderr)
        return 1
    print("\nAll steps succeeded. Achievements may take up to 24-48h to "
          "render on your profile (check with `status`).")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog="github_achievements",
        description="Earn GitHub profile achievements on your own repos.")
    sub = parser.add_subparsers(
        dest="command", required=True,
        metavar="{doctor,publish,status,unlock,manual}")

    token_help = ("GitHub personal access token (default: the "
                  "GITHUB_TOKEN environment variable or a .env file)")

    p = sub.add_parser("doctor", help="check token health via GET /user")
    p.add_argument("--token", help=token_help)

    p = sub.add_parser(
        "publish", help=f"create-or-reuse the playground repo "
                        f"(default <login>/{DEFAULT_REPO_NAME}) and push "
                        "main")
    p.add_argument("--token", help=token_help)
    p.add_argument("--repo", metavar="OWNER/NAME",
                   help="target repo, OWNER/NAME or bare NAME "
                        f"(default <login>/{DEFAULT_REPO_NAME})")
    p.add_argument("--dry-run", action="store_true",
                   help="print the plan without making any changes")

    p = sub.add_parser("status",
                       help="show earned achievements vs targets")
    p.add_argument("--token", help=token_help)

    p = sub.add_parser(
        "unlock", help="run every automatable badge in order")
    p.add_argument("--token", help=token_help)
    p.add_argument("--repo", metavar="OWNER/NAME",
                   help="target repo, OWNER/NAME or bare NAME "
                        f"(default <login>/{DEFAULT_REPO_NAME})")
    p.add_argument("--dry-run", action="store_true",
                   help="print each badge's exact call plan (paths and "
                        "counts) without any writes")
    p.add_argument("--tier", choices=("base", "bronze"), default="bronze",
                   help="base: one cycle per badge; bronze: full Bronze "
                        "targets (default)")
    p.add_argument("--coauthor", metavar='"Name <email>"',
                   help='co-author for Pair Extraordinaire, e.g. '
                        '"Ada Lovelace <ada@x.io>" (omit to skip that '
                        "badge)")

    sub.add_parser("manual", help="manual steps for Public Sponsor and "
                                  "Starstruck")

    args = parser.parse_args(argv)
    if args.command == "manual":
        return cmd_manual()
    dry_run = bool(getattr(args, "dry_run", False))
    token = args.token or os.environ.get("GITHUB_TOKEN") or _dotenv_token()
    if not token:
        if dry_run:
            # --dry-run never makes an authenticated call (no live GETs,
            # no writes), so a missing token is tolerable: use a
            # placeholder and keep printing the plan.
            token = "dry-run"
        else:
            # doctor/status (and live runs) need a real token — print the
            # PAT instructions and exit 1
            token = load_token(args.token)
    # dry-run constructs only the dry-run client — a live, writing client
    # is never instantiated when --dry-run is passed
    client = GitHubClient(token, dry_run=dry_run)
    if args.command == "doctor":
        cmd_doctor(client)
        return 0
    if args.command == "status":
        cmd_status(client)
        return 0
    if args.command == "publish":
        return _cli_publish(client, args)
    return _cli_unlock(client, args)


if __name__ == "__main__":
    sys.exit(main())
