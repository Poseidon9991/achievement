"""Tests for github_achievements.py — Tasks 1-7.

Task 1: client core + token loading. Task 2: doctor/status commands and
profile badge parsing. Task 3: publish command (repo creation + git push).
Task 4: PR engine (pr_cycle, count_merged_prs) plus the quickdraw, yolo,
and pull-shark badge drivers. Task 5: badge_galaxy_brain — Discussions
Q&A with a self-mark-disallowed fallback. Task 6: parse_coauthor and
badge_pair_extraordinaire — merged PRs whose commits carry a
Co-authored-by trailer. Task 7: main() CLI wiring — subcommands
doctor/publish/status/unlock/manual, the unlock orchestration with its
ownership guard and per-badge failure summary, dry-run planning, and
the manual steps for the non-automatable badges.
"""

import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock
from urllib.error import HTTPError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import github_achievements as ga

PAT_URL = "https://github.com/settings/tokens"


def _make_response(body: bytes, status: int = 200):
    """A mock that behaves like the object urllib.request.urlopen returns."""
    resp = mock.MagicMock()
    resp.status = status
    resp.read.return_value = body
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def _json_response(payload, status: int = 200):
    return _make_response(json.dumps(payload).encode("utf-8"), status)


def _http_error(status: int, payload: dict, headers: dict | None = None):
    return HTTPError(
        url="https://api.github.com/x",
        code=status,
        msg="error",
        hdrs=headers or {},
        fp=io.BytesIO(json.dumps(payload).encode("utf-8")),
    )


# Fixture: the achievements section of a GitHub profile page. Badge names
# appear in img `alt` and link `aria-label` attributes (sometimes prefixed
# with "Achievement: "); Pull Shark appears twice to exercise dedupe.
PROFILE_HTML_WITH_BADGES = """
<div class="border-top color-border-muted pt-3 mt-3 d-none d-md-block">
  <h2 class="h4 mb-2">Achievements</h2>
  <div class="d-flex flex-wrap">
    <a href="/octocat?achievement=quickdraw&tab=achievements">
      <img src="https://github.githubassets.com/assets/quickdraw-default.png"
           alt="Quickdraw" width="64" height="64">
    </a>
    <a href="/octocat?achievement=pull-shark&tab=achievements"
       aria-label="Pull Shark">
      <img src="pull-shark.png" alt="Pull Shark" width="64" height="64">
    </a>
    <a href="/octocat?achievement=yolo&tab=achievements">
      <img src="yolo.png" aria-label="Achievement: YOLO" width="64">
    </a>
  </div>
</div>
"""

PROFILE_HTML_NO_BADGES = """
<html><body>
  <div class="position-relative">
    <h2 class="h4 mb-2">Achievements</h2>
    <img src="avatar.png" alt="@octocat" width="64">
    <p>Nothing earned yet.</p>
  </div>
</body></html>
"""

# Fixture: a profile README can carry badge-looking alt text
# (`![Pull Shark](img.png)` renders <img alt="Pull Shark">). Only the real
# `?achievement=` href in the achievements section may count — the stray
# img must not produce a false positive.
PROFILE_HTML_STRAY_README_IMG = """
<div class="readme">
  <img src="img.png" alt="Pull Shark">
</div>
<div class="border-top color-border-muted pt-3 mt-3 d-none d-md-block">
  <h2 class="h4 mb-2">Achievements</h2>
  <a href="/octocat?achievement=quickdraw&tab=achievements">
    <img src="quickdraw.png" alt="Quickdraw" width="64" height="64">
  </a>
</div>
"""

# Fixture: no `?achievement=` hrefs, so the alt/aria-label fallback runs.
# A real badge sits inside the achievements section; a stray badge-looking
# img appears AFTER it, past the next section's landmark — the bounded
# slice must exclude it.
PROFILE_HTML_STRAY_AFTER_SECTION = """
<div class="border-top color-border-muted pt-3 mt-3 d-none d-md-block">
  <h2 class="h4 mb-2">Achievements</h2>
  <img src="yolo.png" alt="YOLO" width="64" height="64">
</div>
<div class="border-top color-border-muted pt-3 mt-3">
  <h2 class="h4 mb-2">Contribution activity</h2>
  <div class="readme-footer"><img src="s.png" alt="Starstruck"></div>
</div>
"""


class LoadTokenTests(unittest.TestCase):
    def test_cli_token_wins_over_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = os.path.join(tmp, ".env")
            with open(env_path, "w", encoding="utf-8") as fh:
                fh.write("GITHUB_TOKEN=dotenv-token\n")
            with mock.patch.object(ga, "ENV_PATH", env_path), \
                    mock.patch.dict(os.environ, {"GITHUB_TOKEN": "env-token"}):
                self.assertEqual(ga.load_token("cli-token"), "cli-token")

    def test_env_beats_dotenv(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = os.path.join(tmp, ".env")
            with open(env_path, "w", encoding="utf-8") as fh:
                fh.write("GITHUB_TOKEN=dotenv-token\n")
            with mock.patch.object(ga, "ENV_PATH", env_path), \
                    mock.patch.dict(os.environ, {"GITHUB_TOKEN": "env-token"}):
                self.assertEqual(ga.load_token(None), "env-token")

    def test_dotenv_parsing(self):
        contents = (
            "# a comment\n"
            "\n"
            "OTHER_KEY=ignore-me\n"
            "GITHUB_TOKEN=dotenv-token\n"
            "TRAILING=after\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            env_path = os.path.join(tmp, ".env")
            with open(env_path, "w", encoding="utf-8") as fh:
                fh.write(contents)
            with mock.patch.object(ga, "ENV_PATH", env_path), \
                    mock.patch.dict(os.environ):
                os.environ.pop("GITHUB_TOKEN", None)
                self.assertEqual(ga.load_token(None), "dotenv-token")

    def test_dotenv_value_is_stripped(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = os.path.join(tmp, ".env")
            with open(env_path, "w", encoding="utf-8") as fh:
                fh.write("GITHUB_TOKEN =   spaced-token   \n")
            with mock.patch.object(ga, "ENV_PATH", env_path), \
                    mock.patch.dict(os.environ):
                os.environ.pop("GITHUB_TOKEN", None)
                self.assertEqual(ga.load_token(None), "spaced-token")

    def test_missing_token_exits_with_pat_instructions(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, ".env")
            with mock.patch.object(ga, "ENV_PATH", missing), \
                    mock.patch.dict(os.environ):
                os.environ.pop("GITHUB_TOKEN", None)
                buf = io.StringIO()
                with redirect_stderr(buf):
                    with self.assertRaises(SystemExit) as cm:
                        ga.load_token(None)
                self.assertEqual(cm.exception.code, 1)
                self.assertIn(PAT_URL, buf.getvalue())


class GHErrorTests(unittest.TestCase):
    def test_attributes(self):
        err = ga.GHError(403, "slow down", 30)
        self.assertEqual(err.status, 403)
        self.assertEqual(err.message, "slow down")
        self.assertEqual(err.retry_after, 30)
        self.assertIsInstance(err, Exception)

    def test_exposed_on_client_class(self):
        self.assertIs(ga.GitHubClient.GHError, ga.GHError)


class ClientTestCase(unittest.TestCase):
    """Base class that redirects LOG_PATH to a temp dir so tests never
    write a real achievement_run.log into the repo."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log_path = os.path.join(self._tmp.name, "run.log")
        patcher = mock.patch.object(ga, "LOG_PATH", self.log_path)
        self.addCleanup(patcher.stop)
        patcher.start()

    def read_log(self) -> str:
        with open(self.log_path, encoding="utf-8") as fh:
            return fh.read()


class RestTests(ClientTestCase):
    def test_rest_builds_authed_json_request(self):
        client = ga.GitHubClient("secret-token", delay=0)
        with mock.patch("urllib.request.urlopen") as m_open:
            m_open.return_value = _json_response({"ok": True})
            result = client.rest("GET", "/user")
        self.assertEqual(result, {"ok": True})
        req = m_open.call_args[0][0]
        self.assertEqual(req.full_url, "https://api.github.com/user")
        self.assertEqual(req.get_header("Authorization"), "Bearer secret-token")
        self.assertEqual(req.get_method(), "GET")

    def test_rest_204_returns_empty_dict(self):
        client = ga.GitHubClient("tok", delay=0)
        with mock.patch("urllib.request.urlopen") as m_open:
            m_open.return_value = _make_response(b"", status=204)
            self.assertEqual(client.rest("DELETE", "/repos/o/r/git/refs/heads/b"), {})

    def test_rest_sleeps_before_mutating_call(self):
        client = ga.GitHubClient("tok", delay=2.5)
        with mock.patch("urllib.request.urlopen") as m_open, \
                mock.patch("time.sleep") as m_sleep:
            m_open.return_value = _json_response({"ok": True})
            client.rest("POST", "/user/repos", {"name": "x"})
        m_sleep.assert_any_call(2.5)

    def test_rest_error_raises_gherror(self):
        client = ga.GitHubClient("tok", delay=0)
        err = _http_error(404, {"message": "Not Found"})
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(ga.GHError) as cm:
                client.rest("GET", "/repos/o/nope")
        self.assertEqual(cm.exception.status, 404)
        self.assertIn("Not Found", cm.exception.message)


class RetryTests(ClientTestCase):
    def test_403_retry_after_retries_once_then_succeeds(self):
        client = ga.GitHubClient("tok", delay=0)
        err = _http_error(403, {"message": "secondary rate limit"},
                          headers={"Retry-After": "1"})
        ok = _json_response({"login": "me"})
        with mock.patch("urllib.request.urlopen", side_effect=[err, ok]) as m_open, \
                mock.patch("time.sleep") as m_sleep:
            result = client.rest("GET", "/user")
        self.assertEqual(result, {"login": "me"})
        self.assertEqual(m_open.call_count, 2)
        m_sleep.assert_called_once_with(1)

    def test_retryable_status_exhausts_retries_then_raises(self):
        client = ga.GitHubClient("tok", delay=0)
        err = _http_error(429, {"message": "too many"},
                          headers={"Retry-After": "1"})
        with mock.patch("urllib.request.urlopen", side_effect=[err] * 4) as m_open, \
                mock.patch("time.sleep"):
            with self.assertRaises(ga.GHError) as cm:
                client.rest("GET", "/user")
        self.assertEqual(cm.exception.status, 429)
        self.assertEqual(cm.exception.retry_after, 1)
        self.assertEqual(m_open.call_count, 4)  # initial + max 3 retries

    def test_mutating_retry_wait_is_floored_to_delay(self):
        """A retried mutating call still waits at least `delay` seconds,
        even when the Retry-After backoff computes less."""
        client = ga.GitHubClient("tok", delay=2.5)
        err = _http_error(403, {"message": "secondary rate limit"},
                          headers={"Retry-After": "1"})
        ok = _json_response({"ok": True})
        with mock.patch("urllib.request.urlopen", side_effect=[err, ok]) as m_open, \
                mock.patch("time.sleep") as m_sleep:
            result = client.rest("PATCH", "/repos/o/r/issues/1",
                                 {"state": "closed"})
        self.assertEqual(result, {"ok": True})
        self.assertEqual(m_open.call_count, 2)
        # politeness sleep, then backoff max(1s, delay) = delay
        self.assertEqual(m_sleep.call_args_list,
                         [mock.call(2.5), mock.call(2.5)])


class GraphQLTests(ClientTestCase):
    def test_errors_key_raises_gherror(self):
        client = ga.GitHubClient("tok", delay=0)
        payload = {"data": None,
                   "errors": [{"message": "Field 'x' doesn't exist"}]}
        with mock.patch("urllib.request.urlopen") as m_open, \
                mock.patch("time.sleep"):
            m_open.return_value = _json_response(payload)
            with self.assertRaises(ga.GHError) as cm:
                client.graphql("query { x }")
        self.assertIn("doesn't exist", cm.exception.message)

    def test_success_returns_data(self):
        client = ga.GitHubClient("tok", delay=0)
        payload = {"data": {"viewer": {"login": "me"}}}
        with mock.patch("urllib.request.urlopen") as m_open, \
                mock.patch("time.sleep"):
            m_open.return_value = _json_response(payload)
            result = client.graphql("query { viewer { login } }")
        self.assertEqual(result, {"viewer": {"login": "me"}})


class DryRunTests(ClientTestCase):
    def test_dry_run_makes_no_calls_and_prints_plan(self):
        client = ga.GitHubClient(token="t", dry_run=True, delay=2.5)
        buf = io.StringIO()
        with mock.patch("urllib.request.urlopen") as m_open, \
                mock.patch("time.sleep") as m_sleep, \
                redirect_stdout(buf):
            rest_result = client.rest("POST", "/x", {"a": 1})
            gql_result = client.graphql("query { viewer { login } }")
        m_open.assert_not_called()
        m_sleep.assert_not_called()
        self.assertEqual(rest_result, {})
        self.assertEqual(gql_result, {})
        log_contents = self.read_log()
        self.assertIn("DRY-RUN POST /x", log_contents)
        self.assertIn("DRY-RUN POST /x", buf.getvalue())
        self.assertIn("DRY-RUN POST /graphql", buf.getvalue())


class LogTests(unittest.TestCase):
    def test_log_appends_timestamped_line_without_token(self):
        token = "ghp_SUPERSECRET123"
        client = ga.GitHubClient(token, delay=0)
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, "run.log")
            with mock.patch.object(ga, "LOG_PATH", log_path):
                client.log(f"GET /user token={token} -> 200")
                client.log("plain message")
            with open(log_path, encoding="utf-8") as fh:
                contents = fh.read()
        self.assertNotIn(token, contents)
        self.assertIn("plain message", contents)
        # ISO-8601-ish timestamp prefix
        self.assertRegex(contents.splitlines()[0],
                         r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

    def test_log_scrubs_token_in_rest_request_lines(self):
        token = "abc123token"
        client = ga.GitHubClient(token, delay=0)
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, "run.log")
            with mock.patch.object(ga, "LOG_PATH", log_path), \
                    mock.patch("urllib.request.urlopen") as m_open:
                m_open.return_value = _json_response({"login": "me"})
                client.rest("GET", "/user")
            with open(log_path, encoding="utf-8") as fh:
                contents = fh.read()
        self.assertNotIn(token, contents)
        self.assertIn("GET /user", contents)
        self.assertIn("-> 200", contents)


class ParseProfileBadgesTests(unittest.TestCase):
    def test_extracts_three_badges_in_document_order(self):
        self.assertEqual(
            ga.parse_profile_badges(PROFILE_HTML_WITH_BADGES),
            ["Quickdraw", "Pull Shark", "YOLO"],
        )

    def test_empty_profile_returns_empty_list(self):
        self.assertEqual(ga.parse_profile_badges(PROFILE_HTML_NO_BADGES), [])

    def test_empty_input_returns_empty_list(self):
        self.assertEqual(ga.parse_profile_badges(""), [])

    def test_ignores_unknown_alt_and_aria_labels(self):
        html = ('<h2>Achievements</h2>'
                '<img src="a.png" alt="Not A Badge">'
                '<a aria-label="Sponsor octocat" href="#">x</a>'
                '<img src="b.png" alt="Starstruck">')
        self.assertEqual(ga.parse_profile_badges(html), ["Starstruck"])

    def test_stray_readme_img_is_not_a_false_positive(self):
        self.assertEqual(
            ga.parse_profile_badges(PROFILE_HTML_STRAY_README_IMG),
            ["Quickdraw"],
        )

    def test_fallback_slice_stops_at_next_section(self):
        self.assertEqual(
            ga.parse_profile_badges(PROFILE_HTML_STRAY_AFTER_SECTION),
            ["YOLO"],
        )

    def test_no_achievements_section_never_scans_document(self):
        html = ('<div class="readme">'
                '<img src="i.png" alt="Pull Shark"></div>')
        self.assertEqual(ga.parse_profile_badges(html), [])

    def test_tier_suffix_and_casing_match_allowlist(self):
        html = ('<h2>Achievements</h2>'
                '<img src="a.png" alt="Pull Shark x3">'
                '<a aria-label="quickdraw" href="#">x</a>')
        self.assertEqual(ga.parse_profile_badges(html),
                         ["Pull Shark", "Quickdraw"])


class DoctorTests(ClientTestCase):
    def test_401_prints_pat_fallback_and_exits_1(self):
        client = ga.GitHubClient("bad-token", delay=0)
        with mock.patch.object(
                client, "rest",
                side_effect=ga.GHError(401, "Bad credentials")):
            buf = io.StringIO()
            with redirect_stderr(buf):
                with self.assertRaises(SystemExit) as cm:
                    ga.cmd_doctor(client)
        self.assertEqual(cm.exception.code, 1)
        self.assertIn(PAT_URL, buf.getvalue())
        self.assertNotIn("bad-token", buf.getvalue())

    def test_success_prints_login_and_plan(self):
        client = ga.GitHubClient("tok", delay=0)
        payload = {"login": "octocat", "plan": {"name": "free"}}
        with mock.patch.object(client, "rest", return_value=payload):
            buf = io.StringIO()
            with redirect_stdout(buf):
                ga.cmd_doctor(client)
        out = buf.getvalue()
        self.assertIn("octocat", out)
        self.assertIn("free", out)


class StatusTests(ClientTestCase):
    def test_fetches_profile_unauthenticated_and_prints_table(self):
        client = ga.GitHubClient("sekrit-token", delay=0)
        with mock.patch.object(
                client, "rest", return_value={"login": "octocat"}), \
                mock.patch("urllib.request.urlopen") as m_open:
            m_open.return_value = _make_response(
                PROFILE_HTML_WITH_BADGES.encode("utf-8"))
            buf = io.StringIO()
            with redirect_stdout(buf):
                ga.cmd_status(client)
        req = m_open.call_args[0][0]
        self.assertEqual(req.full_url, "https://github.com/octocat")
        self.assertIsNone(req.get_header("Authorization"))
        out = buf.getvalue()
        # all 9 earnable badges listed in the target/earned table
        for name in ("Quickdraw", "Pull Shark", "Galaxy Brain", "YOLO",
                     "Pair Extraordinaire", "Starstruck", "Public Sponsor",
                     "Heart On Your Sleeve", "Open Sourcerer"):
            self.assertIn(name, out)
        # earned/unearned rows are marked per the scraped profile
        self.assertRegex(out, r"Quickdraw.*yes")
        self.assertRegex(out, r"Galaxy Brain.*no")
        # retired event badges are not part of the earnable table
        self.assertNotIn("Arctic Code Vault", out)
        self.assertNotIn("Mars 2020", out)


class PublishTests(ClientTestCase):
    """cmd_publish: create-or-reuse the playground repo, add origin, push."""

    def _completed(self, stdout="", stderr="", rc=0):
        return subprocess.CompletedProcess(
            args=["git"], returncode=rc, stdout=stdout, stderr=stderr)

    def _git_commands(self, m_run):
        return [call.args[0] for call in m_run.call_args_list]

    def test_existing_repo_skips_post_and_pushes(self):
        client = ga.GitHubClient("tok", delay=0)
        posts = []

        def fake_rest(method, path, body=None):
            if method == "POST":
                posts.append((path, body))
            if path == "/user":
                return {"login": "octocat"}
            if path == "/repos/octocat/achievement":
                return {"name": "achievement",
                        "full_name": "octocat/achievement",
                        "html_url": "https://github.com/octocat/achievement"}
            raise AssertionError(f"unexpected call {method} {path}")

        with mock.patch.object(client, "rest", side_effect=fake_rest), \
                mock.patch("subprocess.run") as m_run:
            m_run.return_value = self._completed(
                stdout="Everything up-to-date")
            buf = io.StringIO()
            with redirect_stdout(buf):
                result = ga.cmd_publish(client, "achievement")
        # repo already exists -> no POST /user/repos (idempotent)
        self.assertEqual(posts, [])
        commands = self._git_commands(m_run)
        self.assertIn(["git", "remote", "add", "origin",
                       "https://github.com/octocat/achievement.git"],
                      commands)
        self.assertIn(["git", "push", "-u", "origin", "main"], commands)
        # git was invoked non-fatally with captured output
        for call in m_run.call_args_list:
            self.assertEqual(call.kwargs.get("check"), False)
            self.assertTrue(call.kwargs.get("capture_output"))
        self.assertFalse(result["created"])
        self.assertTrue(result["pushed"])
        self.assertEqual(result["full_name"], "octocat/achievement")
        self.assertEqual(result["html_url"],
                         "https://github.com/octocat/achievement")
        # git stdout is echoed and recorded in the activity log
        self.assertIn("Everything up-to-date", buf.getvalue())
        self.assertIn("Everything up-to-date", self.read_log())

    def test_missing_repo_creates_then_pushes(self):
        client = ga.GitHubClient("tok", delay=0)

        def fake_rest(method, path, body=None):
            if path == "/user":
                return {"login": "octocat"}
            if method == "GET" and path == "/repos/octocat/achievement":
                raise ga.GHError(404, "Not Found")
            if method == "POST" and path == "/user/repos":
                return {"name": "achievement",
                        "full_name": "octocat/achievement",
                        "html_url": "https://github.com/octocat/achievement"}
            raise AssertionError(f"unexpected call {method} {path}")

        with mock.patch.object(client, "rest", side_effect=fake_rest) as m_rest, \
                mock.patch("subprocess.run") as m_run:
            m_run.return_value = self._completed(stdout="branch 'main' set up")
            with redirect_stdout(io.StringIO()):
                result = ga.cmd_publish(client, "achievement")
        post_calls = [c for c in m_rest.call_args_list
                      if c.args[0] == "POST"]
        self.assertEqual(len(post_calls), 1)
        self.assertEqual(post_calls[0].args[1], "/user/repos")
        self.assertEqual(post_calls[0].args[2],
                         {"name": "achievement",
                          "private": False,
                          "auto_init": False})
        commands = self._git_commands(m_run)
        # remote add runs before the push
        self.assertEqual(commands[0],
                         ["git", "remote", "add", "origin",
                          "https://github.com/octocat/achievement.git"])
        self.assertEqual(commands[-1],
                         ["git", "push", "-u", "origin", "main"])
        self.assertTrue(result["created"])
        self.assertTrue(result["pushed"])
        self.assertEqual(result["remote_url"],
                         "https://github.com/octocat/achievement.git")

    def test_repo_lookup_non404_error_propagates(self):
        client = ga.GitHubClient("tok", delay=0)

        def fake_rest(method, path, body=None):
            if path == "/user":
                return {"login": "octocat"}
            raise ga.GHError(500, "server exploded")

        with mock.patch.object(client, "rest", side_effect=fake_rest), \
                mock.patch("subprocess.run") as m_run:
            with self.assertRaises(ga.GHError) as cm:
                ga.cmd_publish(client, "achievement")
        self.assertEqual(cm.exception.status, 500)
        m_run.assert_not_called()

    def test_existing_fork_rejected_without_writes(self):
        """An existing repo that is a fork is rejected — the playground
        must be standalone; no POST and no git push may happen."""
        client = ga.GitHubClient("tok", delay=0)
        posts = []

        def fake_rest(method, path, body=None):
            if method == "POST":
                posts.append((path, body))
            if path == "/user":
                return {"login": "octocat"}
            if path == "/repos/octocat/achievement":
                return {"name": "achievement", "fork": True,
                        "full_name": "octocat/achievement"}
            raise AssertionError(f"unexpected call {method} {path}")

        with mock.patch.object(client, "rest", side_effect=fake_rest), \
                mock.patch("subprocess.run") as m_run:
            with self.assertRaises(ga.GHError) as cm:
                ga.cmd_publish(client, "achievement")
        self.assertIn("fork", str(cm.exception))
        self.assertIn("standalone", str(cm.exception))
        self.assertEqual(posts, [])
        m_run.assert_not_called()

    def test_existing_origin_falls_back_to_set_url(self):
        """Re-running publish when origin is already configured: the failed
        `git remote add` is tolerated and `git remote set-url` repoints it."""
        client = ga.GitHubClient("tok", delay=0)

        def fake_rest(method, path, body=None):
            if path == "/user":
                return {"login": "octocat"}
            return {"name": "achievement"}

        def fake_run(cmd, **kwargs):
            if cmd[1:3] == ["remote", "add"]:
                return self._completed(
                    rc=3, stderr="error: remote origin already exists.")
            return self._completed(stdout="ok")

        with mock.patch.object(client, "rest", side_effect=fake_rest), \
                mock.patch("subprocess.run", side_effect=fake_run) as m_run:
            with redirect_stdout(io.StringIO()), \
                    redirect_stderr(io.StringIO()):
                result = ga.cmd_publish(client, "achievement")
        commands = self._git_commands(m_run)
        self.assertIn(["git", "remote", "set-url", "origin",
                       "https://github.com/octocat/achievement.git"],
                      commands)
        self.assertIn(["git", "push", "-u", "origin", "main"], commands)
        self.assertTrue(result["pushed"])

    def test_dry_run_prints_plan_without_running_git(self):
        client = ga.GitHubClient("tok", dry_run=True, delay=0)
        buf = io.StringIO()
        with mock.patch("subprocess.run") as m_run, redirect_stdout(buf):
            result = ga.cmd_publish(client, "achievement")
        m_run.assert_not_called()
        out = buf.getvalue()
        self.assertIn("DRY-RUN", out)
        self.assertIn("git push -u origin main", out)
        # dry-run cannot know whether the repo exists (GET returns {}), so
        # the plan shows the conditional create instead of asserting state
        self.assertIn("DRY-RUN (if 404) POST /user/repos", out)
        self.assertIn('"private": false', out)
        self.assertIn('"auto_init": false', out)
        self.assertNotIn("already exists", out)
        self.assertFalse(result["created"])


class CountMergedPrsTests(ClientTestCase):
    """count_merged_prs: closed PRs with non-null merged_at authored by
    the authenticated user."""

    PULLS_PATH = "/repos/octocat/achievement/pulls?state=closed&per_page=100"

    def _pulls(self, *entries):
        return [{"number": i + 1, "merged_at": merged,
                 "user": {"login": login}}
                for i, (merged, login) in enumerate(entries)]

    def test_two_merged_of_three_closed_returns_two(self):
        client = ga.GitHubClient("tok", delay=0)
        pulls = self._pulls(
            ("2026-01-01T00:00:00Z", "octocat"),
            (None, "octocat"),
            ("2026-01-02T00:00:00Z", "octocat"),
        )

        def fake_rest(method, path, body=None):
            if path == "/user":
                return {"login": "octocat"}
            if path == self.PULLS_PATH:
                return pulls
            raise AssertionError(f"unexpected call {method} {path}")

        with mock.patch.object(client, "rest", side_effect=fake_rest):
            result = ga.count_merged_prs(client, "octocat", "achievement")
        self.assertEqual(result, 2)

    def test_merged_prs_by_other_authors_do_not_count(self):
        client = ga.GitHubClient("tok", delay=0)
        pulls = self._pulls(
            ("2026-01-01T00:00:00Z", "octocat"),
            ("2026-01-02T00:00:00Z", "someone-else"),
        )

        def fake_rest(method, path, body=None):
            if path == "/user":
                return {"login": "octocat"}
            if path == self.PULLS_PATH:
                return pulls
            raise AssertionError(f"unexpected call {method} {path}")

        with mock.patch.object(client, "rest", side_effect=fake_rest):
            result = ga.count_merged_prs(client, "octocat", "achievement")
        self.assertEqual(result, 1)

    def test_explicit_login_skips_user_fetch(self):
        """Passing login avoids the extra GET /user (fetch once, reuse)."""
        client = ga.GitHubClient("tok", delay=0)
        calls = []

        def fake_rest(method, path, body=None):
            calls.append(path)
            if path == self.PULLS_PATH:
                return self._pulls(("x", "octocat"), (None, "octocat"))
            raise AssertionError(f"unexpected call {method} {path}")

        with mock.patch.object(client, "rest", side_effect=fake_rest):
            result = ga.count_merged_prs(client, "octocat", "achievement",
                                         login="octocat")
        self.assertEqual(result, 1)
        self.assertEqual(calls, [self.PULLS_PATH])

    def test_empty_or_nonlist_response_returns_zero(self):
        """Dry-run REST returns {}; anything that is not a list counts 0."""
        client = ga.GitHubClient("tok", dry_run=True, delay=0)
        with redirect_stdout(io.StringIO()):
            result = ga.count_merged_prs(client, "o", "r", login="o")
        self.assertEqual(result, 0)


class PrCycleTests(ClientTestCase):
    """pr_cycle: branch -> commit -> PR -> merge -> branch delete."""

    def _fake_rest(self, calls, delete_fails=False):
        def fake_rest(method, path, body=None):
            calls.append((method, path, body))
            if method == "GET" and path == "/repos/o/r":
                return {"default_branch": "main"}
            if method == "GET" and path == "/repos/o/r/git/refs/heads/main":
                return {"object": {"sha": "abc123"}}
            if method == "POST" and path == "/repos/o/r/pulls":
                return {"number": 42}
            if method == "DELETE" and delete_fails:
                raise ga.GHError(422, "Reference does not exist")
            return {}
        return fake_rest

    def test_five_mutating_calls_in_order_and_returns_pr_number(self):
        client = ga.GitHubClient("tok", delay=0)
        calls = []
        with mock.patch.object(client, "rest",
                               side_effect=self._fake_rest(calls)):
            number = ga.pr_cycle(client, "o", "r", "pull-shark-1",
                                 "Pull Shark PR 1/16")
        self.assertEqual(number, 42)
        mutating = [(m, p) for m, p, _ in calls if m != "GET"]
        self.assertEqual([m for m, _ in mutating],
                         ["POST", "PUT", "POST", "PUT", "DELETE"])
        self.assertEqual(mutating[0], ("POST", "/repos/o/r/git/refs"))
        self.assertTrue(mutating[1][1].startswith(
            "/repos/o/r/contents/notes/pull-shark-1"))
        self.assertTrue(mutating[1][1].endswith(".md"))
        self.assertEqual(mutating[2], ("POST", "/repos/o/r/pulls"))
        self.assertEqual(mutating[3], ("PUT", "/repos/o/r/pulls/42/merge"))
        self.assertTrue(mutating[4][1].startswith(
            "/repos/o/r/git/refs/heads/shark-pr-"))

    def test_request_bodies_match_api_contract(self):
        client = ga.GitHubClient("tok", delay=0)
        calls = []
        with mock.patch.object(client, "rest",
                               side_effect=self._fake_rest(calls)):
            ga.pr_cycle(client, "o", "r", "yolo", "YOLO: no review")

        def body_for(method, path_pred):
            return next(b for m, p, b in calls
                        if m == method and path_pred(p))

        ref_body = body_for("POST", lambda p: p == "/repos/o/r/git/refs")
        self.assertEqual(ref_body["sha"], "abc123")
        self.assertTrue(ref_body["ref"].startswith("refs/heads/shark-pr-"))
        branch = ref_body["ref"].removeprefix("refs/heads/")

        content_body = body_for("PUT", lambda p: "/contents/notes/" in p)
        self.assertEqual(content_body["message"], "YOLO: no review")
        self.assertEqual(content_body["branch"], branch)
        decoded = base64.b64decode(content_body["content"]).decode("utf-8")
        self.assertIn("YOLO: no review", decoded)

        pulls_body = body_for("POST", lambda p: p == "/repos/o/r/pulls")
        self.assertEqual(pulls_body["title"], "YOLO: no review")
        self.assertEqual(pulls_body["head"], branch)
        self.assertEqual(pulls_body["base"], "main")
        self.assertIn("body", pulls_body)

        merge_body = body_for("PUT", lambda p: p.endswith("/merge"))
        self.assertEqual(merge_body, {"merge_method": "merge"})

    def test_branch_delete_failure_is_tolerated(self):
        client = ga.GitHubClient("tok", delay=0)
        calls = []
        with mock.patch.object(
                client, "rest",
                side_effect=self._fake_rest(calls, delete_fails=True)), \
                redirect_stderr(io.StringIO()):
            number = ga.pr_cycle(client, "o", "r", "x", "msg")
        self.assertEqual(number, 42)
        self.assertIn("delete", self.read_log())

    def test_branches_are_unique_per_cycle(self):
        client = ga.GitHubClient("tok", delay=0)
        calls = []
        with mock.patch.object(client, "rest",
                               side_effect=self._fake_rest(calls)):
            ga.pr_cycle(client, "o", "r", "pull-shark", "a")
            ga.pr_cycle(client, "o", "r", "pull-shark", "b")
        refs = [b["ref"] for m, p, b in calls
                if m == "POST" and p == "/repos/o/r/git/refs"]
        self.assertEqual(len(set(refs)), 2)


class QuickdrawTests(ClientTestCase):
    """badge_quickdraw: open an issue, close it immediately."""

    def test_opens_then_closes_issue_immediately(self):
        client = ga.GitHubClient("tok", delay=0)
        calls = []

        def fake_rest(method, path, body=None):
            calls.append((method, path, body))
            if method == "POST" and path == "/repos/o/r/issues":
                return {"number": 9}
            return {}

        with mock.patch.object(client, "rest", side_effect=fake_rest), \
                redirect_stdout(io.StringIO()):
            result = ga.badge_quickdraw(client, "o", "r")
        self.assertTrue(result)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], "POST")
        self.assertEqual(calls[0][1], "/repos/o/r/issues")
        self.assertIn("Quickdraw", calls[0][2]["title"])
        self.assertEqual(calls[1], ("PATCH", "/repos/o/r/issues/9",
                                    {"state": "closed"}))

    def test_api_failure_returns_false(self):
        client = ga.GitHubClient("tok", delay=0)
        with mock.patch.object(
                client, "rest",
                side_effect=ga.GHError(403, "forbidden")), \
                redirect_stderr(io.StringIO()):
            self.assertFalse(ga.badge_quickdraw(client, "o", "r"))


class YoloTests(ClientTestCase):
    """badge_yolo: a single pr_cycle merged with no review requested."""

    def test_runs_one_pr_cycle_and_returns_true(self):
        client = ga.GitHubClient("tok", delay=0)
        with mock.patch.object(ga, "pr_cycle", return_value=42) as m_cycle, \
                redirect_stdout(io.StringIO()):
            result = ga.badge_yolo(client, "o", "r")
        self.assertTrue(result)
        m_cycle.assert_called_once()
        args = m_cycle.call_args.args
        self.assertEqual(args[:3], (client, "o", "r"))
        self.assertIn("yolo", args[3])

    def test_pr_cycle_failure_returns_false(self):
        client = ga.GitHubClient("tok", delay=0)
        with mock.patch.object(
                ga, "pr_cycle",
                side_effect=ga.GHError(422, "merge failed")), \
                redirect_stderr(io.StringIO()):
            self.assertFalse(ga.badge_yolo(client, "o", "r"))


class PullSharkTests(ClientTestCase):
    """badge_pull_shark: idempotent — only runs the cycles still needed."""

    PULLS_PATH = "/repos/octocat/achievement/pulls?state=closed&per_page=100"

    def _fake_rest(self, merged_count):
        pulls = [{"number": i + 1, "merged_at": "x" if i < merged_count
                  else None, "user": {"login": "octocat"}}
                 for i in range(merged_count + 1)]

        def fake_rest(method, path, body=None):
            if path == "/user":
                return {"login": "octocat"}
            if path == self.PULLS_PATH:
                return pulls
            raise AssertionError(f"unexpected call {method} {path}")
        return fake_rest

    def test_two_merged_target_sixteen_runs_fourteen_cycles(self):
        client = ga.GitHubClient("tok", delay=0)
        with mock.patch.object(client, "rest",
                               side_effect=self._fake_rest(2)), \
                mock.patch.object(ga, "pr_cycle", return_value=100) as m_cycle, \
                redirect_stdout(io.StringIO()):
            result = ga.badge_pull_shark(client, "octocat", "achievement", 16)
        self.assertEqual(m_cycle.call_count, 14)
        self.assertEqual(result, 14)
        # every cycle is labelled as a pull-shark PR on the same repo
        for call in m_cycle.call_args_list:
            self.assertEqual(call.args[1:3], ("octocat", "achievement"))
            self.assertIn("pull-shark", call.args[3])

    def test_already_at_target_runs_zero_cycles(self):
        client = ga.GitHubClient("tok", delay=0)
        with mock.patch.object(client, "rest",
                               side_effect=self._fake_rest(16)), \
                mock.patch.object(ga, "pr_cycle") as m_cycle, \
                redirect_stdout(io.StringIO()):
            result = ga.badge_pull_shark(client, "octocat", "achievement", 16)
        m_cycle.assert_not_called()
        self.assertEqual(result, 0)


class GalaxyBrainTests(ClientTestCase):
    """badge_galaxy_brain: enable Discussions, pick (or create) an
    answerable category, then post self-answered Q&A discussions until
    `target` accepted answers exist."""

    def _harness(self, categories=None, discussions=None, can_mark=True):
        """Fake rest/graphql dispatchers over canned repo data.

        Returns (calls, fake_rest, fake_graphql). `calls` records tuples:
        ("rest", method, path, body) and ("graphql", query, variables).
        GraphQL replies are shaped like the real API payloads.
        """
        calls = []
        repo_data = {
            "id": "R_play",
            "discussionCategories": {"nodes": categories or []},
            "discussions": {"nodes": discussions or []},
        }
        seq = {"n": 0}

        def fake_rest(method, path, body=None):
            calls.append(("rest", method, path, body))
            if path == "/user":
                return {"login": "octocat"}
            return {}

        def fake_graphql(query, variables=None):
            calls.append(("graphql", query, variables))
            if "createDiscussionCategory" in query:
                return {"createDiscussionCategory":
                        {"discussionCategory": {"id": "C_new"}}}
            if "createDiscussion" in query:
                seq["n"] += 1
                return {"createDiscussion": {"discussion": {
                    "id": f"D_{seq['n']}", "number": seq["n"]}}}
            if "addDiscussionComment" in query:
                return {"addDiscussionComment": {"comment": {
                    "id": f"CM_{seq['n']}",
                    "viewerCanMarkAsAnswer": can_mark}}}
            if "markDiscussionCommentAsAnswer" in query:
                return {"markDiscussionCommentAsAnswer":
                        {"comment": {"id": "CM", "isAnswer": True}}}
            return {"repository": repo_data}
        return calls, fake_rest, fake_graphql

    def _run(self, client, fake_rest, fake_graphql, target):
        buf = io.StringIO()
        with mock.patch.object(client, "rest", side_effect=fake_rest), \
                mock.patch.object(client, "graphql",
                                  side_effect=fake_graphql), \
                redirect_stdout(buf):
            result = ga.badge_galaxy_brain(client, "o", "r", target)
        return result, buf.getvalue()

    def _queries(self, calls):
        return [c[1] for c in calls if c[0] == "graphql"]

    def test_enables_discussions_with_has_discussions_patch(self):
        client = ga.GitHubClient("tok", delay=0)
        calls, fr, fg = self._harness(
            categories=[{"id": "C_qa", "name": "Q&A",
                         "isAnswerable": True}])
        result, _ = self._run(client, fr, fg, target=1)
        self.assertEqual(result, 1)
        patches = [c for c in calls if c[0] == "rest" and c[1] == "PATCH"]
        self.assertEqual(patches, [("rest", "PATCH", "/repos/o/r",
                                    {"has_discussions": True})])

    def test_selects_existing_answerable_category(self):
        client = ga.GitHubClient("tok", delay=0)
        calls, fr, fg = self._harness(categories=[
            {"id": "C_gen", "name": "General", "isAnswerable": False},
            {"id": "C_qa", "name": "Q&A", "isAnswerable": True},
        ])
        self._run(client, fr, fg, target=1)
        queries = self._queries(calls)
        # an answerable category already exists -> no creation mutation
        self.assertFalse(any("createDiscussionCategory" in q
                             for q in queries))
        create_vars = [c[2] for c in calls
                       if c[0] == "graphql" and "createDiscussion(" in c[1]]
        self.assertTrue(create_vars)
        for v in create_vars:
            self.assertEqual(v["repoId"], "R_play")
            self.assertEqual(v["categoryId"], "C_qa")

    def test_creates_qa_category_when_none_answerable(self):
        client = ga.GitHubClient("tok", delay=0)
        calls, fr, fg = self._harness(categories=[
            {"id": "C_gen", "name": "General", "isAnswerable": False},
        ])
        result, _ = self._run(client, fr, fg, target=1)
        self.assertEqual(result, 1)
        cat_queries = [q for q in self._queries(calls)
                       if "createDiscussionCategory" in q]
        self.assertEqual(len(cat_queries), 1)
        self.assertIn("Q&A", cat_queries[0])
        self.assertIn("QUESTIONS_ANSWERS", cat_queries[0])
        cat_vars = [c[2] for c in calls
                    if c[0] == "graphql"
                    and "createDiscussionCategory" in c[1]]
        self.assertEqual(cat_vars, [{"repoId": "R_play"}])
        # the new category id feeds the createDiscussion input
        create_vars = [c[2] for c in calls
                       if c[0] == "graphql" and "createDiscussion(" in c[1]]
        self.assertEqual(create_vars[0]["categoryId"], "C_new")

    def test_each_answer_creates_comments_and_marks_in_order(self):
        client = ga.GitHubClient("tok", delay=0)
        calls, fr, fg = self._harness(
            categories=[{"id": "C_qa", "name": "Q&A",
                         "isAnswerable": True}])
        result, _ = self._run(client, fr, fg, target=2)
        self.assertEqual(result, 2)
        mutating = [q for q in self._queries(calls)
                    if q.lstrip().startswith("mutation")]
        # create -> comment -> mark, once per answer
        self.assertEqual(len(mutating), 6)
        self.assertEqual(
            ["createDiscussion(" in q for q in mutating],
            [True, False, False, True, False, False])
        self.assertEqual(
            ["addDiscussionComment" in q for q in mutating],
            [False, True, False, False, True, False])
        # markDiscussionCommentAsAnswer takes the COMMENT node id
        mark_vars = [c[2] for c in calls if c[0] == "graphql"
                     and "markDiscussionCommentAsAnswer" in c[1]]
        self.assertEqual(mark_vars, [{"commentId": "CM_1"},
                                     {"commentId": "CM_2"}])

    def test_prior_accepted_answers_count_toward_target(self):
        client = ga.GitHubClient("tok", delay=0)
        discussions = [
            {"answer": {"author": {"login": "octocat"}}},
            {"answer": {"author": {"login": "octocat"}}},
            {"answer": {"author": {"login": "someone-else"}}},
            {"answer": None},
            {},
        ]
        calls, fr, fg = self._harness(
            categories=[{"id": "C_qa", "name": "Q&A",
                         "isAnswerable": True}],
            discussions=discussions)
        result, _ = self._run(client, fr, fg, target=4)
        # 2 of the 4 answers already exist -> only 2 cycles run
        self.assertEqual(result, 2)
        queries = self._queries(calls)
        self.assertEqual(sum("createDiscussion(" in q for q in queries), 2)
        self.assertEqual(
            sum("markDiscussionCommentAsAnswer" in q for q in queries), 2)

    def test_already_at_target_creates_nothing(self):
        client = ga.GitHubClient("tok", delay=0)
        discussions = [{"answer": {"author": {"login": "octocat"}}}
                       for _ in range(8)]
        calls, fr, fg = self._harness(
            categories=[{"id": "C_qa", "name": "Q&A",
                         "isAnswerable": True}],
            discussions=discussions)
        result, out = self._run(client, fr, fg, target=8)
        self.assertEqual(result, 0)
        queries = self._queries(calls)
        self.assertFalse(any("createDiscussion(" in q for q in queries))
        self.assertIn("8/8", out)

    def test_self_mark_disallowed_returns_minus_one_and_stops(self):
        """viewerCanMarkAsAnswer=false -> stop the badge: return -1,
        print the fallback note, and never run markDiscussionCommentAsAnswer
        or a second discussion."""
        client = ga.GitHubClient("tok", delay=0)
        calls, fr, fg = self._harness(
            categories=[{"id": "C_qa", "name": "Q&A",
                         "isAnswerable": True}],
            can_mark=False)
        result, out = self._run(client, fr, fg, target=3)
        self.assertEqual(result, -1)
        self.assertIn("fallback", out.lower())
        queries = self._queries(calls)
        self.assertFalse(any("markDiscussionCommentAsAnswer" in q
                             for q in queries))
        self.assertEqual(sum("createDiscussion(" in q for q in queries), 1)
        self.assertEqual(sum("addDiscussionComment" in q for q in queries),
                         1)

    def test_dry_run_prints_plan_and_returns_zero(self):
        """rest/graphql return {} in dry-run; the badge tolerates the
        empty shapes, prints its planned steps, and returns 0."""
        client = ga.GitHubClient("tok", dry_run=True, delay=0)
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = ga.badge_galaxy_brain(client, "o", "r", 8)
        self.assertEqual(result, 0)
        out = buf.getvalue()
        self.assertIn("DRY-RUN PATCH /repos/o/r", out)
        self.assertIn('"has_discussions": true', out)
        self.assertIn("8", out)
        self.assertIn("discussion", out)


class ParseCoauthorTests(unittest.TestCase):
    """parse_coauthor: 'Name <email>' -> (name, email); anything missing
    the email or the angle brackets raises ValueError with a usage hint."""

    def test_valid_name_email(self):
        self.assertEqual(ga.parse_coauthor("Ada <ada@x.io>"),
                         ("Ada", "ada@x.io"))

    def test_name_with_spaces(self):
        self.assertEqual(ga.parse_coauthor("Ada Lovelace <ada@x.io>"),
                         ("Ada Lovelace", "ada@x.io"))

    def test_missing_email_or_brackets_raise_with_usage(self):
        for raw in ("Ada", "Ada ada@x.io"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as cm:
                    ga.parse_coauthor(raw)
                self.assertIn('--coauthor "Name <email>"',
                              str(cm.exception))

    def test_empty_name_or_email_raise_with_usage(self):
        for raw in ("<ada@x.io>", "Ada <>", ""):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as cm:
                    ga.parse_coauthor(raw)
                self.assertIn('--coauthor "Name <email>"',
                              str(cm.exception))


class PairExtraordinaireTests(ClientTestCase):
    """badge_pair_extraordinaire: pr_cycles whose commit message ends with
    a Co-authored-by trailer; idempotent via merged PRs that already
    carry the trailer on any of their commits."""

    PULLS_PATH = "/repos/octocat/achievement/pulls?state=closed&per_page=100"
    COMMITS_PREFIX = "/repos/octocat/achievement/pulls/"
    COMMITS_SUFFIX = "/commits?per_page=10"
    COAUTHOR = "Ada Lovelace <ada@x.io>"
    TRAILER = "Co-authored-by: Ada Lovelace <ada@x.io>"

    def _fake_rest(self, coauthored: int, plain: int = 0):
        """`coauthored` merged PRs whose commits contain the trailer, plus
        `plain` merged PRs without it; unmerged PRs are ignored anyway."""
        pulls = []
        for i in range(coauthored + plain):
            pulls.append({"number": i + 1, "merged_at": "x",
                          "user": {"login": "octocat"}})
        pulls.append({"number": coauthored + plain + 1,
                      "merged_at": None,   # closed but unmerged
                      "user": {"login": "octocat"}})
        pulls.append({"number": coauthored + plain + 2,
                      "merged_at": "x",    # merged by someone else
                      "user": {"login": "someone-else"}})

        def fake_rest(method, path, body=None):
            if path == "/user":
                return {"login": "octocat"}
            if path == self.PULLS_PATH:
                return pulls
            if (path.startswith(self.COMMITS_PREFIX)
                    and path.endswith(self.COMMITS_SUFFIX)):
                number = int(path[len(self.COMMITS_PREFIX):]
                             .split("/")[0])
                if number <= coauthored:
                    return [{"commit": {"message":
                                        "note\n\n" + self.TRAILER}},
                            {"commit": {"message": "second commit"}}]
                return [{"commit": {"message": "plain commit"}}]
            raise AssertionError(f"unexpected call {method} {path}")
        return fake_rest

    def _commits_calls(self, m_rest):
        return [c for c in m_rest.call_args_list
                if c.args[1].startswith(self.COMMITS_PREFIX)
                and c.args[1].endswith(self.COMMITS_SUFFIX)]

    def test_runs_cycles_with_trailer_at_end_of_message(self):
        client = ga.GitHubClient("tok", delay=0)
        with mock.patch.object(client, "rest",
                               side_effect=self._fake_rest(0)), \
                mock.patch.object(ga, "pr_cycle",
                                  return_value=42) as m_cycle, \
                redirect_stdout(io.StringIO()):
            result = ga.badge_pair_extraordinaire(
                client, "octocat", "achievement", self.COAUTHOR, 10)
        self.assertEqual(result, 10)
        self.assertEqual(m_cycle.call_count, 10)
        for call in m_cycle.call_args_list:
            self.assertEqual(call.args[1:3], ("octocat", "achievement"))
            message = call.args[4]
            self.assertTrue(message.endswith("\n\n" + self.TRAILER),
                            msg=f"message lacks trailer: {message!r}")

    def test_target_already_reached_runs_zero_cycles(self):
        client = ga.GitHubClient("tok", delay=0)
        with mock.patch.object(client, "rest",
                               side_effect=self._fake_rest(10)), \
                mock.patch.object(ga, "pr_cycle") as m_cycle, \
                redirect_stdout(io.StringIO()):
            result = ga.badge_pair_extraordinaire(
                client, "octocat", "achievement", self.COAUTHOR, 10)
        self.assertEqual(result, 0)
        m_cycle.assert_not_called()

    def test_plain_merged_prs_do_not_count_toward_progress(self):
        """3 co-authored + 4 plain merged PRs -> only 3 count; the badge
        runs 7 more cycles for target 10."""
        client = ga.GitHubClient("tok", delay=0)
        with mock.patch.object(client, "rest",
                               side_effect=self._fake_rest(3, plain=4)), \
                mock.patch.object(ga, "pr_cycle",
                                  return_value=42) as m_cycle, \
                redirect_stdout(io.StringIO()):
            result = ga.badge_pair_extraordinaire(
                client, "octocat", "achievement", self.COAUTHOR, 10)
        self.assertEqual(result, 7)
        self.assertEqual(m_cycle.call_count, 7)

    def test_commit_scan_is_bounded_to_50_merged_prs(self):
        """Only the first 50 merged PRs get their commits fetched."""
        client = ga.GitHubClient("tok", delay=0)
        with mock.patch.object(client, "rest",
                               side_effect=self._fake_rest(60)) as m_rest, \
                mock.patch.object(ga, "pr_cycle") as m_cycle, \
                redirect_stdout(io.StringIO()):
            result = ga.badge_pair_extraordinaire(
                client, "octocat", "achievement", self.COAUTHOR, 10)
        self.assertEqual(result, 0)
        m_cycle.assert_not_called()
        self.assertEqual(len(self._commits_calls(m_rest)), 50)

    def test_invalid_coauthor_fails_before_any_api_call(self):
        client = ga.GitHubClient("tok", delay=0)
        with mock.patch.object(client, "rest") as m_rest, \
                mock.patch.object(ga, "pr_cycle") as m_cycle:
            with self.assertRaises(ValueError) as cm:
                ga.badge_pair_extraordinaire(
                    client, "octocat", "achievement", "Ada", 10)
        self.assertIn('--coauthor "Name <email>"', str(cm.exception))
        m_rest.assert_not_called()
        m_cycle.assert_not_called()


class InterfaceTests(unittest.TestCase):
    def test_tier_targets_bronze_values(self):
        self.assertEqual(ga.TIER_TARGETS["quickdraw"], 1)
        self.assertEqual(ga.TIER_TARGETS["yolo"], 1)
        self.assertEqual(ga.TIER_TARGETS["pull_shark"], 16)
        self.assertEqual(ga.TIER_TARGETS["galaxy_brain"], 8)
        self.assertEqual(ga.TIER_TARGETS["pair_extraordinaire"], 10)

    def test_format_coauthor_trailer(self):
        self.assertEqual(
            ga.format_coauthor_trailer("Ada Lovelace", "ada@example.io"),
            "Co-authored-by: Ada Lovelace <ada@example.io>",
        )

    def test_log_path_constant(self):
        self.assertEqual(ga.LOG_PATH, "achievement_run.log")


class ManualCommandTests(unittest.TestCase):
    """main(["manual"]): exact steps for the two manual badges, exit 0."""

    def test_manual_exits_zero_prints_both_badge_guides(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = ga.main(["manual"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("Public Sponsor", out)
        self.assertIn("Starstruck", out)
        # Public Sponsor: $1 sponsorship via github.com/sponsors,
        # needs a real payment
        self.assertIn("https://github.com/sponsors", out)
        self.assertIn("$1", out)
        self.assertIn("payment", out.lower())
        # Starstruck: 16 stars from OTHER people — self-stars don't count
        self.assertIn("16", out)
        self.assertIn("other people", out.lower())


class MissingTokenCliTests(unittest.TestCase):
    def test_missing_token_exits_1_with_pat_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, ".env")
            with mock.patch.object(ga, "ENV_PATH", missing), \
                    mock.patch.dict(os.environ):
                os.environ.pop("GITHUB_TOKEN", None)
                buf = io.StringIO()
                with redirect_stderr(buf):
                    with self.assertRaises(SystemExit) as cm:
                        ga.main(["unlock"])
        self.assertEqual(cm.exception.code, 1)
        self.assertIn(PAT_URL, buf.getvalue())


class MainTests(ClientTestCase):
    """Task 7: main() wiring — unlock orchestration, ownership guard,
    dry-run planning, manual steps."""

    REPO_PATH = "/repos/octocat/achievement"

    def _fake_client(self, can_mark=True, fail=()):
        """A MagicMock standing in for GitHubClient(token).

        rest/graphql dispatch over canned octocat/achievement data; any
        path listed in ``fail`` raises GHError(403). Returns
        ``(client, calls)`` where calls records every dispatched
        ``("rest", method, path)`` / ``("graphql", query)``.
        """
        client = mock.MagicMock()
        client.dry_run = False
        calls = []

        def fake_rest(method, path, body=None):
            calls.append(("rest", method, path))
            if path in fail:
                raise ga.GHError(403, "blocked by test")
            if path == "/user":
                return {"login": "octocat"}
            if path == self.REPO_PATH:
                return {"name": "achievement",
                        "owner": {"login": "octocat"},
                        "default_branch": "main",
                        "html_url":
                            "https://github.com/octocat/achievement"}
            return {}

        def fake_graphql(query, variables=None):
            calls.append(("graphql", query))
            if "repository(" in query:
                return {"repository": {
                    "id": "R_1",
                    "discussionCategories": {"nodes": [
                        {"id": "C_qa", "name": "Q&A",
                         "isAnswerable": True}]},
                    "discussions": {"nodes": []}}}
            if "createDiscussion" in query:
                return {"createDiscussion": {"discussion":
                        {"id": "D_1", "number": 1}}}
            if "addDiscussionComment" in query:
                return {"addDiscussionComment": {"comment": {
                    "id": "CM_1",
                    "viewerCanMarkAsAnswer": can_mark}}}
            return {}

        client.rest.side_effect = fake_rest
        client.graphql.side_effect = fake_graphql
        return client, calls

    def _run_main(self, argv, client):
        out, err = io.StringIO(), io.StringIO()
        completed = subprocess.CompletedProcess(
            args=["git"], returncode=0, stdout="pushed", stderr="")
        with mock.patch.object(ga, "GitHubClient", return_value=client), \
                mock.patch("subprocess.run", return_value=completed), \
                redirect_stdout(out), redirect_stderr(err):
            rc = ga.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_unlock_dry_run_zero_http_calls_and_every_badge_name(self):
        """Dry-run constructs only a dry-run client (never a live one),
        makes zero HTTP calls and zero git subprocesses, and the printed
        plan mentions every badge name it covers."""
        init_calls = []

        class SpyClient(ga.GitHubClient):
            def __init__(self, token, dry_run=False, delay=2.5):
                init_calls.append(dry_run)
                super().__init__(token, dry_run=dry_run, delay=delay)

        buf = io.StringIO()
        with mock.patch.object(ga, "GitHubClient", SpyClient), \
                mock.patch("urllib.request.urlopen") as m_open, \
                mock.patch("subprocess.run") as m_run, \
                redirect_stdout(buf):
            rc = ga.main(["unlock", "--dry-run", "--token", "t",
                          "--coauthor", "Ada Lovelace <ada@x.io>"])
        self.assertEqual(rc, 0, msg=buf.getvalue())
        m_open.assert_not_called()
        m_run.assert_not_called()
        self.assertEqual(init_calls, [True])  # only the dry-run client
        out = buf.getvalue()
        for name in ("Quickdraw", "YOLO", "Pull Shark", "Galaxy Brain",
                     "Pair Extraordinaire"):
            self.assertIn(name, out)
        self.assertIn("DRY-RUN", out)
        # publish's git commands are dry-run printed, not executed
        self.assertIn("DRY-RUN git push -u origin main", out)

    def test_unlock_runs_badges_in_order_and_skips_pair_with_notice(self):
        client, calls = self._fake_client()
        rc, out, err = self._run_main(
            ["unlock", "--token", "t", "--tier", "base"], client)
        self.assertEqual(rc, 0, msg=out + err)
        # plan header announces the badge sequence
        self.assertIn("Unlock plan", out)
        # execution order: quickdraw -> yolo -> pull shark -> galaxy brain
        markers = [
            "Quickdraw: opened and closed issue",
            "YOLO: merged PR",
            "Pull Shark: 0/1 merged",
            "Galaxy Brain: 0/1 accepted answers",
            'Pair Extraordinaire: skipped - pass --coauthor "Name <email>"',
        ]
        positions = [out.index(m) for m in markers]
        self.assertEqual(positions, sorted(positions),
                        msg=f"badges ran out of order: {out}")
        # base tier = one cycle per badge
        self.assertIn("running 1 PR cycle", out)
        # summary table marks the skipped badge (not a failure)
        self.assertIn("Unlock summary", out)
        self.assertRegex(out, r"Pair Extraordinaire\s+SKIPPED")
        # pair was never attempted
        self.assertNotIn("Pair Extraordinaire PR", out)

    def test_unlock_with_coauthor_runs_pair_cycle_with_trailer(self):
        client, calls = self._fake_client()
        with mock.patch.object(ga, "pr_cycle", return_value=7) as m_cycle:
            rc, out, err = self._run_main(
                ["unlock", "--token", "t", "--tier", "base",
                 "--coauthor", "Ada Lovelace <ada@x.io>"], client)
        self.assertEqual(rc, 0, msg=out + err)
        self.assertRegex(out, r"Pair Extraordinaire\s+OK")
        pair_calls = [c for c in m_cycle.call_args_list
                     if "Pair Extraordinaire PR" in c.args[4]]
        self.assertEqual(len(pair_calls), 1)
        self.assertTrue(
            pair_calls[0].args[4].endswith(
                "\n\nCo-authored-by: Ada Lovelace <ada@x.io>"),
            msg=f"message lacks trailer: {pair_calls[0].args[4]!r}")

    def test_unlock_galaxy_self_mark_blocked_is_warning_not_failure(self):
        client, calls = self._fake_client(can_mark=False)
        rc, out, err = self._run_main(
            ["unlock", "--token", "t", "--tier", "base"], client)
        self.assertEqual(rc, 0, msg=out + err)
        self.assertRegex(out, r"Galaxy Brain\s+WARNING")
        self.assertIn("fallback", out.lower())
        # self-mark blocked: the mark mutation was never attempted
        queries = [c[1] for c in calls if c[0] == "graphql"]
        self.assertFalse(any("markDiscussionCommentAsAnswer" in q
                             for q in queries))

    def test_unlock_badge_failure_exits_1_and_continues_other_badges(self):
        client, calls = self._fake_client(
            fail=("/repos/octocat/achievement/issues",))
        rc, out, err = self._run_main(
            ["unlock", "--token", "t", "--tier", "base"], client)
        self.assertEqual(rc, 1)
        self.assertRegex(out, r"Quickdraw\s+FAILED")
        # other badges still ran
        self.assertIn("YOLO: merged PR", out)
        self.assertIn("Pull Shark: 0/1 merged", out)
        self.assertIn("Galaxy Brain", out)
        self.assertIn("Unlock summary", out)

    def test_unlock_rejects_repo_owned_by_another_user(self):
        client, calls = self._fake_client()

        def hostile_rest(method, path, body=None):
            calls.append(("rest", method, path))
            if path == "/user":
                return {"login": "octocat"}
            if path == "/repos/someone/achievement":
                return {"name": "achievement",
                        "owner": {"login": "someone-else"}}
            raise AssertionError(f"unexpected call {method} {path}")

        client.rest.side_effect = hostile_rest
        rc, out, err = self._run_main(
            ["unlock", "--token", "t",
             "--repo", "someone/achievement"], client)
        self.assertEqual(rc, 1)
        self.assertIn("own", err)  # message names the own-repos-only rule
        # stopped before any badge ran or any write was attempted
        self.assertNotIn("Unlock plan", out)
        mutating = [(m, p) for kind, m, p in calls
                    if kind == "rest"
                    and m in ("POST", "PUT", "PATCH", "DELETE")]
        self.assertEqual(mutating, [])

    def test_unlock_rejects_malformed_repo_argument(self):
        client, calls = self._fake_client()
        rc, out, err = self._run_main(
            ["unlock", "--token", "t", "--repo", "a/b/c"], client)
        self.assertEqual(rc, 1)
        self.assertIn("OWNER/NAME", err)
        # rejected before any API call was made
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
