"""Tests for ``s7_mcp.server`` — tools, resources, payloads and the SDK shim.

The ``@mcp.tool()``/``@mcp.resource()``/``@mcp.prompt()`` decorators register
the callables but return them unchanged, so every handler is invoked directly
and the API client is injected by patching ``_get_client``.
"""

import copy
import importlib
import importlib.util
import inspect
import json
import re
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

# Pure-Python modules — importable without mcp/httpx, so the grammar and
# prompt-content tests below run even when the optional dependencies are
# not installed.
from s7_mcp.catalog_grammar import (
    CATALOG_GRAMMAR,
    validate_example_import_objects,
)
from s7_mcp.prompts import AUTHOR_CATALOG_PROMPT, CREATE_TRAMITE_FORM_PROMPT

EXPECTED_TOOLS = {
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
class GetDraftTests(unittest.TestCase):
    """``s7_get_draft`` maps to the draft-read endpoint with ``project_id``.

    The project id is read off the client (resolved from ``S7_PROJECT_ID``)
    and forwarded as a query param — the tool itself takes no such argument,
    the same rule as the write tools' body injection.
    """

    def setUp(self):
        self.client = MagicMock()
        self.client.get_json.return_value = {"ok": True}
        self.client.project_id = None
        patcher = patch.object(
            server, "_get_client", return_value=self.client
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _call(self):
        return self.client.get_json.call_args

    def test_draft_path_with_quoted_segments(self):
        server.s7_get_draft("node/type", "key?1", "v%2")
        self.assertEqual(
            self._call()[0][0],
            "schema/draft/node%2Ftype/key%3F1/v%252/",
        )

    def test_project_id_sent_as_query_param_when_configured(self):
        project_id = "12345678-1234-5678-1234-567812345678"
        self.client.project_id = project_id
        server.s7_get_draft("survey", "k", "v1")
        self.assertEqual(
            self._call()[1]["params"], {"project_id": project_id}
        )

    def test_project_id_is_none_when_not_configured(self):
        """With no ``S7_PROJECT_ID`` the param is forwarded as ``None`` and
        the client drops it from the query string."""
        server.s7_get_draft("survey", "k", "v1")
        self.assertEqual(self._call()[1]["params"], {"project_id": None})

    def test_tool_takes_no_project_id_argument(self):
        # ``ctx`` is a trailing SDK-injected parameter (per-request
        # credential resolution) — it never reaches the input schema.
        self.assertEqual(
            list(inspect.signature(server.s7_get_draft).parameters),
            ["node_type", "key", "version", "ctx"],
        )

    def test_draft_envelope_propagates_verbatim(self):
        envelope = {
            "data": {"key": "k", "nodes": []},
            "meta": {
                "node_type": "survey",
                "key": "k",
                "version": "v1",
                "status": "draft",
            },
        }
        self.client.get_json.return_value = envelope
        self.assertIs(server.s7_get_draft("survey", "k", "v1"), envelope)

    def test_not_found_envelope_propagates_verbatim(self):
        envelope = {
            "type": "schema_not_found",
            "title": "Schema not found",
            "detail": "no draft schema",
            "status": 404,
        }
        self.client.get_json.return_value = envelope
        self.assertIs(server.s7_get_draft("survey", "k", "v1"), envelope)


@unittest.skipUnless(_DEPS, "requires mcp + httpx")
class GetSchemaTests(unittest.TestCase):
    """``s7_get_schema`` maps to the published-read endpoint with ``project_id``.

    The project id is read off the client (resolved from ``S7_PROJECT_ID``)
    and forwarded as a query param — the tool itself takes no such argument,
    the same rule as ``s7_get_draft`` and the write tools' body injection.
    """

    def setUp(self):
        self.client = MagicMock()
        self.client.get_json.return_value = {"ok": True}
        self.client.project_id = None
        patcher = patch.object(
            server, "_get_client", return_value=self.client
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _call(self):
        return self.client.get_json.call_args

    def test_schema_path_with_quoted_segments(self):
        server.s7_get_schema("node/type", "key?1", "v%2")
        self.assertEqual(
            self._call()[0][0],
            "schema/node%2Ftype/key%3F1/v%252/",
        )

    def test_project_id_sent_as_query_param_when_configured(self):
        project_id = "12345678-1234-5678-1234-567812345678"
        self.client.project_id = project_id
        server.s7_get_schema("survey", "k", "v1")
        self.assertEqual(
            self._call()[1]["params"], {"project_id": project_id}
        )

    def test_project_id_is_none_when_not_configured(self):
        """With no ``S7_PROJECT_ID`` the param is forwarded as ``None`` and
        the client drops it from the query string."""
        server.s7_get_schema("survey", "k", "v1")
        self.assertEqual(self._call()[1]["params"], {"project_id": None})

    def test_tool_takes_no_project_id_argument(self):
        # ``ctx`` is a trailing SDK-injected parameter (per-request
        # credential resolution) — it never reaches the input schema.
        self.assertEqual(
            list(inspect.signature(server.s7_get_schema).parameters),
            ["node_type", "key", "version", "ctx"],
        )

    def test_schema_envelope_propagates_verbatim(self):
        envelope = {
            "data": {"key": "k", "nodes": []},
            "meta": {
                "node_type": "survey",
                "key": "k",
                "version": "v1",
            },
        }
        self.client.get_json.return_value = envelope
        self.assertIs(server.s7_get_schema("survey", "k", "v1"), envelope)

    def test_not_found_envelope_propagates_verbatim(self):
        envelope = {
            "type": "schema_not_found",
            "title": "Schema Not Found",
            "detail": "Schema not found, not published, or node type mismatch",
            "status": 404,
        }
        self.client.get_json.return_value = envelope
        self.assertIs(server.s7_get_schema("survey", "k", "v1"), envelope)


@unittest.skipUnless(_DEPS, "requires mcp + httpx")
class DeleteDraftTests(unittest.TestCase):
    """``s7_delete_draft`` maps to the draft-delete endpoint via ``delete_json``.

    The project id is read off the client (resolved from ``S7_PROJECT_ID``)
    and forwarded as a query param — a DELETE carries no body, so the id
    travels as ``?project_id=`` exactly like the reads. The tool itself
    takes no such argument.
    """

    def setUp(self):
        self.client = MagicMock()
        self.client.delete_json.return_value = {"deleted": True}
        self.client.project_id = None
        patcher = patch.object(
            server, "_get_client", return_value=self.client
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _call(self):
        return self.client.delete_json.call_args

    def test_draft_path_with_quoted_segments(self):
        server.s7_delete_draft("node/type", "key?1", "v%2")
        self.assertEqual(
            self._call()[0][0],
            "schema/draft/node%2Ftype/key%3F1/v%252/",
        )

    def test_project_id_sent_as_query_param_when_configured(self):
        project_id = "12345678-1234-5678-1234-567812345678"
        self.client.project_id = project_id
        server.s7_delete_draft("survey", "k", "v1")
        self.assertEqual(
            self._call()[1]["params"], {"project_id": project_id}
        )

    def test_project_id_is_none_when_not_configured(self):
        """With no ``S7_PROJECT_ID`` the param is forwarded as ``None`` and
        the client drops it from the query string."""
        server.s7_delete_draft("survey", "k", "v1")
        self.assertEqual(self._call()[1]["params"], {"project_id": None})

    def test_tool_takes_no_project_id_or_status_argument(self):
        # ``ctx`` is a trailing SDK-injected parameter (per-request
        # credential resolution) — it never reaches the input schema.
        self.assertEqual(
            list(inspect.signature(server.s7_delete_draft).parameters),
            ["node_type", "key", "version", "ctx"],
        )

    def test_delete_reaches_delete_json_not_get_or_post(self):
        """The destructive op must reach ``delete_json`` — a ``get_json``
        call would silently turn the delete into a read."""
        server.s7_delete_draft("survey", "k", "v1")
        self.client.delete_json.assert_called_once()
        self.client.get_json.assert_not_called()
        self.client.post_json.assert_not_called()

    def test_deleted_envelope_propagates_verbatim(self):
        envelope = {
            "deleted": True,
            "meta": {
                "node_type": "survey",
                "key": "k",
                "version": "v1",
            },
        }
        self.client.delete_json.return_value = envelope
        self.assertIs(server.s7_delete_draft("survey", "k", "v1"), envelope)

    def test_not_found_envelope_propagates_verbatim(self):
        """A missing or non-draft schema answers the same RFC-7807 404 as
        the draft read — the envelope passes through untouched."""
        envelope = {
            "type": "schema_not_found",
            "title": "Schema not found",
            "detail": "no draft schema",
            "status": 404,
        }
        self.client.delete_json.return_value = envelope
        self.assertIs(server.s7_delete_draft("survey", "k", "v1"), envelope)


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
class CatalogUsageTests(unittest.TestCase):
    """``s7_catalog_usage`` forwards the dual identifier to ``catalog/usage/``.

    ``model``/``pk`` travel as base params and ``fields`` is flattened
    into the same dict — each natural-key component becomes its own
    query param, and a ``pk=None`` stays inside the params dict (the
    client drops None-valued params when building the query string).
    """

    def setUp(self):
        self.client = MagicMock()
        self.client.get_json.return_value = {"ok": True}
        patcher = patch.object(
            server, "_get_client", return_value=self.client
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _call(self):
        return self.client.get_json.call_args

    def test_natural_key_fields_flattened_into_params(self):
        result = server.s7_catalog_usage(
            "schemas.nodetype", fields={"name": "field"}
        )
        self.assertIs(result, self.client.get_json.return_value)
        self.assertEqual(self._call()[0][0], "catalog/usage/")
        self.assertEqual(
            self._call()[1]["params"],
            {"model": "schemas.nodetype", "pk": None, "name": "field"},
        )

    def test_pk_arm_sends_pk_and_no_nk_components(self):
        pk = "12345678-1234-5678-1234-567812345678"
        server.s7_catalog_usage("schemas.domain", pk=pk)
        self.assertEqual(
            self._call()[1]["params"],
            {"model": "schemas.domain", "pk": pk},
        )

    def test_no_fields_sends_only_model_and_pk(self):
        """``fields=None`` adds nothing — the params dict carries just
        the base identifier entries."""
        server.s7_catalog_usage("schemas.nodetype")
        self.assertEqual(
            self._call()[1]["params"],
            {"model": "schemas.nodetype", "pk": None},
        )

    def test_none_pk_stays_in_params_for_client_to_drop(self):
        """``pk=None`` is forwarded inside the params dict — the
        client, not the tool, filters None-valued params."""
        server.s7_catalog_usage("schemas.nodetype", fields={"name": "x"})
        self.assertIn("pk", self._call()[1]["params"])
        self.assertIsNone(self._call()[1]["params"]["pk"])

    def test_multi_component_natural_key_flattens(self):
        """A multi-position natural key (``domainitem``:
        ``domain``+``value``) lands flat in the params dict — every
        component is its own query param, same vocabulary as the
        delete objects."""
        server.s7_catalog_usage(
            "schemas.domainitem",
            fields={"domain": "dom_x", "value": "v1"},
        )
        self.assertEqual(
            self._call()[1]["params"],
            {
                "model": "schemas.domainitem",
                "pk": None,
                "domain": "dom_x",
                "value": "v1",
            },
        )

    def test_fields_cannot_override_base_params(self):
        """A ``fields`` key named ``model`` or ``pk`` is dropped — it
        would silently redirect the query to a different model or flip
        the identifier arm. ``fields`` carries natural-key components
        only."""
        server.s7_catalog_usage(
            "schemas.nodetype", pk="abc",
            fields={"model": "schemas.domain", "pk": "xyz", "name": "f"},
        )
        self.assertEqual(
            self._call()[1]["params"],
            {"model": "schemas.nodetype", "pk": "abc", "name": "f"},
        )

    def test_success_envelope_propagates_verbatim(self):
        envelope = {
            "implemented": True,
            "usage": {"nodes": 1, "projects": ["app-oaxaca"]},
        }
        self.client.get_json.return_value = envelope
        self.assertIs(
            server.s7_catalog_usage("schemas.nodetype", fields={"name": "f"}),
            envelope,
        )

    def test_not_found_envelope_propagates_verbatim(self):
        envelope = {
            "error": "catalog_object_not_found",
            "model": "schemas.nodetype",
            "identifier": {"natural_key": {"name": "field"}},
        }
        self.client.get_json.return_value = envelope
        self.assertIs(server.s7_catalog_usage("schemas.nodetype"), envelope)

    def test_invalid_request_envelope_propagates_verbatim(self):
        envelope = {
            "error": "invalid_request",
            "message": (
                "identify the object by 'pk' or natural-key parameters"
            ),
        }
        self.client.get_json.return_value = envelope
        self.assertIs(server.s7_catalog_usage("schemas.nodetype"), envelope)

    def test_tool_signature_is_model_pk_fields(self):
        """Thin-wrapper contract: exactly ``(model, pk, fields)`` — no
        extra knobs, same arity as the delete-object identifier."""
        self.assertEqual(
            list(inspect.signature(server.s7_catalog_usage).parameters),
            ["model", "pk", "fields", "ctx"],
        )


@unittest.skipUnless(_DEPS, "requires mcp + httpx")
class ResourceTests(unittest.TestCase):
    """Resources mirror tool paths and propagate envelopes per contract."""

    def setUp(self):
        self.client = MagicMock()
        self.client.project_id = None
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

    def test_schema_resource_sends_project_id_query_param(self):
        """The resource hits the same endpoint as ``s7_get_schema`` and
        forwards ``S7_PROJECT_ID`` as ``?project_id=`` the same way."""
        project_id = "12345678-1234-5678-1234-567812345678"
        self.client.project_id = project_id
        schema = {"nodes": []}
        self.client.get_json.return_value = {"data": schema, "meta": {}}
        self.assertIs(server.schema_resource("survey", "k", "v1"), schema)
        self.assertEqual(
            self.client.get_json.call_args[1]["params"],
            {"project_id": project_id},
        )

    def test_schema_resource_project_id_is_none_when_not_configured(self):
        """With no ``S7_PROJECT_ID`` the param is forwarded as ``None`` and
        the client drops it from the query string."""
        self.client.get_json.return_value = {"data": {}, "meta": {}}
        server.schema_resource("survey", "k", "v1")
        self.assertEqual(
            self.client.get_json.call_args[1]["params"], {"project_id": None}
        )

    def test_schema_resource_takes_no_project_id_argument(self):
        # ``ctx`` is a trailing SDK-injected parameter (per-request
        # credential resolution) — it never reaches the input schema.
        self.assertEqual(
            list(inspect.signature(server.schema_resource).parameters),
            ["node_type", "key", "version", "ctx"],
        )

    def test_schema_resource_error_envelope_propagates_verbatim(self):
        envelope = {"type": "schema_not_found", "status": 404}
        self.client.get_json.return_value = envelope
        self.assertIs(server.schema_resource("nt", "k", "v"), envelope)

    def test_catalog_grammar_resource_returns_static_document(self):
        """The resource serves the versioned module verbatim — the static
        document has no REST endpoint, so the client is never touched."""
        self.assertIs(server.catalog_grammar_resource(), CATALOG_GRAMMAR)
        self.client.get_json.assert_not_called()

    def test_catalog_grammar_resource_takes_no_arguments(self):
        self.assertEqual(
            list(
                inspect.signature(server.catalog_grammar_resource).parameters
            ),
            [],
        )


class CatalogGrammarDataTests(unittest.TestCase):
    """``CATALOG_GRAMMAR`` content checks.

    No ``_DEPS`` guard: ``s7_mcp.catalog_grammar`` is pure Python and
    importable without the optional mcp/httpx dependencies.
    """

    EXPECTED_MODELS = {
        "schemas.domain",
        "schemas.domainitem",
        "schemas.nodetype",
        "schemas.nodetypecomposition",
        "schemas.nodetypevariant",
        "schemas.attributedef",
    }

    EXPECTED_FKS = {
        "schemas.domain": set(),
        "schemas.domainitem": {"domain"},
        "schemas.nodetype": set(),
        "schemas.nodetypecomposition": {"parent_type", "child_type"},
        "schemas.nodetypevariant": {"node_type", "props_node_type"},
        "schemas.attributedef": {"node_type", "data_type", "domain"},
    }

    def test_exactly_the_six_authorizable_models(self):
        self.assertEqual(set(CATALOG_GRAMMAR["models"]), self.EXPECTED_MODELS)
        # schemas.datatype is referenced by attributedef.data_type but is
        # not authorizable through the payload, so it is not listed.
        self.assertNotIn("schemas.datatype", CATALOG_GRAMMAR["models"])

    def test_each_model_carries_natural_key_fields_fks(self):
        for label, model in CATALOG_GRAMMAR["models"].items():
            with self.subTest(model=label):
                self.assertIsInstance(model["natural_key"], list)
                self.assertTrue(model["natural_key"])
                self.assertIsInstance(model["fields"], dict)
                self.assertIsInstance(model["fks"], dict)

    def test_fk_fields_live_in_fks_not_fields(self):
        """Foreign-key field names are documented under ``fks`` only —
        a scalar slot in ``fields`` would mislead the fixture shape."""
        for label, model in CATALOG_GRAMMAR["models"].items():
            with self.subTest(model=label):
                self.assertEqual(set(model["fks"]), self.EXPECTED_FKS[label])
                self.assertFalse(set(model["fields"]) & set(model["fks"]))

    def test_scalar_field_entries_shape(self):
        for label, model in CATALOG_GRAMMAR["models"].items():
            for name, field in model["fields"].items():
                with self.subTest(model=label, field=name):
                    self.assertIn(
                        field["type"], ("string", "integer", "boolean")
                    )
                    self.assertIsInstance(field["required"], bool)
                    self.assertIsInstance(field["nullable"], bool)

    def test_fk_entries_shape(self):
        for label, model in CATALOG_GRAMMAR["models"].items():
            for name, fk in model["fks"].items():
                with self.subTest(model=label, field=name):
                    self.assertTrue(fk["model"].startswith("schemas."))
                    self.assertIsInstance(fk["natural_key"], list)
                    self.assertEqual(fk["shape"], "[natural_key]")
                    self.assertIsInstance(fk["required"], bool)
                    self.assertIsInstance(fk["nullable"], bool)

    def test_real_fixture_field_names_spot_checks(self):
        """Field names must be the real model fields — the whole point of
        the resource is ending discovery-by-rejection."""
        attributedef = CATALOG_GRAMMAR["models"]["schemas.attributedef"]
        self.assertIn("is_required", attributedef["fields"])
        self.assertIn("is_common", attributedef["fields"])
        self.assertIn("json_key", attributedef["fields"])
        self.assertIn("variant_key", attributedef["fields"])
        self.assertIn("data_type", attributedef["fks"])
        self.assertEqual(
            attributedef["natural_key"],
            ["node_type", "json_key", "variant_key"],
        )

        composition = CATALOG_GRAMMAR["models"]["schemas.nodetypecomposition"]
        self.assertIn("collection_key", composition["fields"])
        self.assertIn("parent_type", composition["fks"])
        self.assertIn("child_type", composition["fks"])
        self.assertEqual(
            composition["natural_key"],
            ["parent_type", "child_type", "collection_key"],
        )

        variant = CATALOG_GRAMMAR["models"]["schemas.nodetypevariant"]
        self.assertIn("discriminator_attr", variant["fields"])
        self.assertIn("props_node_type", variant["fks"])
        self.assertEqual(
            variant["natural_key"],
            ["node_type", "variant_key", "discriminator_attr"],
        )

        domainitem = CATALOG_GRAMMAR["models"]["schemas.domainitem"]
        self.assertEqual(domainitem["natural_key"], ["domain", "value"])
        self.assertIn("domain", domainitem["fks"])

        nodetype = CATALOG_GRAMMAR["models"]["schemas.nodetype"]
        for name in (
            "name", "label", "is_container", "is_root", "json_scope",
            "default_json_key", "is_json_map", "json_scalar_attribute",
        ):
            self.assertIn(name, nodetype["fields"])

    def test_payload_envelope_semantics_documented(self):
        self.assertTrue(CATALOG_GRAMMAR["format"].strip())
        for key in ("objects", "pk", "fields", "semantics"):
            self.assertIn(key, CATALOG_GRAMMAR["payload"])
        # The update usage gate must be discoverable in the prose the
        # agent reads — a field-changing update on an implemented row is
        # rejected, creates and no-op updates are exempt.
        semantics = CATALOG_GRAMMAR["payload"]["semantics"]
        self.assertIn("catalog_object_in_use", semantics)
        self.assertIn("'usage'", semantics)

    def test_usage_check_section_shape(self):
        """The ``usage_check`` section is a top-level sibling carrying
        the pre-check prose — endpoint, tool, dual identifier and the
        gate it mirrors must all be discoverable in it."""
        section = CATALOG_GRAMMAR["usage_check"]
        self.assertIsInstance(section, str)
        for needle in (
            "s7_catalog_usage", "catalog/usage/",
            "catalog_object_in_use", "implemented",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, section)

    def test_document_is_json_serializable(self):
        """Served with ``mime_type="application/json"`` — the dict must
        round-trip through ``json.dumps`` unchanged."""
        self.assertEqual(
            json.loads(json.dumps(CATALOG_GRAMMAR)), CATALOG_GRAMMAR
        )

    def test_resolved_vs_import_section_shape(self):
        """The read-vs-write table is a top-level sibling of ``models``:
        a ``mapping`` list of resolved→fixture rows plus a worked
        ``example``."""
        section = CATALOG_GRAMMAR["resolved_vs_import"]
        self.assertIsInstance(section["summary"], str)
        self.assertTrue(section["summary"].strip())
        self.assertIsInstance(section["mapping"], list)
        self.assertTrue(section["mapping"])
        for entry in section["mapping"]:
            with self.subTest(entry=entry.get("resolved")):
                self.assertTrue(entry["resolved"].strip())
                self.assertTrue(entry["fixture"].strip())
        example = section["example"]
        self.assertIn("resolved", example)
        self.assertIn("import", example)
        self.assertIsInstance(example["import"]["objects"], list)
        self.assertTrue(example["import"]["objects"])

    def test_resolved_vs_import_covers_the_catalog_blocks(self):
        """Every resolved block that confused pilot agents has a mapping
        row, and every fixture target names an authorizable model."""
        section = CATALOG_GRAMMAR["resolved_vs_import"]
        resolved_text = " ".join(
            entry["resolved"] for entry in section["mapping"]
        )
        for block in (
            "root_node_type", "node_types", "attributes", "data_type",
            "domain", "server_generated", "variants", "compositions",
            "domains",
        ):
            with self.subTest(block=block):
                self.assertIn(block, resolved_text)
        fixture_text = " ".join(
            entry["fixture"] for entry in section["mapping"]
        )
        for label in self.EXPECTED_MODELS:
            with self.subTest(model=label):
                self.assertIn(label, fixture_text)

    def test_resolved_vs_import_documents_the_field_renames(self):
        """The renames behind the cryptic pilot errors are spelled out:
        ``required``→``is_required``, ``common``→``is_common`` and the
        natural-key list shape of the ``data_type``/``domain`` FKs."""
        section = CATALOG_GRAMMAR["resolved_vs_import"]
        text = json.dumps(section)
        for needle in (
            "is_required", "is_common", "is_root", "json_scope",
            "natural-key",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)

    def test_resolved_vs_import_example_resolved_fragment_shape(self):
        """The resolved fragment mirrors the read contract: node types
        carry attributes/variants/compositions and attributes carry the
        flat read fields."""
        resolved = CATALOG_GRAMMAR["resolved_vs_import"]["example"][
            "resolved"
        ]
        self.assertIn("root_node_type", resolved)
        self.assertIn("node_types", resolved)
        for node_type in resolved["node_types"]:
            for key in (
                "name", "label", "is_root", "attributes", "variants",
                "compositions",
            ):
                self.assertIn(key, node_type)
            for attr in node_type["attributes"]:
                for key in (
                    "json_key", "data_type", "domain", "required",
                    "server_generated", "common", "variant_key",
                ):
                    self.assertIn(key, attr)

    def test_resolved_vs_import_example_objects_are_payload_valid(self):
        """The worked example must survive the import contract — shape
        and completeness checked end to end by the shared validator
        ``catalog_grammar.validate_example_import_objects``: real
        labels, no ``id``/``version`` inside ``fields``, every
        natural-key and required component present, FK values shaped
        as non-empty ``[natural_key]`` string lists or null only where
        nullable, and scalar ``fields`` positions never carrying a list
        and null only where nullable. The strict semantics live next to
        the document; the backend suite invokes the same validator with
        the live ``CATALOG_MODELS`` whitelist, this side passes the
        static ``EXPECTED_MODELS`` copy."""
        objects = CATALOG_GRAMMAR["resolved_vs_import"]["example"][
            "import"
        ]["objects"]
        errors = validate_example_import_objects(
            objects,
            CATALOG_GRAMMAR["models"],
            allowed_models=self.EXPECTED_MODELS,
        )
        self.assertEqual(errors, [])

    def test_example_import_objects_reject_residual_laxity(self):
        """Mutation guard pinning the two residual rejections the
        validator now shares with the real import: an fk carrying
        ``[]`` — the empty natural-key reference that never resolves
        to a target row — and ``None`` on a scalar documented
        ``nullable: False``. Legitimate nulls stay accepted: the
        example already carries ``variant_key: None`` (nullable scalar)
        and ``domain: None`` on an attributedef (nullable fk); the
        added ``default_json_key: None`` (nullable scalar) and
        ``props_node_type: None`` (nullable fk) must not error either —
        each failure would be reported below as a third error. Only an
        in-memory copy is mutated; ``CATALOG_GRAMMAR`` is never
        touched."""
        objects = copy.deepcopy(
            CATALOG_GRAMMAR["resolved_vs_import"]["example"]["import"][
                "objects"
            ]
        )
        # Legitimate nulls — nullable scalar, nullable fk.
        objects[0]["fields"]["default_json_key"] = None
        objects.append(
            {
                "model": "schemas.nodetypevariant",
                "fields": {
                    "node_type": ["example_root"],
                    "variant_key": "input",
                    "discriminator_attr": "type",
                    "props_node_type": None,
                },
            }
        )
        # The two hardened rejections, both on the first domainitem.
        objects[2]["fields"]["domain"] = []
        objects[2]["fields"]["label"] = None
        errors = validate_example_import_objects(
            objects,
            CATALOG_GRAMMAR["models"],
            allowed_models=self.EXPECTED_MODELS,
        )
        self.assertEqual(len(errors), 2, msg=f"errors: {errors}")
        fk_error = next(e for e in errors if "'domain'" in e)
        scalar_error = next(e for e in errors if "'label'" in e)
        self.assertIn("object[2] (schemas.domainitem)", fk_error)
        self.assertIn("object[2] (schemas.domainitem)", scalar_error)
        joined = "\n".join(errors)
        for legit in ("default_json_key", "props_node_type", "variant_key"):
            self.assertNotIn(legit, joined)

    def test_example_import_objects_reject_fk_ref_shape(self):
        """Mutation guard pinning the two fk-reference rejections the
        validator now shares with the real import over an already
        non-empty list of strings: a whitespace-only/empty component
        (``["  "]`` — whitespace is never deliberate identity, the
        same rule the import applies to natural-key components) and
        an arity mismatch against the target's documented
        ``natural_key`` (``["a", "b"]`` for an fk whose target
        declares a one-component key — it can never match a real
        natural-key tuple). The checks are independent: ``["", "x"]``
        on an arity-1 fk reports both. Legitimate references stay
        accepted: ``["yes_no"]`` already rides the example, the
        appended ``schemas.nodetypevariant`` carries
        ``props_node_type: ["example_root"]`` (a nullable fk with a
        valid one-component reference) and ``[" yes_no "]`` on the
        second domainitem is kept — non-whitespace padding would
        resolve if a row were keyed by that literal, so the guard
        must not over-reject. Only in-memory copies are mutated;
        ``CATALOG_GRAMMAR`` is never touched."""
        objects = copy.deepcopy(
            CATALOG_GRAMMAR["resolved_vs_import"]["example"]["import"][
                "objects"
            ]
        )
        # Legitimate refs — a nullable fk carrying a valid reference,
        # and a padded-but-non-whitespace component the guard keeps.
        objects.append(
            {
                "model": "schemas.nodetypevariant",
                "fields": {
                    "node_type": ["example_root"],
                    "variant_key": "input",
                    "discriminator_attr": "type",
                    "props_node_type": ["example_root"],
                },
            }
        )
        objects[3]["fields"]["domain"] = [" yes_no "]

        whitespace = copy.deepcopy(objects)
        whitespace[2]["fields"]["domain"] = ["  "]
        errors = validate_example_import_objects(
            whitespace,
            CATALOG_GRAMMAR["models"],
            allowed_models=self.EXPECTED_MODELS,
        )
        self.assertEqual(len(errors), 1, msg=f"errors: {errors}")
        self.assertIn("'domain'", errors[0])
        self.assertIn("object[2] (schemas.domainitem)", errors[0])
        self.assertIn("'  '", errors[0])

        arity = copy.deepcopy(objects)
        arity[2]["fields"]["domain"] = ["a", "b"]
        errors = validate_example_import_objects(
            arity,
            CATALOG_GRAMMAR["models"],
            allowed_models=self.EXPECTED_MODELS,
        )
        self.assertEqual(len(errors), 1, msg=f"errors: {errors}")
        self.assertIn("'domain'", errors[0])
        self.assertIn("object[2] (schemas.domainitem)", errors[0])
        self.assertIn("arity 2", errors[0])
        self.assertIn("arity 1", errors[0])

        # The checks are independent — one value can fail both.
        both = copy.deepcopy(objects)
        both[2]["fields"]["domain"] = ["", "x"]
        errors = validate_example_import_objects(
            both,
            CATALOG_GRAMMAR["models"],
            allowed_models=self.EXPECTED_MODELS,
        )
        self.assertEqual(len(errors), 2, msg=f"errors: {errors}")
        joined = "\n".join(errors)
        self.assertIn("'domain'", joined)
        self.assertIn("object[2] (schemas.domainitem)", joined)
        self.assertIn("''", joined)
        self.assertIn("arity", joined)
        for legit in ("props_node_type", "object[3]", "object[6]"):
            self.assertNotIn(legit, joined)

    def test_resolved_vs_import_example_authors_server_generated_defs(self):
        """``server_generated`` flags value emission, not authoring: the
        resolved example carries one and every resolved attribute —
        it included — has its attributedef authored on the import
        side; declaring the def is what makes the key exist."""
        example = CATALOG_GRAMMAR["resolved_vs_import"]["example"]
        resolved = {
            attr["json_key"]
            for node_type in example["resolved"]["node_types"]
            for attr in node_type["attributes"]
        }
        generated = {
            attr["json_key"]
            for node_type in example["resolved"]["node_types"]
            for attr in node_type["attributes"]
            if attr["server_generated"]
        }
        self.assertTrue(generated)
        authored = {
            obj["fields"]["json_key"]
            for obj in example["import"]["objects"]
            if obj["model"] == "schemas.attributedef"
        }
        self.assertLessEqual(generated, authored)
        self.assertEqual(resolved, authored)

    def test_server_generated_mapping_row_targets_attributedef(self):
        """The mapping row for the flag must point at the authored
        attributedef row — a "(no object)" fixture target or a
        'never author' note reintroduces the misleading lesson."""
        mapping = CATALOG_GRAMMAR["resolved_vs_import"]["mapping"]
        row = next(
            entry for entry in mapping
            if "server_generated" in entry["resolved"]
        )
        self.assertEqual(row["fixture"], "schemas.attributedef")
        self.assertIn("authored", row["notes"].lower())
        self.assertNotIn("never author", row["notes"].lower())

    def test_expressions_section_shape(self):
        """``expressions`` is a top-level sibling of ``models``: a usage
        summary, the when-to-use rule, the ``syntax`` subset table, an
        explicit not-supported list, worked examples and the
        validation / canonical-grammar pointers."""
        section = CATALOG_GRAMMAR["expressions"]
        self.assertIsInstance(section, dict)
        for key in (
            "summary", "when_to_use", "syntax", "not_supported",
            "examples", "validation", "canonical_grammar",
        ):
            self.assertIn(key, section)
        for key in ("paths", "literals", "operators", "grouping"):
            self.assertIn(key, section["syntax"])
        self.assertIsInstance(section["not_supported"], list)
        self.assertTrue(section["not_supported"])
        for entry in section["not_supported"]:
            with self.subTest(entry=entry[:40]):
                self.assertIsInstance(entry, str)
                self.assertTrue(entry.strip())

    def test_expressions_scopes_tokens_to_expression_attributes(self):
        """The section must teach the boundary agents get wrong:
        ``{{expr}}`` applies only to attributes whose ``data_type`` is
        ``expression`` (every other datatype takes its value literally)
        while ``conditional``-typed attributes — ``show_if`` /
        ``enabled_if`` — stay structured values."""
        text = json.dumps(CATALOG_GRAMMAR["expressions"])
        for needle in (
            "data_type", "expression", "conditional",
            "show_if", "enabled_if",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)

    def test_expressions_examples_shape(self):
        """Worked examples are the teaching payload: a non-empty list of
        ``prop``/``value``/``notes`` dicts whose ``value`` strings carry
        ``{{`` tokens (a plain literal is allowed for contrast). The
        backend drift suite feeds every value through the real
        ``{{expr}}`` validator, so grammar correctness is checked there."""
        examples = CATALOG_GRAMMAR["expressions"]["examples"]
        self.assertIsInstance(examples, list)
        self.assertTrue(examples)
        token_bearing = 0
        for example in examples:
            with self.subTest(prop=example.get("prop")):
                self.assertIsInstance(example, dict)
                for key in ("prop", "value", "notes"):
                    self.assertIn(key, example)
                self.assertIsInstance(example["value"], str)
                self.assertTrue(example["value"].strip())
                if "{{" in example["value"]:
                    token_bearing += 1
        # A section teaching {{expr}} without a single token example
        # would teach nothing.
        self.assertTrue(token_bearing)

    def test_expressions_points_at_canonical_surfaces(self):
        """``canonical_grammar`` defers to the ``expression`` entry of
        the resolved catalog's ``datatypes`` block (the surface that
        actually carries the grammar — ``s7://datatypes`` lists names
        and descriptions only) and ``validation`` routes errors through
        ``s7_validate_schema`` — the section summarizes usage; it is
        not a second grammar or a separate check endpoint."""
        section = CATALOG_GRAMMAR["expressions"]
        self.assertIn("s7://catalog/{scope}", section["canonical_grammar"])
        self.assertIn("datatypes", section["canonical_grammar"])
        self.assertIn("s7_validate_schema", section["validation"])


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


class PromptContentTests(unittest.TestCase):
    """Prompt template content checks.

    No ``_DEPS`` guard: ``s7_mcp.prompts`` holds pure string constants and
    is importable without the optional mcp/httpx dependencies — the
    regression guards must not skip silently in minimal environments.
    """

    def test_tramite_prompt_catalog_example_is_a_resolvable_scope(self):
        """Regression guard: the step-1 catalog literal must be a real
        ``json_scope`` — ``survey`` is the root node type's *name* and
        resolves to ``scope_not_found`` every time."""
        self.assertIn("`s7://catalog/survey_root`", CREATE_TRAMITE_FORM_PROMPT)
        # ``survey_root`` carries the broken ``survey`` literal as a prefix,
        # so a plain substring check would false-positive — the broken form
        # is ``survey`` not followed by another word character.
        self.assertIsNone(
            re.search(
                r"s7://catalog/survey(?![\w])", CREATE_TRAMITE_FORM_PROMPT
            )
        )

    def test_prompts_document_scope_discovery_via_the_error_envelope(self):
        """Both workflows name ``available_scopes`` on ``scope_not_found``
        as the supported way to discover valid scope names."""
        for prompt in (CREATE_TRAMITE_FORM_PROMPT, AUTHOR_CATALOG_PROMPT):
            with self.subTest(prompt=prompt.splitlines()[0][:50]):
                self.assertIn("scope_not_found", prompt)
                self.assertIn("available_scopes", prompt)

    def test_author_catalog_warns_resolved_is_a_read_shape(self):
        """Agents must not resubmit the resolved catalog as the import
        payload — the prompt states the read-vs-write gap and points at
        the ``resolved_vs_import`` section of ``s7://catalog-grammar``."""
        self.assertIn("*read* shape", AUTHOR_CATALOG_PROMPT)
        self.assertIn("fixture objects", AUTHOR_CATALOG_PROMPT)
        self.assertIn("resolved_vs_import", AUTHOR_CATALOG_PROMPT)

    def test_tramite_prompt_draft_step_drops_root_version_and_status_requirements(self):
        """The DRAFT step no longer requires a root ``version`` or a
        ``"status": "draft"`` field on the authored object — the canonical
        version lives on the declared ``natural_version`` attribute and the
        backend forces draft. ``status`` still appears as a *prohibition*
        ("Never author a `status` field"), so the guard pins the old
        requirement literals, not the word."""
        self.assertNotIn(
            "MUST include `key`, `version`", CREATE_TRAMITE_FORM_PROMPT
        )
        self.assertNotIn('"status": "draft"', CREATE_TRAMITE_FORM_PROMPT)
        self.assertIn(
            "Never author a `status` field", CREATE_TRAMITE_FORM_PROMPT
        )

    def test_tramite_prompt_names_natural_version_as_canonical_source(self):
        """The DRAFT step instructs declaring the version on the catalog
        attribute whose ``data_type`` is ``natural_version`` (typically
        ``metadata[].version``), with root ``version`` as a tolerated alias
        and ``schema_version`` as the overriding tool argument."""
        self.assertIn("natural_version", CREATE_TRAMITE_FORM_PROMPT)
        self.assertIn("metadata[].version", CREATE_TRAMITE_FORM_PROMPT)
        self.assertIn("alias", CREATE_TRAMITE_FORM_PROMPT)
        self.assertIn(
            "the declared attribute wins", CREATE_TRAMITE_FORM_PROMPT
        )
        self.assertIn(
            "`schema_version` tool argument", CREATE_TRAMITE_FORM_PROMPT
        )

    def test_tramite_prompt_teaches_expression_token_values(self):
        """The DRAFT step teaches the ``{{expr}}`` token dialect for
        ``expression``-typed attributes and defers to the ``expressions``
        section of ``s7://catalog-grammar`` for the subset — the guidance
        added so agents stop guessing the grammar."""
        for needle in (
            "{{expr}}", "expression", "expressions",
            "s7://catalog-grammar",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, CREATE_TRAMITE_FORM_PROMPT)

    def test_tramite_prompt_keeps_conditionals_structured(self):
        """Same step draws the boundary: ``show_if`` / ``enabled_if`` and
        other ``conditional``-typed attributes stay structured values —
        never converted to ``{{}}`` token strings."""
        for needle in (
            "show_if", "enabled_if", "conditional",
            "never convert", "{{}}",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, CREATE_TRAMITE_FORM_PROMPT)


@unittest.skipUnless(_DEPS, "requires mcp + httpx")
class CachedClientTests(unittest.TestCase):
    """``_get_client`` resolves config per call and caches clients per
    resolved ``(token, project_id)`` — the env credential is one entry.

    The full header/cache matrix lives in ``test_http_transport.py``; this
    pins the stdio-path invariant (no ctx → one cached client).
    """

    def test_client_built_once_per_resolved_credential(self):
        config = server.S7Config(base_url="http://backend.test", token="tok")
        with patch.dict(server._clients, clear=True), patch.object(
            server, "get_config", return_value=config
        ) as get_config, patch.object(server, "S7Client") as cls:
            first = server._get_client()
            second = server._get_client()
        self.assertIs(first, second)
        # The client is cached, but the env config is re-resolved on every
        # call — always with the token optional (headers may supply it).
        self.assertEqual(get_config.call_count, 2)
        get_config.assert_called_with(require_token=False)
        cls.assert_called_once_with(config)


@unittest.skipUnless(_DEPS, "requires mcp + httpx")
class McpSurfaceTests(unittest.TestCase):
    """The exposed MCP surface: exactly the EXPECTED_TOOLS set."""

    def test_every_expected_tool_exists_and_is_callable(self):
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
            ["node_type", "schema", "schema_key", "schema_version",
             "overwrite", "ctx"],
        )
        self.assertNotIn("status", params)
        self.assertNotIn("schema_status", params)

    def test_registered_tool_set_matches_module_functions(self):
        """The SDK registry (when introspectable) holds exactly the expected tools."""
        tool_manager = getattr(server.mcp, "_tool_manager", None)
        registered = getattr(tool_manager, "_tools", None)
        if isinstance(registered, dict):
            self.assertEqual(set(registered), EXPECTED_TOOLS)


@unittest.skipUnless(_DEPS, "requires mcp + httpx")
class MainTests(unittest.TestCase):
    """``main()`` validates config first, then serves.

    ``main`` now takes ``argv`` — tests always pass an explicit list so the
    parser never consumes the unittest runner's ``sys.argv``. Transport
    dispatch coverage lives in ``test_http_transport.py``.
    """

    def test_main_runs_server_after_config_ok(self):
        with patch.object(server, "get_config") as get_config, patch.object(
            server.mcp, "run"
        ) as run:
            server.main([])
        get_config.assert_called_once_with(require_token=True)
        run.assert_called_once_with()

    def test_main_propagates_config_exit_without_running(self):
        with patch.object(
            server, "get_config", side_effect=SystemExit(2)
        ), patch.object(server.mcp, "run") as run:
            with self.assertRaises(SystemExit) as cm:
                server.main([])
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
                    "s7://catalog-grammar",
                },
            )
            self.assertEqual(
                set(server.mcp.prompts),
                {"create_tramite_form", "author_catalog"},
            )


if __name__ == "__main__":
    unittest.main()
