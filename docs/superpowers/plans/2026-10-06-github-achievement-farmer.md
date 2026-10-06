# GitHub Achievement Farmer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A single-file, zero-dependency Python CLI that unlocks the 5 solo-earnable GitHub achievements (Bronze tier) on the user's own repos, plus status/manual commands.

**Architecture:** One stdlib-only module `github_achievements.py` containing a `GitHubClient` (REST + GraphQL over `urllib`, politeness delays, Retry-After backoff, activity log), one function per badge sharing a PR-loop engine, and an argparse CLI. Tests import the module and run with `python -m unittest`.

**Tech Stack:** Python 3.13 stdlib only (urllib.request, unittest, argparse). No pip installs.

**Spec:** `docs/superpowers/specs/2026-10-06-github-achievement-farmer-design.md`

## Global Constraints

- Zero third-party dependencies; `import` of anything outside stdlib is a bug.
- All tool code lives in `github_achievements.py`; tests live in `tests/test_github_achievements.py`.
- Token is read from `--token`, `GITHUB_TOKEN`, or `.env` (`GITHUB_TOKEN=...`); never printed, logged, or committed.
- `.gitignore` contains `.env`, `achievement_run.log`, `__pycache__/` from Task 1 onward.
- Mutating calls are separated by 2.5s; HTTP 403/429 honored via `Retry-After` (max 3 retries, exponential).
- Bronze targets: quickdraw 1, yolo 1, pull-shark 16, galaxy-brain 8, pair-extraordinaire 10 (constant `TIER_TARGETS`).
- Playground repo: `Poseidon9991/achievement`, public, standalone (never a fork); PR merge method `"merge"`.
- The tool only performs writes against repos whose `owner.login` equals the authenticated `GET /user` login.
- Dry-run prints the exact call plan, performs zero writes.

## Review Focus

1. **Missing/invalid token** — doctor must report a clear PAT fallback and never echo the token. Test in Task 1 (log scrubbing) and Task 2 (doctor).
2. **Secondary rate limit (403 + Retry-After)** — client must sleep and retry, not crash. Test in Task 1 with a mocked opener.
3. **Malformed co-author** (`"Name"` without `<email>`) — rejected with usage message, not sent to API. Test in Task 6.
4. **Galaxy Brain self-marking disallowed** (`viewerCanMarkAsAnswer: false`) — badge stops cleanly with the fallback note. Test in Task 5 with a mocked response.
5. **Profile page with zero badges** — `status` returns an empty list, no crash. Test in Task 2 with fixture HTML.

---

### Task 1: GitHubClient core + token loading + scaffolding

**Files:**
- Create: `github_achievements.py`
- Create: `tests/test_github_achievements.py`
- Create: `.gitignore`

**Interfaces:**
- Produces:
  - `load_token(cli_token: str | None) -> str` — raises `SystemExit` with PAT instructions if none found; reads `.env` line-by-line.
  - `class GitHubClient(token: str, dry_run: bool = False, delay: float = 2.5)`
    - `rest(method: str, path: str, body: dict | None = None) -> dict` — sends JSON to `https://api.github.com{path}`, returns parsed JSON (`{}` for 204); raises `GHError(status, message, retry_after)` after retries.
    - `graphql(query: str, variables: dict | None = None) -> dict` — POST `/graphql`, raises `GHError` on `errors` key.
    - `class GHError(Exception)` with `.status`, `.message`, `.retry_after`.
  - Module-level `LOG_PATH = "achievement_run.log"`; client method `log(msg: str)` appends timestamped lines.

- [ ] **Step 1: Write failing tests** — `load_token` precedence (flag > env > `.env`), `.env` parsing, `GHError` on GraphQL `errors`, 403-with-`Retry-After` triggers exactly one retry then success (mock `urlopen`, assert two sends), token absent from log file after `log()` calls.
- [ ] **Step 2: Run** `python -m unittest -v` — expect failure (module missing).
- [ ] **Step 3: Implement** client (urllib request builder, backoff `min(2**attempt * int(retry_after or 1), 120)`, `time.sleep(delay)` before each mutating call, log scrubbing `token[:0]` never written), `.gitignore`, `TIER_TARGETS`, `format_coauthor_trailer`.
- [ ] **Step 4: Run tests** — pass.
- [ ] **Step 5: Commit** — `feat: GitHubClient core with rate-limit backoff and token loading`.

### Task 2: doctor + status (profile badge parsing)

**Files:**
- Modify: `github_achievements.py`
- Test: `tests/test_github_achievements.py`

**Interfaces:**
- Consumes: `GitHubClient`, `GHError`.
- Produces:
  - `parse_profile_badges(html: str) -> list[str]` — extracts achievement names from the profile achievements section (regex over `aria-label`/`alt` badge attributes), dedupes.
  - `cmd_doctor(client: GitHubClient) -> None` — `GET /user`, prints login and plan summary; on 401 prints PAT fallback URL `https://github.com/settings/tokens`.
  - `cmd_status(client: GitHubClient) -> None` — fetches `https://github.com/{login}` via `urllib` (no auth header), prints target/earned table for the 9 earnable badges.

- [ ] **Step 1: Failing tests** — `parse_profile_badges` on fixture HTML with 3 badges → `["Quickdraw", "Pull Shark", "YOLO"]`; empty-profile fixture → `[]`; `cmd_doctor` with mocked 401 prints fallback and exits 1.
- [ ] **Step 2: Run** — fail.
- [ ] **Step 3: Implement** parser + two commands.
- [ ] **Step 4: Run** — pass.
- [ ] **Step 5: Commit** — `feat: doctor and status commands with profile badge parsing`.

### Task 3: publish (repo creation + push)

**Files:**
- Modify: `github_achievements.py`
- Test: `tests/test_github_achievements.py`

**Interfaces:**
- Consumes: `GitHubClient`, `load_token`.
- Produces: `cmd_publish(client: GitHubClient, name: str) -> dict` — if `GET /repos/{login}/{name}` 404s, `POST /user/repos` `{"name": name, "private": False, "auto_init": False}`, then runs `git remote add origin https://github.com/{login}/{name}.git` and `git push -u origin main` via `subprocess.run` (check=False, capture output, log stdout); idempotent when repo exists.

- [ ] **Step 1: Failing tests** — repo-exists path skips `POST /user/repos` (assert no `posts` recorded on mock); 404 path creates then pushes (mock subprocess).
- [ ] **Step 2: Run** — fail.
- [ ] **Step 3: Implement** with `subprocess.run(["git", ...])`.
- [ ] **Step 4: Run** — pass.
- [ ] **Step 5: Commit** — `feat: publish command creating and pushing playground repo`.

### Task 4: PR engine + quickdraw + yolo + pull-shark

**Files:**
- Modify: `github_achievements.py`
- Test: `tests/test_github_achievements.py`

**Interfaces:**
- Consumes: `GitHubClient`, `TIER_TARGETS`.
- Produces:
  - `count_merged_prs(client: GitHubClient, owner: str, repo: str) -> int` — `GET /repos/{o}/{r}/pulls?state=closed&per_page=100`, counts `merged_at is not None`.
  - `pr_cycle(client: GitHubClient, owner: str, repo: str, label: str, message: str) -> int` — ref create (`POST .../git/refs`, branch off default HEAD), Contents API commit (`PUT .../contents/notes/{label}.md`), `POST .../pulls`, `PUT .../pulls/{n}/merge` `{"merge_method": "merge"}`, delete ref; returns PR number.
  - `badge_quickdraw(client, owner, repo) -> bool` — issue open + immediate close.
  - `badge_yolo(client, owner, repo) -> bool` — one `pr_cycle` with no review requested.
  - `badge_pull_shark(client, owner, repo, target: int) -> int` — idempotent: `remaining = max(0, target - count_merged_prs(...))`, then `remaining` cycles.

- [ ] **Step 1: Failing tests** — `count_merged_prs` on canned JSON (2 merged of 3 closed → 2); pull-shark with 2 existing merged PRs and target 16 performs 14 cycles (mock call count); quickdraw closes with `{"state": "closed"}` immediately after create; `pr_cycle` sends 5 REST calls in order (refs → contents → pulls → merge → refs delete).
- [ ] **Step 2: Run** — fail.
- [ ] **Step 3: Implement** engine + three badges.
- [ ] **Step 4: Run** — pass.
- [ ] **Step 5: Commit** — `feat: PR engine plus quickdraw, yolo, pull-shark badges`.

### Task 5: Galaxy Brain

**Files:**
- Modify: `github_achievements.py`
- Test: `tests/test_github_achievements.py`

**Interfaces:**
- Consumes: `GitHubClient`, `pr_cycle`-style logging.
- Produces:
  - `badge_galaxy_brain(client: GitHubClient, owner: str, repo: str, target: int) -> int` — `PATCH /repos/{o}/{r}` `{"has_discussions": true}`; GraphQL `repository.id` + answerable category (fallback: `createDiscussionCategory` mutation with `name: "Q&A"`); per answer: `createDiscussion` → `addDiscussionComment` → read `viewerCanMarkAsAnswer` on the comment → if true `markDiscussionCommentAsAnswer`, else `return -1` and print fallback note (partner marks or manual "Select as answer" click); counts prior accepted answers via `discussions(first: 50)` where `answer.author.login == login`.

- [ ] **Step 1: Failing tests** — enable-discussions PATCH payload; answerable-category selection from canned GraphQL nodes; self-mark disallowed path returns `-1` and prints "fallback" without mutating further; count logic on canned discussion list.
- [ ] **Step 2: Run** — fail.
- [ ] **Step 3: Implement** with the three GraphQL mutations.
- [ ] **Step 4: Run** — pass.
- [ ] **Step 5: Commit** — `feat: galaxy brain badge with self-mark fallback`.

### Task 6: Pair Extraordinaire

**Files:**
- Modify: `github_achievements.py`
- Test: `tests/test_github_achievements.py`

**Interfaces:**
- Consumes: `pr_cycle`, `format_coauthor_trailer`.
- Produces:
  - `parse_coauthor(raw: str) -> tuple[str, str]` — validates `"Name <email>"`, raises `ValueError` with usage example on missing email or angle brackets.
  - `badge_pair_extraordinaire(client, owner, repo, coauthor: str, target: int) -> int` — `target` `pr_cycle`s where `message` ends with `\n\nCo-authored-by: Name <email>`; idempotent via counting merged PRs whose commits contain the trailer (skip if already reached).

- [ ] **Step 1: Failing tests** — `parse_coauthor("Ada <ada@x.io>")` → `("Ada", "ada@x.io")`; `parse_coauthor("Ada")` and `parse_coauthor("Ada ada@x.io")` raise `ValueError` mentioning `--coauthor "Name <email>"`; commit message ends with exact trailer; target already reached → 0 cycles.
- [ ] **Step 2: Run** — fail.
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run** — pass.
- [ ] **Step 5: Commit** — `feat: pair extraordinaire badge with co-author trailers`.

### Task 7: CLI wiring, dry-run, manual, README, end-to-end check

**Files:**
- Modify: `github_achievements.py`
- Create: `README.md`
- Test: `tests/test_github_achievements.py`

**Interfaces:**
- Consumes: all badge functions, `cmd_publish`, `cmd_status`.
- Produces: `main(argv: list[str] | None = None) -> int` — argparse subcommands `doctor|publish|status|unlock|manual` with `--dry-run`, `--tier {base,bronze}`, `--coauthor`, `--repo`; `unlock` runs publish → quickdraw → yolo → pull-shark → galaxy-brain → pair-extraordinaire (pair skipped with a notice if `--coauthor` absent), each failure caught and reported in a summary table; `manual` prints Public Sponsor ($1 sponsorship steps) and Starstruck (16 stars from others) instructions; dry-run prints each badge's exact call plan (paths + counts) and exits 0 without instantiating a live client.

- [ ] **Step 1: Failing tests** — `main(["manual"])` exits 0 and prints both badge names; dry-run makes zero HTTP calls and mentions every badge name; `unlock` without `--coauthor` skips pair with notice (mock client); missing token exits 1 with PAT URL.
- [ ] **Step 2: Run** — fail.
- [ ] **Step 3: Implement** CLI + README (usage, badge table, safety notes, 24-48h render note).
- [ ] **Step 4: Run** `python -m unittest -v` and `python github_achievements.py --help` — all pass.
- [ ] **Step 5: Commit** — `feat: CLI wiring, dry-run mode, manual steps, README`.
