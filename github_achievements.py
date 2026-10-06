"""github_achievements — earn GitHub profile achievements on your own repos.

Single-file, stdlib-only CLI. Task 1 provides the core plumbing:
``GitHubClient`` (REST + GraphQL over ``urllib`` with politeness delays and
Retry-After backoff), token loading (``--token`` > ``GITHUB_TOKEN`` > ``.env``),
an activity log that never writes the token, and shared constants/helpers
(``TIER_TARGETS``, ``format_coauthor_trailer``) used by the badge tasks.
Task 2 adds ``cmd_doctor`` (token health check) and ``cmd_status`` (scrapes
the public profile page for earned achievements via ``parse_profile_badges``).
"""

from __future__ import annotations

import json
import os
import re
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

_BADGE_ATTR_RE = re.compile(
    r"""(?:alt|aria-label)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_ACHIEVEMENT_PREFIX_RE = re.compile(r"^\s*achievement\s*:\s*", re.IGNORECASE)


class GHError(Exception):
    """GitHub API failure. ``retry_after`` is seconds from the Retry-After
    header, or ``None`` when the server did not supply one."""

    def __init__(self, status: int, message: str,
                 retry_after: int | None = None) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.retry_after = retry_after


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


def _retry_after_seconds(raw: str | None) -> int | None:
    """Parse a Retry-After header value; non-numeric/missing -> ``None``."""
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


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
            return json.loads(raw.decode("utf-8"))
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


def parse_profile_badges(html: str) -> list[str]:
    """Extract achievement names from a GitHub profile page's achievements
    section.

    Badge names appear in ``alt``/``aria-label`` attributes in that section,
    sometimes prefixed with ``"Achievement: "`` (e.g. ``alt="Quickdraw"``,
    ``aria-label="Achievement: YOLO"``). Unknown attribute values are
    ignored; a profile with zero badges returns ``[]``. Results are deduped
    in first-seen (document) order.
    """
    known = set(KNOWN_BADGES)
    earned: list[str] = []
    for match in _BADGE_ATTR_RE.finditer(html):
        name = _ACHIEVEMENT_PREFIX_RE.sub("", match.group(1)).strip()
        if name in known and name not in earned:
            earned.append(name)
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
