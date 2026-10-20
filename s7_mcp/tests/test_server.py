"""Tests for ``s7_mcp.server`` — tools, resources, payloads and the SDK shim.

The ``@mcp.tool()``/``@mcp.resource()``/``@mcp.prompt()`` decorators register
the callables but return them unchanged, so every handler is invoked directly
and the API client is injected by patching ``_get_client``.
"""

import importlib
import importlib.util
import inspect
import json
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

_DEPS = (
    importlib.util.find_spec("mcp") is not None
    and importlib.util.find_spec("httpx") is not None
)

if _DEPS:
    import s7_mcp.server as server
    from s7_mcp.prompts import AUTHOR_CATALOG_PROMPT, CREATE_TRAMITE_FORM_PROMPT

EXPECTED_TOOLS = {
    "s7_get_catalog",
    "s7_get_schema",
    "s7_list_datatypes",
    "s7_validate_schema",
    "s7_import_draft",
    "s7_import_catalog",
}


@unittest.skipUnless(_DEPS, "requires mcp + httpx")
class ToolPathTests(unittest.TestCase):
    """Each tool forwards to its 1:1 ``/api/`` path with quoted segments."""

    def setUp(self):
        self.client = MagicMock()
        self.client.get_json.return_value = {"ok": True}
        self.client.post_json.return_value = {"ok": True}
        patcher = patch.object(
            server, "_get_client", return_value=self.client
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_get_catalog_plain_scope(self):
        result = server.s7_get_catalog("forms")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(self.client.get_json.call_args[0][0], "catalog/forms/")

    def test_get_catalog_quotes_reserved_characters(self):
        server.s7_get_catalog("a/b c?%ñ")
        self.assertEqual(
            self.client.get_json.call_args[0][0],
            "catalog/a%2Fb%20c%3F%25%C3%B1/",
        )

    def test_get_schema_quotes_each_segment_independently(self):
        server.s7_get_schema("node/type", "key?1", "v%2")
        self.assertEqual(
            self.client.get_json.call_args[0][0],
            "schema/node%2Ftype/key%3F1/v%252/",
        )

    def test_list_datatypes_path(self):
        server.s7_list_datatypes()
        self.assertEqual(
            self.client.get_json.call_args[0][0], "catalog/datatypes/"
        )

    def test_error_envelope_propagates_verbatim(self):
        envelope = {"error": {"code": "transport_error", "message": "x", "status": None}}
        self.client.get_json.return_value = envelope
        self.assertIs(server.s7_get_catalog("forms"), envelope)


@unittest.skipUnless(_DEPS, "requires mcp + httpx")
class SchemaPayloadTests(unittest.TestCase):
    """``_schema_payload`` shapes and the never-write payload contract."""

    def setUp(self):
        self.client = MagicMock()
        self.client.post_json.return_value = {"ok": True}
        patcher = patch.object(
            server, "_get_client", return_value=self.client
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _posted(self):
        return self.client.post_json.call_args[0]

    def test_dict_schema_is_serialized_to_schema_text(self):
        schema = {"type": "object", "n": 1}
        result = server.s7_validate_schema("survey", schema, "k", "v1")
        self.assertIs(result, self.client.post_json.return_value)
        path, payload = self._posted()
        self.assertEqual(path, "schema/validate/")
        self.assertEqual(payload["schema_text"], json.dumps(schema))
        self.assertEqual(payload["node_type"], "survey")
        self.assertEqual(payload["schema_key"], "k")
        self.assertEqual(payload["schema_version"], "v1")

    def test_str_schema_is_sent_verbatim(self):
        raw = '{"already": "serialized"}'
        server.s7_validate_schema("survey", raw)
        _, payload = self._posted()
        self.assertEqual(payload["schema_text"], raw)

    def test_optional_key_and_version_omitted_when_not_given(self):
        server.s7_validate_schema("survey", {"a": 1})
        _, payload = self._posted()
        self.assertEqual(set(payload), {"schema_text", "node_type"})

    def test_validate_payload_has_no_write_fields(self):
        server.s7_validate_schema("survey", {"a": 1}, "k", "v")
        _, payload = self._posted()
        self.assertEqual(
            set(payload),
            {"schema_text", "node_type", "schema_key", "schema_version"},
        )
        # Tenancy fields stay out of the tool-built payload: project_id (when
        # configured) is injected by the client from S7_PROJECT_ID downstream,
        # never from agent-controlled tool input.
        for forbidden in ("schema_status", "project_id", "organization_id"):
            self.assertNotIn(forbidden, payload)

    def test_import_posts_to_import_endpoint_with_overwrite(self):
        result = server.s7_import_draft("survey", {"a": 1}, overwrite=True)
        self.assertIs(result, self.client.post_json.return_value)
        path, payload = self._posted()
        self.assertEqual(path, "schema/import/")
        self.assertIs(payload["overwrite"], True)
        # Same rule as validate: project_id comes from S7_PROJECT_ID via the
        # client, never from the tool surface.
        for forbidden in ("schema_status", "project_id", "organization_id", "status"):
            self.assertNotIn(forbidden, payload)

    def test_import_default_overwrite_is_false(self):
        server.s7_import_draft("survey", "raw-json")
        _, payload = self._posted()
        self.assertIs(payload["overwrite"], False)


@unittest.skipUnless(_DEPS, "requires mcp + httpx")
class CatalogImportTests(unittest.TestCase):
    """``s7_import_catalog`` forwards the fixture-format payload verbatim."""

    def setUp(self):
        self.client = MagicMock()
        self.client.post_json.return_value = {"ok": True}
        patcher = patch.object(
            server, "_get_client", return_value=self.client
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _objects(self):
        return [
            {
                "model": "schemas.nodetype",
                "fields": {
                    "label": "esign_root",
                    "is_root": True,
                    "json_scope": "esign",
                },
            }
        ]

    def test_posts_objects_verbatim_to_catalog_import(self):
        objects = self._objects()
        result = server.s7_import_catalog(objects)
        self.assertIs(result, self.client.post_json.return_value)
        path, payload = self.client.post_json.call_args[0]
        self.assertEqual(path, "catalog/import/")
        self.assertEqual(payload, {"objects": objects})
        self.assertIs(payload["objects"], objects)

    def test_success_envelope_propagates_verbatim(self):
        envelope = {
            "applied": True,
            "created": {"nodetype": 1},
            "updated": {},
            "warnings": [],
        }
        self.client.post_json.return_value = envelope
        self.assertIs(server.s7_import_catalog(self._objects()), envelope)

    def test_applied_false_envelope_propagates_verbatim(self):
        """A 400-style ``{"applied": false, "errors": [...]}`` body reaches
        the caller untouched — the tool never inspects the envelope."""
        envelope = {
            "applied": False,
            "errors": [
                {
                    "code": "unknown_model",
                    "message": "model not in the catalog subset",
                    "path": "$.objects[0]",
                }
            ],
        }
        self.client.post_json.return_value = envelope
        self.assertIs(server.s7_import_catalog(self._objects()), envelope)


@unittest.skipUnless(_DEPS, "requires mcp + httpx")
class ResourceTests(unittest.TestCase):
    """Resources mirror tool paths and propagate envelopes per contract."""

    def setUp(self):
        self.client = MagicMock()
        patcher = patch.object(
            server, "_get_client", return_value=self.client
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_catalog_resource_path(self):
        self.client.get_json.return_value = {"scope": "forms"}
        result = server.catalog_resource("a/b")
        self.assertEqual(result, {"scope": "forms"})
        self.assertEqual(
            self.client.get_json.call_args[0][0], "catalog/a%2Fb/"
        )

    def test_datatypes_resource_path(self):
        self.client.get_json.return_value = {"datatypes": []}
        self.assertEqual(server.datatypes_resource(), {"datatypes": []})
        self.assertEqual(
            self.client.get_json.call_args[0][0], "catalog/datatypes/"
        )

    def test_example_resource_returns_the_example_value(self):
        example = {"field": "value"}
        self.client.get_json.return_value = {"example": example, "scope": "s"}
        self.assertIs(server.example_resource("s"), example)

    def test_example_resource_none_example_returns_marker(self):
        self.client.get_json.return_value = {"example": None, "scope": "s"}
        self.assertEqual(server.example_resource("s"), {"example": None})

    def test_example_resource_error_envelope_propagates_verbatim(self):
        envelope = {"error": {"code": "transport_error", "message": "m", "status": None}}
        self.client.get_json.return_value = envelope
        self.assertIs(server.example_resource("s"), envelope)

    def test_schema_resource_unwraps_data(self):
        schema = {"nodes": []}
        self.client.get_json.return_value = {"data": schema, "meta": {"v": 1}}
        self.assertIs(server.schema_resource("nt", "k", "v"), schema)
        self.assertEqual(
            self.client.get_json.call_args[0][0], "schema/nt/k/v/"
        )

    def test_schema_resource_error_envelope_propagates_verbatim(self):
        envelope = {"type": "schema_not_found", "status": 404}
        self.client.get_json.return_value = envelope
        self.assertIs(server.schema_resource("nt", "k", "v"), envelope)


@unittest.skipUnless(_DEPS, "requires mcp + httpx")
class PromptTests(unittest.TestCase):
    def test_prompt_returns_the_template_text(self):
        result = server.create_tramite_form()
        self.assertIs(result, CREATE_TRAMITE_FORM_PROMPT)
        self.assertIsInstance(result, str)
        self.assertTrue(result.strip())

    def test_author_catalog_prompt_returns_the_template_text(self):
        result = server.author_catalog()
        self.assertIs(result, AUTHOR_CATALOG_PROMPT)
        self.assertIsInstance(result, str)
        self.assertTrue(result.strip())


@unittest.skipUnless(_DEPS, "requires mcp + httpx")
class LazyClientTests(unittest.TestCase):
    """``_get_client`` builds the singleton only on first use."""

    def test_client_built_once_from_config(self):
        with patch.object(server, "_client", None), patch.object(
            server, "get_config"
        ) as get_config, patch.object(server, "S7Client") as cls:
            first = server._get_client()
            second = server._get_client()
        self.assertIs(first, second)
        get_config.assert_called_once_with()
        cls.assert_called_once_with(get_config.return_value)


@unittest.skipUnless(_DEPS, "requires mcp + httpx")
class McpSurfaceTests(unittest.TestCase):
    """The exposed MCP surface: exactly the six read/author tools."""

    def test_the_six_tools_exist_and_are_callable(self):
        for name in EXPECTED_TOOLS:
            with self.subTest(tool=name):
                self.assertTrue(
                    callable(getattr(server, name)), f"{name} missing"
                )

    def test_no_publish_approve_apply_tools(self):
        for name in ("publish", "approve", "apply"):
            for attr in dir(server):
                self.assertNotIn(
                    name,
                    attr.lower(),
                    f"write-like attribute leaked into the module: {attr}",
                )

    def test_import_draft_has_no_status_parameter(self):
        params = list(inspect.signature(server.s7_import_draft).parameters)
        self.assertEqual(
            params,
            ["node_type", "schema", "schema_key", "schema_version", "overwrite"],
        )
        self.assertNotIn("status", params)
        self.assertNotIn("schema_status", params)

    def test_registered_tool_set_matches_module_functions(self):
        """The SDK registry (when introspectable) holds exactly the 6 tools."""
        tool_manager = getattr(server.mcp, "_tool_manager", None)
        registered = getattr(tool_manager, "_tools", None)
        if isinstance(registered, dict):
            self.assertEqual(set(registered), EXPECTED_TOOLS)


@unittest.skipUnless(_DEPS, "requires mcp + httpx")
class MainTests(unittest.TestCase):
    """``main()`` validates config first, then serves."""

    def test_main_runs_server_after_config_ok(self):
        with patch.object(server, "get_config") as get_config, patch.object(
            server.mcp, "run"
        ) as run:
            server.main()
        get_config.assert_called_once_with()
        run.assert_called_once_with()

    def test_main_propagates_config_exit_without_running(self):
        with patch.object(
            server, "get_config", side_effect=SystemExit(2)
        ), patch.object(server.mcp, "run") as run:
            with self.assertRaises(SystemExit) as cm:
                server.main()
        self.assertEqual(cm.exception.code, 2)
        run.assert_not_called()


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
        self.assertIs(server.MCPServer, expected)
        self.assertIsInstance(server.mcp, server.MCPServer)

    def test_fastmcp_fallback_branch(self):
        """Reloading the module with ``MCPServer`` unimportable lands on the
        ``FastMCP`` branch and still registers the full surface."""
        fake_mcpserver = types.ModuleType("mcp.server.mcpserver")
        # No MCPServer attribute: ``from ... import MCPServer`` raises
        # ImportError and the shim falls back to FastMCP.
        fake_fastmcp = types.ModuleType("mcp.server.fastmcp")
        _FakeFastMCP.instances = []
        fake_fastmcp.FastMCP = _FakeFastMCP

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
            importlib.reload(server)

        self.addCleanup(restore)
        with patch.dict(
            sys.modules,
            {
                "mcp.server.mcpserver": fake_mcpserver,
                "mcp.server.fastmcp": fake_fastmcp,
            },
        ):
            importlib.reload(server)

            self.assertIs(server.MCPServer, _FakeFastMCP)
            # First construction with ``version=`` fails (no such kwarg on
            # mcp 1.x) and the shim retries without it: exactly one instance.
            self.assertEqual(len(_FakeFastMCP.instances), 1)
            self.assertIs(server.mcp, _FakeFastMCP.instances[0])
            self.assertEqual(server.mcp.name, "s7-author")
            self.assertEqual(set(server.mcp.tools), EXPECTED_TOOLS)
            self.assertEqual(
                set(server.mcp.resources),
                {
                    "s7://catalog/{scope}",
                    "s7://datatypes",
                    "s7://examples/{scope}",
                    "s7://schemas/{node_type}/{key}/{version}",
                },
            )
            self.assertEqual(
                set(server.mcp.prompts),
                {"create_tramite_form", "author_catalog"},
            )


if __name__ == "__main__":
    unittest.main()
