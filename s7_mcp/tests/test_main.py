"""Tests for process-level entry points: import safety and the deps hint.

These run in subprocesses (``sys.executable``) so each case gets a clean
interpreter: no ``S7_*`` environment variables, and ``sys.modules`` /
``sys.meta_path`` can be poisoned to simulate missing optional dependencies.
"""

import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_DEPS = (
    importlib.util.find_spec("mcp") is not None
    and importlib.util.find_spec("httpx") is not None
)

if _DEPS:
    try:  # mcp 1.x ships a working FastMCP; mcp 2.x leaves a tombstone
        importlib.import_module("mcp.server.fastmcp")
        _FASTMCP_RESOLVES = True
    except ImportError:
        _FASTMCP_RESOLVES = False
else:
    _FASTMCP_RESOLVES = False

_CLEAN_ENV = {
    key: value for key, value in os.environ.items() if not key.startswith("S7_")
}

# Simulates ``httpx`` being uninstalled: ``import httpx`` raises ImportError
# whose ``name`` is exactly "httpx".
_POISON_HTTPX = "import sys; sys.modules['httpx'] = None; import s7_mcp.__main__"

# Simulates ``mcp`` being uninstalled via a meta-path blocker, so the
# ModuleNotFoundError carries ``name='mcp'`` like a genuinely absent package.
_BLOCK_MCP = (
    "import sys, importlib.abc\n"
    "class _Block(importlib.abc.MetaPathFinder):\n"
    "    def find_spec(self, name, path=None, target=None):\n"
    "        if name == 'mcp' or name.startswith('mcp.'):\n"
    "            raise ModuleNotFoundError('No module named %r' % name,"
    " name=name)\n"
    "        return None\n"
    "sys.meta_path.insert(0, _Block())\n"
    "import s7_mcp.__main__\n"
)

# Poisoning only the submodule: the shim falls back to ``fastmcp``, a
# tombstone under mcp 2.x. Which ``exc.name`` escapes depends on the SDK
# layout ('mcp.server.fastmcp' for a flat tombstone, 'mcp.server.mcpserver'
# when the parent package eager-imports it) — always rooted at mcp, so the
# missing-dependency hint applies. Under mcp 1.x the fallback resolves for
# real and the import succeeds.
_POISON_MCPSERVER = (
    "import sys; sys.modules['mcp.server.mcpserver'] = None;"
    " import s7_mcp.__main__"
)

# ``sys.modules['mcp'] = None`` makes every ``mcp.*`` import inside the shim
# fail with ``exc.name='mcp.server'`` (parent package is not a package) —
# still a missing s7_mcp requirement, so the hint applies on any SDK version.
_POISON_MCP_ROOT = "import sys; sys.modules['mcp'] = None; import s7_mcp.__main__"

# A module outside the optional deps failing (``json``, imported by
# ``server.py``) means a broken environment, not missing requirements: the
# ImportError must propagate under its real name — traceback, no hint.
_POISON_JSON = "import sys; sys.modules['json'] = None; import s7_mcp.__main__"

# A bare ``ImportError`` raised inside a dependency carries ``name=None`` —
# ``__main__``'s guard must treat it as "not a missing s7_mcp dep" and let
# it propagate, instead of crashing on ``None.split``.
_BLOCK_NAMELESS = (
    "import sys, importlib.abc\n"
    "class _Boom(importlib.abc.MetaPathFinder):\n"
    "    def find_spec(self, name, path=None, target=None):\n"
    "        if name == 'mcp' or name.startswith('mcp.'):\n"
    "            raise ImportError('boom')  # no name= → exc.name is None\n"
    "        return None\n"
    "sys.meta_path.insert(0, _Boom())\n"
    "import s7_mcp.__main__\n"
)


def _run(*args, env=None):
    return subprocess.run(
        [sys.executable, *args],
        env=_CLEAN_ENV if env is None else env,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=30,
    )


@unittest.skipUnless(_DEPS, "requires mcp + httpx")
class ImportSafetyTests(unittest.TestCase):
    """Importing the server must never require configuration (lazy config)."""

    def test_import_server_without_env_vars_succeeds(self):
        result = _run("-c", "import s7_mcp.server")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")

    def test_module_entrypoint_fails_fast_without_env(self):
        """``python -m s7_mcp`` exits 2 with the diagnostic on stderr only."""
        result = _run("-m", "s7_mcp")
        self.assertEqual(result.returncode, 2)
        self.assertIn("missing required environment variable", result.stderr)
        self.assertIn("S7_BASE_URL", result.stderr)
        self.assertIn("S7_TOKEN", result.stderr)
        # stdio hygiene: stdout carries the MCP protocol and stays empty.
        self.assertEqual(result.stdout, "")


@unittest.skipUnless(_DEPS, "requires mcp + httpx")
class MissingDependencyHintTests(unittest.TestCase):
    """``__main__`` converts missing optional deps into a friendly hint."""

    def _assert_hint(self, code, dep_name):
        result = _run("-c", code)
        self.assertEqual(result.returncode, 2, msg=result.stderr)
        self.assertIn("missing dependency", result.stderr)
        self.assertIn(repr(dep_name), result.stderr)
        self.assertIn("pip install -r s7_mcp/requirements.txt", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_missing_httpx_prints_hint_and_exits_2(self):
        self._assert_hint(_POISON_HTTPX, "httpx")

    def test_missing_mcp_prints_hint_and_exits_2(self):
        self._assert_hint(_BLOCK_MCP, "mcp")

    def test_missing_mcpserver_submodule_falls_back_or_fails(self):
        """``mcp.server.mcpserver`` failing makes the shim try ``fastmcp``:
        resolvable under mcp 1.x (import succeeds), a tombstone under 2.x
        (an ``mcp.server.*`` ImportError escapes → hint + exit 2)."""
        if _FASTMCP_RESOLVES:  # mcp 1.x: the real fallback path works end to end
            result = _run("-c", _POISON_MCPSERVER)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
        else:  # mcp 2.x: a broken mcp.* import is a missing dep → hint + exit 2
            result = _run("-c", _POISON_MCPSERVER)
            self.assertEqual(result.returncode, 2, msg=result.stderr)
            self.assertIn("missing dependency 'mcp.server", result.stderr)
            self.assertIn("pip install -r s7_mcp/requirements.txt", result.stderr)
            self.assertEqual(result.stdout, "")

    def test_poisoned_mcp_root_prints_hint_and_exits_2(self):
        """``sys.modules['mcp'] = None`` fails ``mcp.*`` imports with
        ``exc.name='mcp.server'`` — still rooted in a missing dependency."""
        self._assert_hint(_POISON_MCP_ROOT, "mcp.server")

    def test_unrelated_import_error_propagates(self):
        """An ImportError not rooted in mcp/httpx keeps its real name:
        traceback, exit other than 2, no hint, stdout stays clean."""
        result = _run("-c", _POISON_JSON)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotEqual(result.returncode, 2)
        self.assertIn("Traceback", result.stderr)
        self.assertNotIn("missing dependency", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_nameless_import_error_propagates(self):
        """A bare ``ImportError`` (``name=None``) inside a dependency is a
        broken environment too: traceback, exit other than 2, no hint."""
        result = _run("-c", _BLOCK_NAMELESS)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotEqual(result.returncode, 2)
        self.assertIn("Traceback", result.stderr)
        self.assertIn("ImportError: boom", result.stderr)
        self.assertNotIn("missing dependency", result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
