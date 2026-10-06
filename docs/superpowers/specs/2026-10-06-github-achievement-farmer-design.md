# GitHub Achievement Farmer — Design Spec

Date: 2026-10-06
Owner: Poseidon9991
Status: Approved in chat (approach A, Bronze tier, this repo as playground)

## Purpose

A single-file, zero-dependency Python CLI that unlocks every GitHub profile
achievement that a solo user can legitimately earn, on the user's own
repositories, using the user's own stored Git Credential Manager token.

## Scope

**In scope:** 5 automatable achievements (base + Bronze tiers), publishing this
repo as the public playground, a status/verification command, manual-step
instructions for the 2 remaining earnable badges.

**Out of scope (by policy):** second/sock-puppet accounts (ToS violation), any
activity in repos the user does not own, paid actions (sponsorships),
star-inflation.

## Badge targets (Bronze tier run)

| Badge | Criterion (verified) | Target |
|---|---|---|
| Quickdraw | Close an issue/PR within 5 min of opening | 1 |
| YOLO | Merge a PR with zero reviews | 1 |
| Pull Shark | Merged PRs authored by user (2/16/128/1024) | 16 |
| Galaxy Brain | Accepted answers in Discussions (2/8/16/32) | 8 |
| Pair Extraordinaire | Co-authored commit in a merged PR (1/10/24/48) | 10 |
| Public Sponsor | Manual ($1 sponsorship) | manual |
| Starstruck | Manual (16 stars from other people) | manual |
| Arctic Vault / Mars 2020 / Heart On Your Sleeve / Open Sourcerer | Retired or never released | impossible |

Counting rules honored (per community catalog, verified 2026-04): standalone
repo (not a fork), public, changes land on default branch via merge, commits
use the authenticated account identity (Contents API does this automatically),
issues/PRs/discussions only count outside forks.

## Architecture

```
achievement/
├── github_achievements.py   # single-file tool, stdlib only
├── .env                      # GITHUB_TOKEN=...  (gitignored)
├── .gitignore                # .env, __pycache__
└── README.md
```

### Components (inside the single file)

1. **GitHubClient** — wraps REST + GraphQL over `urllib.request`.
   - Token from `--token`, `GITHUB_TOKEN`, or `.env`.
   - Politeness delay (2.5s) between mutating calls; honors `Retry-After`
     on 403/429 with exponential backoff, max 3 retries.
   - Never logs the token; logs every request method/path/status to
     `achievement_run.log`.
2. **Badges** — one function per badge, each idempotent (checks existing
   progress before acting, e.g. counts already-merged playground PRs toward
   Pull Shark's 16).
3. **Status** — scrapes `https://github.com/<user>` profile HTML for the
   achievement badge names already displayed; prints target/earned table.
4. **CLI** — argparse subcommands: `doctor`, `publish`, `status`, `unlock`,
   `manual`, each with `--dry-run`.

### API usage per badge

- **publish**: `POST /user/repos` (create `achievement`, public, no auto-init)
  then local `git push` (GCM supplies credentials).
- **quickdraw**: `POST /repos/{o}/{r}/issues` → `PATCH .../issues/{n}`
  `{"state":"closed"}` immediately.
- **yolo** (also emitted by any review-free merge): create branch
  (`POST .../git/refs`), commit via Contents API (`PUT .../contents/{path}`),
  `POST .../pulls`, `PUT .../pulls/{n}/merge` (method `merge`).
- **pull-shark 16**: repeat the PR loop; each iteration uses a unique branch
  and file path; deletes the branch after merge.
- **galaxy-brain 8**: `PATCH /repos/{o}/{r}` `{"has_discussions":true}`;
  GraphQL `createDiscussion` in an answerable category,
  `addDiscussionComment` (the answer), `markDiscussionCommentAsAnswer`.
  Self-marking is attempted first; `viewerCanMarkAsAnswer` is inspected and
  if GitHub disallows self-marking, the tool stops that badge and reports
  the exact fallback (partner marks, or 2 manual UI clicks per answer), never
  guessing.
- **pair-extraordinaire 10**: same PR loop but the Contents API commit message
  ends with `Co-authored-by: <friend-name> <friend-email>` trailer; friend
  identity passed via `--coauthor "Name <email>"` or prompted interactively.

### Error handling

- Any 4xx on a badge step: log, mark badge as failed, continue other badges;
  exit code 1 if any failed; summary table at the end.
- Missing token scopes (`doctor` detects via `GET /user` + header inspection):
  print exact PAT instructions as fallback.
- Achievements may take up to 24–48h to render on the profile — stated in the
  final summary.

## Security

- Token extracted from GCM via `git credential fill` piped straight into
  `.env`; never echoed to console, logs, or git (`.gitignore`).
- All activity confined to repos owned by the authenticated user.
- No second accounts, no other people's repos, no payment actions.

## Verification plan

1. `python github_achievements.py doctor` — token valid, scopes OK.
2. `unlock --dry-run` — prints the full call plan, zero writes.
3. Real run → `status` before/after; profile URL given for manual check.
4. Unit checks: `python -c "import github_achievements"` plus a self-test
   mode asserting URL construction and trailer formatting.
