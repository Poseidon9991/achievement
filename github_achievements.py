"""github_achievements — earn GitHub profile achievements on your own repos.

Single-file, stdlib-only CLI. Task 1 provides the core plumbing:
``GitHubClient`` (REST + GraphQL over ``urllib`` with politeness delays and
Retry-After backoff), token loading (``--token`` > ``GITHUB_TOKEN`` > ``.env``),
an activity log that never writes the token, and shared constants/helpers
(``TIER_TARGETS``, ``format_coauthor_trailer``) used by the badge tasks.
"""

from __future__ import annotations

import json
import os
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
    - ``dry_run=True`` logs the intended call and returns ``{}`` — zero writes.
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
            self.log(f"DRY-RUN {method} {path} body={json.dumps(body)}")
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
