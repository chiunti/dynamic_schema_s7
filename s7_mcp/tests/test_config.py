"""Tests for ``s7_mcp.config`` — environment parsing and fatal diagnostics.

``config.py`` is pure stdlib, so this module needs no dependency guard. The
contract under test: required vars missing or malformed values print a
``s7-author:`` diagnostic to stderr and exit with code 2; stdout stays clean.
"""

import contextlib
import io
import os
import unittest
from unittest.mock import patch

from s7_mcp import config
from s7_mcp.config import (
    ENV_BASE_URL,
    ENV_MAX_PAYLOAD_KB,
    ENV_PROJECT_ID,
    ENV_TIMEOUT_S,
    ENV_TOKEN,
)

_VALID_ENV = {
    ENV_BASE_URL: "http://backend.test",
    ENV_TOKEN: "secret-token",
}


def _run_with_env(env, func, *args, **kwargs):
    """Call ``func`` under ``os.environ`` replaced by ``env``, capturing stderr.

    Returns ``(result, stderr_text)``; ``result`` is ``None`` when the call
    raised ``SystemExit`` (whose code is appended to the return triple).
    """
    stderr = io.StringIO()
    code = None
    result = None
    with patch.dict(os.environ, env, clear=True):
        with contextlib.redirect_stderr(stderr):
            try:
                result = func(*args, **kwargs)
            except SystemExit as exc:
                code = exc.code
    return result, code, stderr.getvalue()


class FatalTests(unittest.TestCase):
    """``_fatal`` writes to stderr only and exits with code 2."""

    def test_fatal_prints_to_stderr_and_exits_2(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
            io.StringIO()
        ) as stderr:
            with self.assertRaises(SystemExit) as cm:
                config._fatal("boom")
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("s7-author: boom", stderr.getvalue())
        # stdio hygiene: diagnostics must never reach stdout.
        self.assertEqual(stdout.getvalue(), "")


class PositiveNumberTests(unittest.TestCase):
    """``_positive_number`` optional-env parsing (path: numeric guard)."""

    def _call_int(self, raw):
        env = dict(_VALID_ENV)
        if raw is not None:
            env[ENV_MAX_PAYLOAD_KB] = raw
        return _run_with_env(
            env, config._positive_number, ENV_MAX_PAYLOAD_KB, int, 512
        )

    def test_unset_returns_default(self):
        result, code, _ = _run_with_env(
            _VALID_ENV, config._positive_number, ENV_MAX_PAYLOAD_KB, int, 99
        )
        self.assertIsNone(code)
        self.assertEqual(result, 99)

    def test_blank_returns_default(self):
        result, code, _ = self._call_int("   ")
        self.assertIsNone(code)
        self.assertEqual(result, 512)

    def test_non_numeric_is_fatal(self):
        _, code, stderr = self._call_int("abc")
        self.assertEqual(code, 2)
        self.assertIn("must be a number", stderr)
        self.assertIn("'abc'", stderr)
        self.assertIn(ENV_MAX_PAYLOAD_KB, stderr)

    def test_zero_is_fatal(self):
        _, code, stderr = self._call_int("0")
        self.assertEqual(code, 2)
        self.assertIn("positive finite", stderr)

    def test_negative_is_fatal(self):
        _, code, stderr = self._call_int("-3")
        self.assertEqual(code, 2)
        self.assertIn("positive finite", stderr)

    def test_nan_is_fatal_for_float_cast(self):
        env = dict(_VALID_ENV)
        env[ENV_TIMEOUT_S] = "nan"
        _, code, stderr = _run_with_env(
            env, config._positive_number, ENV_TIMEOUT_S, float, 30.0
        )
        self.assertEqual(code, 2)
        self.assertIn("positive finite", stderr)
        self.assertIn("'nan'", stderr)

    def test_inf_is_fatal_for_float_cast(self):
        env = dict(_VALID_ENV)
        env[ENV_TIMEOUT_S] = "inf"
        _, code, stderr = _run_with_env(
            env, config._positive_number, ENV_TIMEOUT_S, float, 30.0
        )
        self.assertEqual(code, 2)
        self.assertIn("positive finite", stderr)

    def test_valid_int_and_float_values(self):
        env = dict(_VALID_ENV)
        env[ENV_MAX_PAYLOAD_KB] = "64"
        env[ENV_TIMEOUT_S] = "2.5"
        cfg, code, _ = _run_with_env(env, config.get_config)
        self.assertIsNone(code)
        self.assertEqual(cfg.max_payload_kb, 64)
        self.assertEqual(cfg.timeout_s, 2.5)


class GetConfigTests(unittest.TestCase):
    """``get_config`` required-var and URL-shape validation."""

    def test_missing_both_vars_lists_them_and_exits_2(self):
        _, code, stderr = _run_with_env({}, config.get_config)
        self.assertEqual(code, 2)
        self.assertIn("missing required environment variable", stderr)
        # The missing list joins names with ", " — this can only come from the
        # reported list, not from the "Set S7_BASE_URL ..." hint that follows.
        self.assertIn(f"{ENV_BASE_URL}, {ENV_TOKEN}", stderr)

    def test_missing_token_only(self):
        _, code, stderr = _run_with_env(
            {ENV_BASE_URL: "http://backend.test"}, config.get_config
        )
        self.assertEqual(code, 2)
        self.assertIn(ENV_TOKEN, stderr)
        # S7_BASE_URL is present, so it must not be reported as missing.
        self.assertNotIn("S7_BASE_URL,", stderr)

    def test_blank_values_count_as_missing(self):
        _, code, stderr = _run_with_env(
            {ENV_BASE_URL: "   ", ENV_TOKEN: "\t"}, config.get_config
        )
        self.assertEqual(code, 2)
        self.assertIn(ENV_BASE_URL, stderr)
        self.assertIn(ENV_TOKEN, stderr)

    def test_non_http_scheme_is_fatal(self):
        env = dict(_VALID_ENV, **{ENV_BASE_URL: "ftp://backend.test"})
        _, code, stderr = _run_with_env(env, config.get_config)
        self.assertEqual(code, 2)
        self.assertIn("absolute http(s) URL", stderr)

    def test_missing_netloc_is_fatal(self):
        env = dict(_VALID_ENV, **{ENV_BASE_URL: "http:///path/only"})
        _, code, stderr = _run_with_env(env, config.get_config)
        self.assertEqual(code, 2)
        self.assertIn("absolute http(s) URL", stderr)

    def test_bare_word_url_is_fatal(self):
        env = dict(_VALID_ENV, **{ENV_BASE_URL: "backend.test"})
        _, code, stderr = _run_with_env(env, config.get_config)
        self.assertEqual(code, 2)
        self.assertIn("absolute http(s) URL", stderr)

    def test_happy_path_defaults_and_stripped_url(self):
        env = dict(_VALID_ENV, **{ENV_BASE_URL: "https://backend.test/"})
        cfg, code, _ = _run_with_env(env, config.get_config)
        self.assertIsNone(code)
        self.assertEqual(cfg.base_url, "https://backend.test")
        self.assertEqual(cfg.token, "secret-token")
        self.assertEqual(cfg.max_payload_kb, 512)
        self.assertEqual(cfg.timeout_s, 30.0)

    def test_token_is_stripped_of_surrounding_whitespace(self):
        env = dict(_VALID_ENV, **{ENV_TOKEN: "  tok  "})
        cfg, code, _ = _run_with_env(env, config.get_config)
        self.assertIsNone(code)
        self.assertEqual(cfg.token, "tok")


class RequireTokenTests(unittest.TestCase):
    """``get_config(require_token=...)`` — stdio requires ``S7_TOKEN``,
    streamable-http treats it as a per-request fallback."""

    def test_require_token_defaults_to_true(self):
        """Pin the default: forgetting the kwarg keeps the strict stdio
        behavior."""
        import inspect

        param = inspect.signature(config.get_config).parameters[
            "require_token"
        ]
        self.assertIs(param.default, True)

    def test_optional_token_allows_missing_token(self):
        """Streamable-http: ``S7_TOKEN`` absent resolves ``token=None`` —
        each request's ``Authorization`` supplies the credential."""
        cfg, code, stderr = _run_with_env(
            {ENV_BASE_URL: "http://backend.test"},
            config.get_config,
            require_token=False,
        )
        self.assertIsNone(code, msg=stderr)
        self.assertIsNone(cfg.token)
        self.assertEqual(cfg.base_url, "http://backend.test")

    def test_optional_token_still_reads_a_present_token(self):
        """The env token stays a usable fallback when set."""
        cfg, code, _ = _run_with_env(
            dict(_VALID_ENV), config.get_config, require_token=False
        )
        self.assertIsNone(code)
        self.assertEqual(cfg.token, "secret-token")

    def test_optional_token_still_requires_base_url(self):
        """Only the token becomes optional — ``S7_BASE_URL`` is always
        required, and the missing-var hint stops mentioning the token."""
        _, code, stderr = _run_with_env(
            {}, config.get_config, require_token=False
        )
        self.assertEqual(code, 2)
        self.assertIn(ENV_BASE_URL, stderr)
        # The reported missing list must not claim S7_TOKEN — and the hint
        # is the server-environment variant, not the client one.
        self.assertNotIn(ENV_TOKEN, stderr)
        self.assertIn("server environment", stderr)

    def test_required_token_missing_exits_2_as_before(self):
        """Explicit ``require_token=True`` keeps the strict contract."""
        _, code, stderr = _run_with_env(
            {ENV_BASE_URL: "http://backend.test"},
            config.get_config,
            require_token=True,
        )
        self.assertEqual(code, 2)
        self.assertIn(ENV_TOKEN, stderr)


class ProjectIdTests(unittest.TestCase):
    """``S7_PROJECT_ID`` optional UUID parsing and validation."""

    _UUID = "12345678-1234-5678-1234-567812345678"

    def test_unset_project_id_defaults_to_none(self):
        cfg, code, _ = _run_with_env(_VALID_ENV, config.get_config)
        self.assertIsNone(code)
        self.assertIsNone(cfg.project_id)

    def test_blank_project_id_counts_as_unset(self):
        env = dict(_VALID_ENV, **{ENV_PROJECT_ID: "   "})
        cfg, code, _ = _run_with_env(env, config.get_config)
        self.assertIsNone(code)
        self.assertIsNone(cfg.project_id)

    def test_valid_project_id_is_canonicalized(self):
        env = dict(_VALID_ENV, **{ENV_PROJECT_ID: f"  {self._UUID.upper()}  "})
        cfg, code, _ = _run_with_env(env, config.get_config)
        self.assertIsNone(code)
        self.assertEqual(cfg.project_id, self._UUID)

    def test_malformed_project_id_is_fatal(self):
        env = dict(_VALID_ENV, **{ENV_PROJECT_ID: "not-a-uuid"})
        _, code, stderr = _run_with_env(env, config.get_config)
        self.assertEqual(code, 2)
        self.assertIn(ENV_PROJECT_ID, stderr)
        self.assertIn("must be a UUID", stderr)
        self.assertIn("'not-a-uuid'", stderr)


if __name__ == "__main__":
    unittest.main()
