"""Tests for ``s7_mcp.server`` — tools, resources, payloads and the SDK shim.

The ``@mcp.tool()``/``@mcp.resource()``/``@mcp.prompt()`` decorators register
the callables but return them unchanged, so every handler is invoked directly
and the API client is injected by patching ``_get_client``.
"""

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
from s7_mcp.catalog_grammar import CATALOG_GRAMMAR
from s7_mcp.prompts import AUTHOR_CATALOG_PROMPT, CREATE_TRAMITE_FORM_PROMPT

EXPECTED_TOOLS = {
    "s7_get_catalog",
    "s7_get_schema",
    "s7_get_draft",
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
        self.assertEqual(
            list(inspect.signature(server.s7_get_draft).parameters),
            ["node_type", "key", "version"],
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
        self.assertEqual(
            list(inspect.signature(server.s7_get_schema).parameters),
            ["node_type", "key", "version"],
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
        self.assertEqual(
            list(inspect.signature(server.schema_resource).parameters),
            ["node_type", "key", "version"],
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
        """The worked example must survive the import contract: real
        labels, no ``id`` inside ``fields``, every natural-key and
        required component present, and FK values shaped as
        ``[natural_key]`` lists or null."""
        objects = CATALOG_GRAMMAR["resolved_vs_import"]["example"][
            "import"
        ]["objects"]
        for obj in objects:
            label = obj["model"]
            with self.subTest(model=label):
                self.assertIn(label, self.EXPECTED_MODELS)
                model = CATALOG_GRAMMAR["models"][label]
                fields = obj["fields"]
                # 'id' is forbidden inside fields; 'version' is an
                # instance-document field, never a catalog row field.
                for forbidden in ("id", "version"):
                    self.assertNotIn(forbidden, fields)
                for name in model["natural_key"]:
                    self.assertIn(name, fields)
                for name, spec in model["fields"].items():
                    if spec["required"]:
                        self.assertIn(name, fields)
                for name, spec in model["fks"].items():
                    if spec["required"]:
                        self.assertIn(name, fields)
                for name, value in fields.items():
                    self.assertTrue(
                        name in model["fields"] or name in model["fks"],
                        f"{label}.{name} not in the grammar",
                    )
                    if name in model["fks"]:
                        self.assertTrue(
                            value is None
                            or (
                                isinstance(value, list)
                                and all(
                                    isinstance(item, str)
                                    for item in value
                                )
                            ),
                            f"{label}.{name} not a [natural_key] list",
                        )

    def test_resolved_vs_import_example_skips_server_generated(self):
        """The example resolves a ``server_generated`` attribute but no
        attributedef object authors it — the divergence the section
        exists to teach."""
        example = CATALOG_GRAMMAR["resolved_vs_import"]["example"]
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
        self.assertFalse(generated & authored)

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
    """The exposed MCP surface: exactly the seven read/author tools."""

    def test_the_seven_tools_exist_and_are_callable(self):
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
        """The SDK registry (when introspectable) holds exactly the 7 tools."""
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
                    "s7://catalog-grammar",
                },
            )
            self.assertEqual(
                set(server.mcp.prompts),
                {"create_tramite_form", "author_catalog"},
            )


if __name__ == "__main__":
    unittest.main()
