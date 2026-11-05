"""Tests for ``s7_mcp.catalog_server`` — the governed changeset MCP surface.

The ``@mcp.tool()``/``@mcp.prompt()`` decorators register the callables but
return them unchanged, so every handler is invoked directly and the API
client is injected by patching ``_get_client`` — the same pattern as
``test_server.py``. The tools delegate to the client's named changeset
methods, so wire shapes (method, quoted path, body, ``project_id``
injection) are pinned in ``test_client.py``; here the contract is
argument forwarding plus verbatim envelope propagation.
"""

import importlib
import importlib.util
import inspect
import os
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

_DEPS = (
    importlib.util.find_spec("mcp") is not None
    and importlib.util.find_spec("httpx") is not None
)

if _DEPS:
    import s7_mcp.catalog_server as catalog_server
    import s7_mcp.server as author_server

# Pure-Python module — importable without mcp/httpx, so the prompt-content
# tests below run even when the optional dependencies are not installed.
from s7_mcp.prompts import PROPOSE_CATALOG_CHANGE_PROMPT

REPO_ROOT = Path(__file__).resolve().parents[2]

# The governed changeset loop: propose/read/fix only. Review verdicts and
# the governed apply are human actions — never credential capabilities.
EXPECTED_TOOLS = {
    "s7_propose_changeset",
    "s7_get_changeset",
    "s7_fix_changeset",
}

# The sibling entry point (``s7_mcp.server``) must keep its own surface:
# importing ``s7_mcp.catalog_server`` binds tools on its own ``mcp``
# instance and adds nothing to the author's.
AUTHOR_EXPECTED_TOOLS = {
    "s7_get_catalog",
    "s7_get_schema",
    "s7_get_draft",
    "s7_delete_draft",
    "s7_list_datatypes",
    "s7_validate_schema",
    "s7_import_draft",
    "s7_import_catalog",
    "s7_catalog_usage",
}

_CLEAN_ENV = {
    key: value for key, value in os.environ.items() if not key.startswith("S7_")
}

_OPS = [{"op": "add_node_type", "name": "x_type", "label": "X"}]


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
class ProposeChangesetTests(unittest.TestCase):
    """``s7_propose_changeset`` delegates to ``client.propose_changeset``."""

    def setUp(self):
        self.client = MagicMock()
        self.client.propose_changeset.return_value = {"ok": True}
        patcher = patch.object(
            catalog_server, "_get_client", return_value=self.client
        )
        self.get_client = patcher.start()
        self.addCleanup(patcher.stop)

    def test_ops_and_rationale_forwarded_verbatim(self):
        ops = list(_OPS)
        result = catalog_server.s7_propose_changeset(ops, "the why")
        self.assertIs(result, self.client.propose_changeset.return_value)
        self.client.propose_changeset.assert_called_once_with(ops, "the why")
        # Identity, not equality: the ops list must reach the client
        # untouched — the tool never reshapes the mutation document.
        self.assertIs(
            self.client.propose_changeset.call_args[0][0], ops
        )

    def test_ctx_is_forwarded_to_get_client(self):
        """``ctx`` is SDK-injected transport plumbing: it resolves the
        per-request credential through ``_get_client`` and never reaches
        the client method."""
        ctx = object()
        catalog_server.s7_propose_changeset(_OPS, "why", ctx=ctx)
        self.get_client.assert_called_once_with(ctx)
        self.client.propose_changeset.assert_called_once_with(_OPS, "why")

    def test_tool_signature_is_ops_rationale_ctx(self):
        self.assertEqual(
            list(
                inspect.signature(
                    catalog_server.s7_propose_changeset
                ).parameters
            ),
            ["ops", "rationale", "ctx"],
        )

    def test_created_envelope_propagates_verbatim(self):
        """The 201 body is the serialized changeset at ``valid`` —
        propagated untouched."""
        envelope = {
            "id": "12345678-1234-5678-1234-567812345678",
            "status": "valid",
            "ops": _OPS,
            "rationale": "the why",
            "validation_report": {"findings": [], "impact": {}},
        }
        self.client.propose_changeset.return_value = envelope
        self.assertIs(
            catalog_server.s7_propose_changeset(_OPS, "the why"), envelope
        )

    def test_invalid_changeset_is_a_body_not_an_error(self):
        """A validation failure is still a persisted 201 at ``invalid`` —
        ``validation_report.findings`` is the feedback channel, and the
        tool propagates it like any other envelope."""
        envelope = {
            "id": "12345678-1234-5678-1234-567812345678",
            "status": "invalid",
            "validation_report": {
                "findings": [
                    {
                        "op_index": 0,
                        "severity": "breaking",
                        "entity": "schemas.nodetype:x_type",
                        "code": "duplicate_natural_key",
                        "message": "target already exists",
                    }
                ],
                "impact": {},
            },
        }
        self.client.propose_changeset.return_value = envelope
        self.assertIs(
            catalog_server.s7_propose_changeset(_OPS, "the why"), envelope
        )

    def test_invalid_request_envelope_propagates_verbatim(self):
        envelope = {
            "error": "invalid_request",
            "message": "rationale must be non-blank",
        }
        self.client.propose_changeset.return_value = envelope
        self.assertIs(
            catalog_server.s7_propose_changeset(_OPS, " "), envelope
        )

    def test_capability_gate_envelope_propagates_verbatim(self):
        """403 for a project credential, a level-1 token or a token
        without ``can_propose_changeset`` — verbatim, whatever the body."""
        envelope = {
            "error": "forbidden",
            "detail": "can_propose_changeset capability required",
        }
        self.client.propose_changeset.return_value = envelope
        self.assertIs(
            catalog_server.s7_propose_changeset(_OPS, "the why"), envelope
        )

    def test_scopes_not_covered_envelope_propagates_verbatim(self):
        """The post-validation 403: derived scopes escape the token's
        ``allowed_scopes`` and the proposal rolls back."""
        envelope = {
            "error": "changeset_scopes_not_covered",
            "message": "ops derive scopes outside the token perimeter",
        }
        self.client.propose_changeset.return_value = envelope
        self.assertIs(
            catalog_server.s7_propose_changeset(_OPS, "the why"), envelope
        )


@unittest.skipUnless(_DEPS, "requires mcp + httpx")
class GetChangesetTests(unittest.TestCase):
    """``s7_get_changeset`` delegates to ``client.get_changeset``.

    The id travels inside the path — quoting lives in the client
    (``test_client.py`` pins ``"a/b c"`` → ``a%2Fb%20c``); the tool
    forwards the raw value untouched.
    """

    def setUp(self):
        self.client = MagicMock()
        self.client.get_changeset.return_value = {"ok": True}
        patcher = patch.object(
            catalog_server, "_get_client", return_value=self.client
        )
        self.get_client = patcher.start()
        self.addCleanup(patcher.stop)

    def test_changeset_id_forwarded_verbatim(self):
        result = catalog_server.s7_get_changeset("a/b c")
        self.assertIs(result, self.client.get_changeset.return_value)
        # The raw id — quoting is the client's job, never the tool's.
        self.client.get_changeset.assert_called_once_with("a/b c")

    def test_ctx_is_forwarded_to_get_client(self):
        ctx = object()
        catalog_server.s7_get_changeset("cs-1", ctx=ctx)
        self.get_client.assert_called_once_with(ctx)
        self.client.get_changeset.assert_called_once_with("cs-1")

    def test_tool_signature_is_changeset_id_ctx(self):
        self.assertEqual(
            list(
                inspect.signature(
                    catalog_server.s7_get_changeset
                ).parameters
            ),
            ["changeset_id", "ctx"],
        )

    def test_changeset_envelope_propagates_verbatim(self):
        """The 200 body carries ``status`` plus the full
        ``validation_report`` — the read arm of the feedback loop."""
        envelope = {
            "id": "cs-1",
            "status": "invalid",
            "validation_report": {
                "findings": [
                    {
                        "op_index": 1,
                        "severity": "warning",
                        "entity": "schemas.domain:colors",
                        "code": "row_implemented",
                        "message": "row already implemented",
                        "remediation": "propose an additive op instead",
                    }
                ],
                "impact": {"schemas.domain:colors": {"nodes": 3}},
            },
        }
        self.client.get_changeset.return_value = envelope
        self.assertIs(catalog_server.s7_get_changeset("cs-1"), envelope)

    def test_not_found_envelope_propagates_verbatim(self):
        envelope = {
            "error": "changeset_not_found",
            "changeset_id": "cs-9",
        }
        self.client.get_changeset.return_value = envelope
        self.assertIs(catalog_server.s7_get_changeset("cs-9"), envelope)

    def test_scopes_not_covered_envelope_propagates_verbatim(self):
        envelope = {
            "error": "changeset_scopes_not_covered",
            "scopes": ["forms", "sdui"],
        }
        self.client.get_changeset.return_value = envelope
        self.assertIs(catalog_server.s7_get_changeset("cs-1"), envelope)


@unittest.skipUnless(_DEPS, "requires mcp + httpx")
class FixChangesetTests(unittest.TestCase):
    """``s7_fix_changeset`` delegates to ``client.fix_changeset``."""

    def setUp(self):
        self.client = MagicMock()
        self.client.fix_changeset.return_value = {"ok": True}
        patcher = patch.object(
            catalog_server, "_get_client", return_value=self.client
        )
        self.get_client = patcher.start()
        self.addCleanup(patcher.stop)

    def test_id_ops_and_new_rationale_forwarded_verbatim(self):
        ops = list(_OPS)
        result = catalog_server.s7_fix_changeset(
            "cs-1", ops, "addresses finding at op 0"
        )
        self.assertIs(result, self.client.fix_changeset.return_value)
        self.client.fix_changeset.assert_called_once_with(
            "cs-1", ops, "addresses finding at op 0"
        )
        self.assertIs(self.client.fix_changeset.call_args[0][1], ops)

    def test_ctx_is_forwarded_to_get_client(self):
        ctx = object()
        catalog_server.s7_fix_changeset("cs-1", _OPS, "why", ctx=ctx)
        self.get_client.assert_called_once_with(ctx)
        self.client.fix_changeset.assert_called_once_with(
            "cs-1", _OPS, "why"
        )

    def test_tool_signature_is_id_ops_rationale_ctx(self):
        self.assertEqual(
            list(
                inspect.signature(
                    catalog_server.s7_fix_changeset
                ).parameters
            ),
            ["changeset_id", "ops", "rationale", "ctx"],
        )

    def test_revalidated_envelope_propagates_verbatim(self):
        """The 200 body is the same serialized changeset shape with a
        refreshed ``validation_report`` — ops were replaced wholesale."""
        envelope = {
            "id": "cs-1",
            "status": "valid",
            "ops": _OPS,
            "rationale": "addresses finding at op 0",
            "validation_report": {"findings": [], "impact": {}},
        }
        self.client.fix_changeset.return_value = envelope
        self.assertIs(
            catalog_server.s7_fix_changeset("cs-1", _OPS, "rev"), envelope
        )

    def test_conflict_envelope_propagates_verbatim(self):
        """409 when the current status has no ``→ validating`` edge —
        ``approved``/``applied``/``rejected`` and friends."""
        envelope = {
            "error": "changeset cannot transition from 'applied' to 'validating'",
        }
        self.client.fix_changeset.return_value = envelope
        self.assertIs(
            catalog_server.s7_fix_changeset("cs-1", _OPS, "rev"), envelope
        )

    def test_not_found_envelope_propagates_verbatim(self):
        envelope = {
            "error": "changeset_not_found",
            "changeset_id": "cs-9",
        }
        self.client.fix_changeset.return_value = envelope
        self.assertIs(
            catalog_server.s7_fix_changeset("cs-9", _OPS, "rev"), envelope
        )

    def test_scopes_not_covered_envelope_propagates_verbatim(self):
        envelope = {
            "error": "changeset_scopes_not_covered",
            "message": "patch derives scopes outside the token perimeter",
        }
        self.client.fix_changeset.return_value = envelope
        self.assertIs(
            catalog_server.s7_fix_changeset("cs-1", _OPS, "rev"), envelope
        )


@unittest.skipUnless(_DEPS, "requires mcp + httpx")
class McpSurfaceTests(unittest.TestCase):
    """The exposed MCP surface: exactly the three changeset tools."""

    def _tools_registry(self):
        manager = getattr(catalog_server.mcp, "_tool_manager", None)
        tools = getattr(manager, "_tools", None)
        return tools if isinstance(tools, dict) else {}

    def test_every_expected_tool_exists_and_is_callable(self):
        for name in EXPECTED_TOOLS:
            with self.subTest(tool=name):
                self.assertTrue(
                    callable(getattr(catalog_server, name)),
                    f"{name} missing",
                )

    def test_registered_tool_set_matches_module_functions(self):
        """The SDK registry (when introspectable) holds exactly the
        expected tools — nothing else was decorated."""
        registered = self._tools_registry()
        if registered:
            self.assertEqual(set(registered), EXPECTED_TOOLS)

    def test_no_approve_apply_reject_publish_tools(self):
        """Review verdicts and the governed apply are human actions —
        nothing named like them may leak into the module surface, as a
        tool or as a plain attribute."""
        for name in ("approve", "apply", "reject", "publish"):
            for attr in dir(catalog_server):
                self.assertNotIn(
                    name,
                    attr.lower(),
                    f"write-like attribute leaked into the module: {attr}",
                )

    def test_registered_registry_carries_no_write_verbs(self):
        """Registry-side guard for the never-write rule: even a tool
        registered under a non-obvious name cannot smuggle a verb."""
        for tool_name in self._tools_registry():
            self.assertNotIn("approve", tool_name)
            self.assertNotIn("apply", tool_name)
            self.assertNotIn("reject", tool_name)
            self.assertNotIn("publish", tool_name)

    def test_every_tool_signature_has_trailing_ctx(self):
        for name in EXPECTED_TOOLS:
            with self.subTest(tool=name):
                params = inspect.signature(
                    getattr(catalog_server, name)
                ).parameters
                self.assertEqual(list(params)[-1], "ctx")
                self.assertIs(params["ctx"].default, None)

    def test_ctx_absent_from_every_tool_input_schema(self):
        """Via the SDK registry: no tool exposes ``ctx`` in ``properties``
        or ``required`` — it is transport plumbing, not agent input."""
        tools = self._tools_registry()
        if not tools:  # registry layout not introspectable on this SDK
            self.skipTest("tool registry not introspectable")
        for name, tool in tools.items():
            with self.subTest(tool=name):
                schema = tool.parameters
                self.assertNotIn("ctx", schema.get("properties", {}))
                self.assertNotIn("ctx", schema.get("required", []))
                context_kwarg = getattr(tool, "context_kwarg", None)
                if context_kwarg is not None:
                    self.assertEqual(context_kwarg, "ctx")

    def test_mcp_instance_is_distinct_from_the_author_server(self):
        """Two entry points, one package: the tools bind to
        ``MCPServer("s7-catalog")``, never to the author's instance —
        registering ``s7-author`` alone grants no propose capability."""
        self.assertIsNot(catalog_server.mcp, author_server.mcp)

    def test_author_registry_is_untouched_by_this_import(self):
        """Import-safety: importing ``s7_mcp.catalog_server`` adds no tool
        to ``s7_mcp.server`` — the surfaces never mix, in either
        direction."""
        manager = getattr(author_server.mcp, "_tool_manager", None)
        registered = getattr(manager, "_tools", None)
        if isinstance(registered, dict):
            self.assertEqual(set(registered), AUTHOR_EXPECTED_TOOLS)

    def test_get_client_is_the_shared_author_machinery(self):
        """The credential resolver and bounded-LRU cache live in
        ``s7_mcp.server`` — imported, not duplicated."""
        self.assertIs(catalog_server._get_client, author_server._get_client)


@unittest.skipUnless(_DEPS, "requires mcp + httpx")
class PromptTests(unittest.TestCase):
    """``propose_catalog_change`` serves the changeset-loop prompt."""

    def test_prompt_returns_the_template_text(self):
        result = catalog_server.propose_catalog_change()
        self.assertIs(result, PROPOSE_CATALOG_CHANGE_PROMPT)
        self.assertIsInstance(result, str)
        self.assertTrue(result.strip())

    def test_prompt_is_registered_on_the_catalog_instance(self):
        manager = getattr(catalog_server.mcp, "_prompt_manager", None)
        prompts = getattr(manager, "_prompts", None)
        if isinstance(prompts, dict):
            self.assertEqual(set(prompts), {"propose_catalog_change"})


class PromptContentTests(unittest.TestCase):
    """``PROPOSE_CATALOG_CHANGE_PROMPT`` content checks.

    No ``_DEPS`` guard: ``s7_mcp.prompts`` holds pure string constants and
    is importable without the optional mcp/httpx dependencies — the
    regression guards must not skip silently in minimal environments.
    """

    def test_names_the_three_changeset_tools_in_loop_order(self):
        """The prompt is the workflow doc for the propose → read → fix
        loop: all three tools must be named, and named in order."""
        propose = PROPOSE_CATALOG_CHANGE_PROMPT.index("s7_propose_changeset")
        read = PROPOSE_CATALOG_CHANGE_PROMPT.index("s7_get_changeset")
        fix = PROPOSE_CATALOG_CHANGE_PROMPT.index("s7_fix_changeset")
        self.assertLess(propose, read)
        self.assertLess(read, fix)

    def test_documents_the_capability_gate(self):
        """Every tool requires a user MCP token at ``mcp_level >= 2``
        plus the ``can_propose_changeset`` flag — the prompt states both
        so agents can diagnose their own 403s."""
        self.assertIn("can_propose_changeset", PROPOSE_CATALOG_CHANGE_PROMPT)
        self.assertIn("mcp_level >= 2", PROPOSE_CATALOG_CHANGE_PROMPT)

    def test_declares_review_and_apply_as_human_actions(self):
        """The never-write rule must be discoverable in the prose the
        agent reads: there is intentionally no approve/apply/reject
        tool — verdicts and the governed apply are human actions."""
        self.assertIn(
            "no approve/apply/reject tool", PROPOSE_CATALOG_CHANGE_PROMPT
        )
        self.assertIn("human actions", PROPOSE_CATALOG_CHANGE_PROMPT)

    def test_documents_the_scopes_gate(self):
        """When the token carries ``allowed_scopes``, uncovered derived
        scopes answer 403 ``changeset_scopes_not_covered`` and roll back
        — the perimeter rule is part of the workflow."""
        self.assertIn("allowed_scopes", PROPOSE_CATALOG_CHANGE_PROMPT)
        self.assertIn(
            "changeset_scopes_not_covered", PROPOSE_CATALOG_CHANGE_PROMPT
        )

    def test_documents_wholesale_replacement_and_fresh_rationale(self):
        """``s7_fix_changeset`` replaces the ops document wholesale —
        never merges — and every PATCH requires a NEW non-blank
        rationale."""
        self.assertIn("wholesale", PROPOSE_CATALOG_CHANGE_PROMPT)
        self.assertIn("NEW", PROPOSE_CATALOG_CHANGE_PROMPT)
        self.assertIn("rationale", PROPOSE_CATALOG_CHANGE_PROMPT)

    def test_documents_the_patchable_status_set_and_409(self):
        """Only ``proposed``/``invalid``/``valid``/``stale`` accept PATCH;
        the terminal and in-flight statuses answer 409."""
        for status in ("proposed", "invalid", "valid", "stale"):
            with self.subTest(status=status):
                self.assertIn(status, PROPOSE_CATALOG_CHANGE_PROMPT)
        self.assertIn("409", PROPOSE_CATALOG_CHANGE_PROMPT)

    def test_documents_the_six_writable_models(self):
        """Ops span the six writable catalog models — the same set the
        grammar reference and the backend validator accept."""
        for label in (
            "nodetype",
            "attributedef",
            "domain",
            "domainitem",
            "nodetypevariant",
            "nodetypecomposition",
        ):
            with self.subTest(model=label):
                self.assertIn(label, PROPOSE_CATALOG_CHANGE_PROMPT)


@unittest.skipUnless(_DEPS, "requires mcp + httpx")
class MainTests(unittest.TestCase):
    """``main(argv)`` — transport dispatch, config gating and argparse.

    ``mcp.run`` and ``get_config`` are always mocked: no port is bound and
    no event loop is started.
    """

    def _run_main(self, argv):
        with patch.object(
            catalog_server, "get_config"
        ) as get_config, patch.object(catalog_server.mcp, "run") as run:
            catalog_server.main(argv)
        return get_config, run

    def test_default_argv_serves_stdio_and_requires_token(self):
        get_config, run = self._run_main([])
        get_config.assert_called_once_with(require_token=True)
        run.assert_called_once_with()

    def test_explicit_stdio_transport(self):
        get_config, run = self._run_main(["--transport", "stdio"])
        get_config.assert_called_once_with(require_token=True)
        run.assert_called_once_with()

    def test_streamable_http_defaults(self):
        get_config, run = self._run_main(["--transport", "streamable-http"])
        get_config.assert_called_once_with(require_token=False)
        run.assert_called_once_with(
            transport="streamable-http", host="127.0.0.1", port=8000
        )

    def test_streamable_http_custom_host_and_port(self):
        get_config, run = self._run_main(
            [
                "--transport", "streamable-http",
                "--host", "0.0.0.0",
                "--port", "9100",
            ]
        )
        get_config.assert_called_once_with(require_token=False)
        run.assert_called_once_with(
            transport="streamable-http", host="0.0.0.0", port=9100
        )

    def test_run_http_v1_fallback_uses_settings(self):
        """mcp 1.x compat: when ``run`` rejects the bind kwargs the
        host/port go on ``mcp.settings`` and ``run`` is retried with the
        transport only — same shape as the author's ``_run_http``."""
        run = MagicMock(side_effect=[TypeError("unexpected kwargs"), None])
        settings = types.SimpleNamespace(host="127.0.0.1", port=8000)
        with patch.object(catalog_server, "get_config"), patch.object(
            catalog_server.mcp, "run", run
        ), patch.object(catalog_server.mcp, "settings", settings):
            catalog_server.main(
                [
                    "--transport", "streamable-http",
                    "--host", "0.0.0.0",
                    "--port", "9100",
                ]
            )
        self.assertEqual(
            run.call_args_list,
            [
                call(transport="streamable-http", host="0.0.0.0", port=9100),
                call(transport="streamable-http"),
            ],
        )
        self.assertEqual(settings.host, "0.0.0.0")
        self.assertEqual(settings.port, 9100)

    def test_config_failure_on_http_exits_before_run(self):
        with patch.object(
            catalog_server, "get_config", side_effect=SystemExit(2)
        ), patch.object(catalog_server.mcp, "run") as run:
            with self.assertRaises(SystemExit) as cm:
                catalog_server.main(["--transport", "streamable-http"])
        self.assertEqual(cm.exception.code, 2)
        run.assert_not_called()


@unittest.skipUnless(_DEPS, "requires mcp + httpx")
class ImportSafetyTests(unittest.TestCase):
    """Importing the catalog server must never require configuration."""

    def test_import_without_env_vars_succeeds(self):
        result = _run("-c", "import s7_mcp.catalog_server")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stderr, "")

    def test_import_leaves_the_author_registry_at_nine_tools(self):
        """Subprocess pin for surface isolation: after importing
        ``s7_mcp.catalog_server`` the author's ``_tool_manager`` still
        holds exactly its nine tools — importing the sibling module
        registers its own instance and adds nothing."""
        code = (
            "import s7_mcp.catalog_server\n"
            "import s7_mcp.server as s\n"
            "print(sorted(s.mcp._tool_manager._tools), flush=True)\n"
        )
        result = _run("-c", code)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("s7_get_catalog", result.stdout)
        self.assertIn("s7_catalog_usage", result.stdout)
        self.assertNotIn("s7_propose_changeset", result.stdout)
        self.assertNotIn("s7_get_changeset", result.stdout)
        self.assertNotIn("s7_fix_changeset", result.stdout)
        self.assertEqual(result.stdout.count("s7_"), len(AUTHOR_EXPECTED_TOOLS))

    def test_module_entrypoint_fails_fast_without_env(self):
        """``python -m s7_mcp.catalog_server`` exits 2 with the
        diagnostic on stderr only — stdout carries the MCP protocol."""
        result = _run("-m", "s7_mcp.catalog_server")
        self.assertEqual(result.returncode, 2)
        self.assertIn("missing required environment variable", result.stderr)
        self.assertIn("S7_BASE_URL", result.stderr)
        self.assertIn("S7_TOKEN", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_streamable_http_without_env_fails_for_base_url_not_token(self):
        """``S7_TOKEN`` is optional under streamable-http: a bare run
        still exits 2, but only ``S7_BASE_URL`` is reported missing."""
        result = _run(
            "-m", "s7_mcp.catalog_server",
            "--transport", "streamable-http",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("S7_BASE_URL", result.stderr)
        self.assertNotIn("S7_TOKEN", result.stderr)
        self.assertEqual(result.stdout, "")


class _FakeFastMCP:
    """Stand-in for the mcp 1.x ``FastMCP`` class.

    Deliberately takes no ``version`` kwarg so the TypeError fallback in the
    shim is exercised too; decorator factories return identity decorators
    while recording registrations.
    """

    instances = []

    def __init__(self, name):
        self.name = name
        self.tools = {}
        self.resources = {}
        self.prompts = {}
        type(self).instances.append(self)

    def tool(self, *args, **kwargs):
        def decorator(func):
            self.tools[func.__name__] = func
            return func

        return decorator

    def resource(self, uri, **kwargs):
        def decorator(func):
            self.resources[uri] = func
            return func

        return decorator

    def prompt(self, *args, **kwargs):
        def decorator(func):
            self.prompts[func.__name__] = func
            return func

        return decorator

    def run(self, *args, **kwargs):  # pragma: no cover - never invoked
        raise AssertionError("run() must not be called during import")


class _FakeContext:
    """Stand-in for the SDK ``Context`` class on the ``FastMCP`` fallback.

    The shim imports ``Context`` alongside ``FastMCP`` — it is only used in
    annotations (deferred by ``from __future__ import annotations``), so an
    empty class suffices.
    """


@unittest.skipUnless(_DEPS, "requires mcp + httpx")
class ShimBranchTests(unittest.TestCase):
    """Both branches of the ``MCPServer``/``FastMCP`` compatibility shim."""

    def test_active_branch_matches_installed_sdk(self):
        """``MCPServer`` resolves to the class the installed SDK provides:
        ``mcp.server.mcpserver.MCPServer`` on mcp 2.x, ``FastMCP`` on 1.x."""
        try:
            from mcp.server.mcpserver import MCPServer as expected
        except ImportError:  # mcp 1.x
            from mcp.server.fastmcp import FastMCP as expected
        self.assertIs(catalog_server.MCPServer, expected)
        self.assertIsInstance(catalog_server.mcp, catalog_server.MCPServer)

    def test_fastmcp_fallback_branch(self):
        """Reloading the module with ``MCPServer`` unimportable lands on
        the ``FastMCP`` branch and still registers the full surface on a
        ``s7-catalog``-named instance."""
        fake_mcpserver = types.ModuleType("mcp.server.mcpserver")
        # No MCPServer attribute: ``from ... import MCPServer`` raises
        # ImportError and the shim falls back to FastMCP.
        fake_fastmcp = types.ModuleType("mcp.server.fastmcp")
        _FakeFastMCP.instances = []
        fake_fastmcp.FastMCP = _FakeFastMCP
        fake_fastmcp.Context = _FakeContext

        saved = {
            name: sys.modules.get(name)
            for name in ("mcp.server.mcpserver", "mcp.server.fastmcp")
        }

        def restore():
            for name, module in saved.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module
            importlib.reload(catalog_server)

        self.addCleanup(restore)
        with patch.dict(
            sys.modules,
            {
                "mcp.server.mcpserver": fake_mcpserver,
                "mcp.server.fastmcp": fake_fastmcp,
            },
        ):
            importlib.reload(catalog_server)

            self.assertIs(catalog_server.MCPServer, _FakeFastMCP)
            # First construction with ``version=`` fails (no such kwarg on
            # mcp 1.x) and the shim retries without it: exactly one
            # instance, named for this entry point.
            self.assertEqual(len(_FakeFastMCP.instances), 1)
            self.assertIs(catalog_server.mcp, _FakeFastMCP.instances[0])
            self.assertEqual(catalog_server.mcp.name, "s7-catalog")
            self.assertEqual(set(catalog_server.mcp.tools), EXPECTED_TOOLS)
            self.assertEqual(set(catalog_server.mcp.resources), set())
            self.assertEqual(
                set(catalog_server.mcp.prompts), {"propose_catalog_change"}
            )


if __name__ == "__main__":
    unittest.main()
