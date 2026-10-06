"""Tests for github_achievements.py — Tasks 1-3.

Task 1: client core + token loading. Task 2: doctor/status commands and
profile badge parsing. Task 3: publish command (repo creation + git push).
"""

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


if __name__ == "__main__":
    unittest.main()
