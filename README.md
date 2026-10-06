# github_achievements

Earn GitHub profile achievements on your own repositories with a single,
stdlib-only Python CLI — no third-party packages, just Python 3.10+, `git`,
and a personal access token.

## Setup

1. Create a personal access token at
   <https://github.com/settings/tokens> with the scopes
   `repo`, `read:discussion`, `write:discussion`.
2. Provide the token one of three ways (checked in this order):
   - `--token <token>` on the command line,
   - a `GITHUB_TOKEN` environment variable,
   - a `.env` file in the working directory containing
     `GITHUB_TOKEN=<token>` (add `.env` to your `.gitignore` — never commit it).

## Usage

```console
# check the token and show who you are
python github_achievements.py doctor

# see which badges you already earned vs their targets
python github_achievements.py status

# preview every API call and git command, with zero writes
python github_achievements.py unlock --dry-run --token <token>

# run every automatable badge to its full Bronze target on <login>/achievement
python github_achievements.py unlock

# include Pair Extraordinaire (10 co-authored merged PRs)
python github_achievements.py unlock --coauthor "Ada Lovelace <ada@example.io>"

# one cycle per badge instead of the full Bronze targets
python github_achievements.py unlock --tier base

# target a specific repo you own (OWNER/NAME or bare NAME)
python github_achievements.py unlock --repo octocat/achievement

# create-or-reuse the playground repo and push main, on its own
python github_achievements.py publish

# exact steps for the two badges that cannot be automated
python github_achievements.py manual
```

`unlock` runs the badges in order — publish (setup) -> Quickdraw -> YOLO
-> Pull Shark -> Galaxy Brain -> Pair Extraordinaire — each failure is
caught, reported, and the remaining badges continue; a summary table
ends the run. The exit code is `1` if any badge failed, `0` otherwise.
Every run also appends a token-free activity log to
`achievement_run.log`.

## Badge table

| Badge | Bronze target | Category | How it's earned |
|---|---|---|---|
| Quickdraw | 1 issue closed < 5 min after opening | Automated | `unlock` opens and immediately closes an issue |
| YOLO | 1 PR merged without a review | Automated | `unlock` merges a PR with no review requested |
| Pull Shark | 16 merged PRs | Automated | `unlock` merges PRs (idempotent — only the remainder) |
| Galaxy Brain | 8 accepted answers in Discussions | Automated* | `unlock` posts self-answered Q&A discussions |
| Pair Extraordinaire | 10 merged co-authored PRs | Automated | `unlock --coauthor "Name <email>"` puts a `Co-authored-by:` trailer on each commit |
| Starstruck | 16 stars | Manual | stars from **other people** — share the repo; self-stars don't count |
| Public Sponsor | 1 sponsorship | Manual | a real $1 payment at <https://github.com/sponsors>; no API-only path |
| Arctic Code Vault Contributor | — | Impossible | one-off 2020 event; retired |
| Mars 2020 Contributor | — | Impossible | one-off 2020 event; retired |

\* Galaxy Brain: GitHub may disallow marking your own comment as the
answer (`viewerCanMarkAsAnswer: false`). The tool stops that badge and
prints a fallback: have a partner mark your comments as answers, or
click "Select as answer" yourself in each discussion
(2 clicks per discussion).

Without `--coauthor`, `unlock` skips Pair Extraordinaire with a notice
explaining how to pass it.

## Safety notes

- **Your own repos only.** `unlock` and `publish` verify — via
  `GET /user` and `GET /repos/{owner}/{repo}` — that the target repo
  belongs to the authenticated login before any write runs; a repo
  owned by someone else is rejected outright. In `--dry-run` the
  network check is skipped and the `OWNER/NAME` argument is trusted,
  since nothing is written anyway.
- **No second accounts.** Earning achievements with additional
  accounts you control (starring yourself, marking your own answers,
  fake co-authors) violates GitHub's terms of service. Use a real
  collaborator's `--coauthor` identity, and get real stars from real
  people.
- **Token handling.** The token is sent only in the `Authorization`
  header, is scrubbed from every log line, and is never printed. Keep
  it in `.env` or an environment variable; do not commit it.
- **Dry-run first.** `--dry-run` instantiates only a dry-run client and
  prints the exact plan (every API path, body, and count, plus the git
  commands) without making a single HTTP call or running a single git
  command.
- **Politeness.** The client sleeps between mutating calls and honors
  `Retry-After` on rate-limit responses with capped backoff.

## Timing note

Achievements may take up to **24–48 hours** to render on your profile
after the qualifying activity. `status` reads the public profile page,
so freshly earned badges may not appear immediately.
