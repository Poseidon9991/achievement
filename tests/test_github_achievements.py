"""Tests for github_achievements.py — Task 1: client core + token loading."""

import io
import json
import os
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
