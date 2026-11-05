"""Tests for ``s7_mcp.client`` — the httpx wrapper and its error contract.

The contract under test:

- Any HTTP response (any status): JSON body returned verbatim.
- Transport failure or non-JSON body: ``transport_error`` envelope
  (``status`` set only when an HTTP response exists).
- Outgoing payload above ``max_payload_kb``: ``payload_too_large`` envelope
  with ``status: None`` and no request sent.

Everything runs through ``httpx.MockTransport`` — no live backend, no network.
"""

import importlib.util
import json
import unittest

_DEPS = importlib.util.find_spec("httpx") is not None

if _DEPS:
    import httpx

    from s7_mcp.client import S7Client
    from s7_mcp.config import S7Config


@unittest.skipUnless(_DEPS, "requires httpx")
class ClientConstructionTests(unittest.TestCase):
    """The internal httpx.Client carries auth, timeout and redirect policy."""

    def test_bearer_header_timeout_and_redirects(self):
        client = S7Client(
            S7Config(base_url="http://backend.test", token="tok", timeout_s=7.5)
        )
        self.assertEqual(client._http.headers.get("authorization"), "Bearer tok")
        self.assertEqual(client._http.timeout.as_dict()["read"], 7.5)
        self.assertIs(client._http.follow_redirects, True)

    def test_none_token_sends_no_authorization_header(self):
        """A token-less credential sends no ``Authorization`` at all — the
        backend answers its own 401 verbatim (never ``Bearer None``)."""
        client = S7Client(
            S7Config(base_url="http://backend.test", token=None)
        )
        self.assertIsNone(client._http.headers.get("authorization"))
        self.assertNotIn("authorization", client._http.headers)

    def test_blank_token_sends_no_authorization_header(self):
        client = S7Client(S7Config(base_url="http://backend.test", token=""))
        self.assertNotIn("authorization", client._http.headers)

    def test_project_id_property_reflects_config(self):
        client = S7Client(
            S7Config(
                base_url="http://backend.test",
                token="tok",
                project_id="12345678-1234-5678-1234-567812345678",
            )
        )
        self.assertEqual(
            client.project_id, "12345678-1234-5678-1234-567812345678"
        )

    def test_project_id_property_defaults_to_none(self):
        client = S7Client(
            S7Config(base_url="http://backend.test", token="tok")
        )
        self.assertIsNone(client.project_id)


class _ClientCase(unittest.TestCase):
    """Base case wiring an ``S7Client`` to a ``MockTransport`` handler."""

    def _client(self, handler, *, max_payload_kb=512, base_url="http://backend.test",
                project_id=None):
        client = S7Client(
            S7Config(
                base_url=base_url,
                token="tok",
                max_payload_kb=max_payload_kb,
                timeout_s=5,
                project_id=project_id,
            )
        )
        client._http = httpx.Client(
            base_url=base_url, transport=httpx.MockTransport(handler)
        )
        return client


@unittest.skipUnless(_DEPS, "requires httpx")
class RequestEnvelopeTests(_ClientCase):
    """Response-body handling: verbatim dicts, wraps and transport_error."""

    def test_get_builds_api_path_and_returns_body(self):
        seen = {}

        def handler(request):
            seen["method"] = request.method
            seen["path"] = request.url.path
            return httpx.Response(200, json={"datatypes": ["string"]})

        result = self._client(handler).get_json("catalog/datatypes/")
        self.assertEqual(seen, {"method": "GET", "path": "/api/catalog/datatypes/"})
        self.assertEqual(result, {"datatypes": ["string"]})

    def test_get_strips_leading_slash_from_path(self):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            return httpx.Response(200, json={})

        self._client(handler).get_json("/catalog/datatypes/")
        self.assertEqual(seen["path"], "/api/catalog/datatypes/")

    def test_error_status_body_returned_verbatim(self):
        envelope = {
            "type": "schema_not_found",
            "title": "Schema not found",
            "detail": "no published schema",
            "status": 404,
        }
        for status in (400, 404, 500):
            with self.subTest(status=status):
                client = self._client(
                    lambda request: httpx.Response(status, json=envelope)
                )
                self.assertEqual(client.get_json("schema/nt/k/v/"), envelope)

    def test_created_status_body_returned_verbatim(self):
        body = {"schema_id": "abc", "warning": None}
        client = self._client(lambda request: httpx.Response(201, json=body))
        self.assertEqual(client.post_json("schema/import/", {"x": 1}), body)

    def test_transport_exception_yields_transport_error_without_status(self):
        def handler(request):
            raise httpx.ConnectError("connection refused")

        result = self._client(handler).get_json("catalog/datatypes/")
        self.assertEqual(result["error"]["code"], "transport_error")
        self.assertIsNone(result["error"]["status"])
        self.assertIn("ConnectError", result["error"]["message"])
        self.assertIn("connection refused", result["error"]["message"])

    def test_request_on_closed_client_yields_transport_error(self):
        """A request dispatched on an already-closed ``httpx.Client`` raises
        a plain ``RuntimeError`` (not ``HTTPError``) — it must still map to
        the ``transport_error`` envelope. This pins the use-after-evict
        window of the server's bounded LRU at client level: ``close()``
        plus a deterministic request, no concurrency mocks."""
        client = self._client(lambda request: httpx.Response(200, json={}))
        client._http.close()
        result = client.get_json("catalog/datatypes/")
        self.assertEqual(result["error"]["code"], "transport_error")
        self.assertIsNone(result["error"]["status"])
        self.assertIn("RuntimeError", result["error"]["message"])

    def test_read_timeout_yields_transport_error(self):
        def handler(request):
            raise httpx.ReadTimeout("timed out")

        result = self._client(handler).get_json("catalog/datatypes/")
        self.assertEqual(result["error"]["code"], "transport_error")
        self.assertIsNone(result["error"]["status"])
        self.assertIn("ReadTimeout", result["error"]["message"])

    def test_non_json_body_yields_transport_error_with_status(self):
        client = self._client(
            lambda request: httpx.Response(502, text="<html>Bad Gateway</html>")
        )
        result = client.get_json("catalog/datatypes/")
        self.assertEqual(result["error"]["code"], "transport_error")
        self.assertEqual(result["error"]["status"], 502)
        self.assertIn("non-JSON", result["error"]["message"])

    def test_non_dict_json_body_is_wrapped_in_data(self):
        for body in ([1, 2, 3], "plain", 5):
            with self.subTest(body=body):
                client = self._client(
                    lambda request: httpx.Response(200, json=body)
                )
                self.assertEqual(client.get_json("x/"), {"data": body})

    def test_post_sends_json_body_with_content_type(self):
        seen = {}

        def handler(request):
            seen["content_type"] = request.headers.get("content-type")
            seen["body"] = request.content.decode()
            return httpx.Response(200, json={"ok": True})

        payload = {"schema_text": "{}", "node_type": "survey"}
        self._client(handler).post_json("schema/validate/", payload)
        self.assertEqual(seen["content_type"], "application/json")
        self.assertEqual(seen["body"], json.dumps(payload))


@unittest.skipUnless(_DEPS, "requires httpx")
class GetJsonParamsTests(_ClientCase):
    """``get_json`` encodes optional query params onto the request URL."""

    def _seen_url(self, params):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            seen["params"] = request.url.params
            return httpx.Response(200, json={"ok": True})

        result = self._client(handler).get_json(
            "schema/draft/nt/k/v/", params=params
        )
        self.assertEqual(result, {"ok": True})
        return seen

    def test_params_become_the_query_string(self):
        seen = self._seen_url({"project_id": "abc-123", "other": "x"})
        self.assertEqual(seen["path"], "/api/schema/draft/nt/k/v/")
        self.assertEqual(seen["params"]["project_id"], "abc-123")
        self.assertEqual(seen["params"]["other"], "x")

    def test_param_values_are_urlencoded(self):
        seen = self._seen_url({"project_id": "a b&c=d"})
        self.assertEqual(seen["params"]["project_id"], "a b&c=d")

    def test_none_param_values_are_skipped(self):
        seen = self._seen_url({"project_id": None, "other": "x"})
        self.assertNotIn("project_id", seen["params"])
        self.assertEqual(seen["params"]["other"], "x")

    def test_all_none_params_send_no_query_string(self):
        seen = self._seen_url({"project_id": None})
        self.assertEqual(len(seen["params"]), 0)

    def test_no_params_sends_no_query_string(self):
        seen = self._seen_url(None)
        self.assertEqual(len(seen["params"]), 0)

    def test_error_contract_is_unchanged_with_params(self):
        envelope = {"type": "schema_not_found", "status": 404}
        client = self._client(
            lambda request: httpx.Response(404, json=envelope)
        )
        result = client.get_json("schema/draft/nt/k/v/",
                                 params={"project_id": "abc"})
        self.assertEqual(result, envelope)


@unittest.skipUnless(_DEPS, "requires httpx")
class DeleteJsonParamsTests(_ClientCase):
    """``delete_json`` issues DELETE and encodes params as a query string."""

    def _seen_request(self, params, *, project_id=None):
        seen = {}

        def handler(request):
            seen["method"] = request.method
            seen["path"] = request.url.path
            seen["params"] = request.url.params
            seen["body"] = request.content
            return httpx.Response(200, json={"deleted": True})

        result = self._client(handler, project_id=project_id).delete_json(
            "schema/draft/nt/k/v/", params=params
        )
        self.assertEqual(result, {"deleted": True})
        return seen

    def test_delete_method_and_api_path(self):
        seen = self._seen_request({"project_id": "abc-123"})
        self.assertEqual(seen["method"], "DELETE")
        self.assertEqual(seen["path"], "/api/schema/draft/nt/k/v/")

    def test_params_become_the_query_string(self):
        seen = self._seen_request({"project_id": "abc-123", "other": "x"})
        self.assertEqual(seen["params"]["project_id"], "abc-123")
        self.assertEqual(seen["params"]["other"], "x")

    def test_param_values_are_urlencoded(self):
        seen = self._seen_request({"project_id": "a b&c=d"})
        self.assertEqual(seen["params"]["project_id"], "a b&c=d")

    def test_none_param_values_are_skipped(self):
        seen = self._seen_request({"project_id": None, "other": "x"})
        self.assertNotIn("project_id", seen["params"])
        self.assertEqual(seen["params"]["other"], "x")

    def test_all_none_params_send_no_query_string(self):
        seen = self._seen_request({"project_id": None})
        self.assertEqual(len(seen["params"]), 0)

    def test_no_params_sends_no_query_string(self):
        seen = self._seen_request(None)
        self.assertEqual(len(seen["params"]), 0)

    def test_no_body_is_sent(self):
        """A DELETE carries no body — ``project_id`` travels in the query
        string, never injected into a payload the way ``post_json`` does."""
        seen = self._seen_request({"project_id": "abc"})
        self.assertEqual(seen["body"], b"")

    def test_configured_project_id_is_not_injected(self):
        """Unlike ``post_json`` (which injects ``S7_PROJECT_ID`` into the
        body), ``delete_json`` adds nothing on its own — the caller forwards
        the id explicitly through ``params``."""
        seen = self._seen_request(None, project_id="some-uuid")
        self.assertEqual(len(seen["params"]), 0)

    def test_error_status_body_returned_verbatim(self):
        envelope = {"type": "schema_not_found", "status": 404}
        for status in (400, 403, 404, 500):
            with self.subTest(status=status):
                client = self._client(
                    lambda request: httpx.Response(status, json=envelope)
                )
                result = client.delete_json(
                    "schema/draft/nt/k/v/", params={"project_id": "abc"}
                )
                self.assertEqual(result, envelope)

    def test_non_json_body_yields_transport_error_with_status(self):
        client = self._client(
            lambda request: httpx.Response(502, text="<html>Bad Gateway</html>")
        )
        result = client.delete_json("schema/draft/nt/k/v/")
        self.assertEqual(result["error"]["code"], "transport_error")
        self.assertEqual(result["error"]["status"], 502)
        self.assertIn("non-JSON", result["error"]["message"])


@unittest.skipUnless(_DEPS, "requires httpx")
class PayloadSizeGuardTests(_ClientCase):
    """``post_json`` refuses bodies above the configured byte limit."""

    def _counting_client(self, max_payload_kb):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"ok": True})

        return self._client(handler, max_payload_kb=max_payload_kb), calls

    def test_payload_exactly_at_limit_is_sent(self):
        client, calls = self._counting_client(max_payload_kb=1)
        pad = "x" * (1024 - len(json.dumps({"pad": ""})))
        payload = {"pad": pad}
        self.assertEqual(len(json.dumps(payload)), 1024)  # sanity on the limit
        result = client.post_json("schema/validate/", payload)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(calls), 1)

    def test_payload_one_byte_over_limit_is_refused(self):
        client, calls = self._counting_client(max_payload_kb=1)
        pad = "x" * (1024 - len(json.dumps({"pad": ""})) + 1)
        result = client.post_json("schema/validate/", {"pad": pad})
        self.assertEqual(result["error"]["code"], "payload_too_large")
        self.assertIsNone(result["error"]["status"])
        self.assertIn("1025", result["error"]["message"])
        self.assertIn("1024", result["error"]["message"])
        # The refusal happens before any request is sent.
        self.assertEqual(calls, [])

    def test_unicode_is_measured_on_the_escaped_json(self):
        """``json.dumps`` escapes non-ASCII (``\\uXXXX``), so a payload whose
        Python length is below the limit can still exceed it once encoded."""
        client, calls = self._counting_client(max_payload_kb=1)
        payload = {"pad": "á" * 200}
        self.assertLess(len(json.dumps(payload, ensure_ascii=False)), 1024)
        self.assertGreater(len(json.dumps(payload)), 1024)  # escaped form
        result = client.post_json("schema/validate/", payload)
        self.assertEqual(result["error"]["code"], "payload_too_large")
        self.assertIsNone(result["error"]["status"])
        self.assertEqual(calls, [])


@unittest.skipUnless(_DEPS, "requires httpx")
class ProjectIdInjectionTests(_ClientCase):
    """``post_json`` adds ``project_id`` to the wire body when configured."""

    _PROJECT_ID = "12345678-1234-5678-1234-567812345678"

    def _seen_body(self, project_id):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json={"ok": True})

        result = self._client(handler, project_id=project_id).post_json(
            "schema/validate/", {"schema_text": "{}", "node_type": "survey"}
        )
        self.assertEqual(result, {"ok": True})
        return seen["body"]

    def test_project_id_injected_when_configured(self):
        body = self._seen_body(self._PROJECT_ID)
        self.assertEqual(body["project_id"], self._PROJECT_ID)
        self.assertEqual(body["node_type"], "survey")

    def test_project_id_absent_when_not_configured(self):
        body = self._seen_body(None)
        self.assertNotIn("project_id", body)
        self.assertEqual(set(body), {"schema_text", "node_type"})

    def test_organization_id_is_never_injected(self):
        body = self._seen_body(self._PROJECT_ID)
        self.assertNotIn("organization_id", body)

    def test_env_project_id_wins_over_same_named_key(self):
        """A payload already carrying ``project_id`` is overridden by env."""
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json={"ok": True})

        self._client(handler, project_id=self._PROJECT_ID).post_json(
            "schema/validate/", {"node_type": "survey", "project_id": "other"}
        )
        self.assertEqual(seen["body"]["project_id"], self._PROJECT_ID)

    def test_injected_project_id_counts_toward_payload_limit(self):
        """Injection happens before the size check: the wire body is measured."""
        calls = []
        schema_text = "x" * (1024 - len(json.dumps(
            {"schema_text": "", "node_type": "s", "project_id": self._PROJECT_ID}
        )) + 1)
        client = self._client(
            lambda request: calls.append(request) or httpx.Response(200, json={}),
            max_payload_kb=1,
            project_id=self._PROJECT_ID,
        )
        result = client.post_json(
            "schema/validate/", {"schema_text": schema_text, "node_type": "s"}
        )
        # Sized so the body is 1 byte over the limit only once project_id
        # is injected; without it the same payload would pass.
        self.assertEqual(result["error"]["code"], "payload_too_large")
        self.assertEqual(calls, [])


@unittest.skipUnless(_DEPS, "requires httpx")
class PatchJsonTests(_ClientCase):
    """``patch_json`` mirrors ``post_json``: PATCH with a JSON body, the
    same ``project_id`` injection, size guard and error contract."""

    _PROJECT_ID = "12345678-1234-5678-1234-567812345678"

    def _seen_request(self, payload, *, project_id=None, max_payload_kb=512):
        seen = {}
        calls = []

        def handler(request):
            calls.append(request)
            seen["method"] = request.method
            seen["path"] = request.url.path
            seen["content_type"] = request.headers.get("content-type")
            seen["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json={"ok": True})

        result = self._client(
            handler, project_id=project_id, max_payload_kb=max_payload_kb
        ).patch_json("catalog/changesets/abc/", payload)
        return result, seen, calls

    def test_patch_method_and_api_path(self):
        result, seen, _ = self._seen_request({"ops": []})
        self.assertEqual(result, {"ok": True})
        self.assertEqual(seen["method"], "PATCH")
        self.assertEqual(seen["path"], "/api/catalog/changesets/abc/")

    def test_patch_sends_json_body_with_content_type(self):
        payload = {"ops": [{"op": "add_domain"}], "rationale": "why"}
        _, seen, _ = self._seen_request(payload)
        self.assertEqual(seen["content_type"], "application/json")
        self.assertEqual(seen["body"], payload)

    def test_project_id_injected_when_configured(self):
        _, seen, _ = self._seen_request(
            {"ops": []}, project_id=self._PROJECT_ID
        )
        self.assertEqual(seen["body"]["project_id"], self._PROJECT_ID)

    def test_project_id_absent_when_not_configured(self):
        _, seen, _ = self._seen_request({"ops": [], "rationale": "r"})
        self.assertNotIn("project_id", seen["body"])
        self.assertEqual(set(seen["body"]), {"ops", "rationale"})

    def test_env_project_id_wins_over_same_named_key(self):
        """A payload already carrying ``project_id`` is overridden by
        env — same rule as ``post_json``."""
        _, seen, _ = self._seen_request(
            {"ops": [], "project_id": "other"}, project_id=self._PROJECT_ID
        )
        self.assertEqual(seen["body"]["project_id"], self._PROJECT_ID)

    def test_organization_id_is_never_injected(self):
        _, seen, _ = self._seen_request(
            {"ops": []}, project_id=self._PROJECT_ID
        )
        self.assertNotIn("organization_id", seen["body"])

    def test_payload_one_byte_over_limit_is_refused(self):
        pad = "x" * (1024 - len(json.dumps({"pad": ""})) + 1)
        _, seen, calls = self._seen_request(
            {"pad": pad}, max_payload_kb=1
        )
        # The refusal happens before any request is sent — the envelope,
        # not the wire, carries the diagnostic.
        self.assertEqual(calls, [])
        self.assertEqual(seen, {})

    def test_payload_too_large_envelope_shape(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"ok": True})

        client = self._client(handler, max_payload_kb=1)
        pad = "x" * (1024 - len(json.dumps({"pad": ""})) + 1)
        result = client.patch_json("catalog/changesets/abc/", {"pad": pad})
        self.assertEqual(result["error"]["code"], "payload_too_large")
        self.assertIsNone(result["error"]["status"])
        self.assertIn("1025", result["error"]["message"])
        self.assertIn("1024", result["error"]["message"])
        self.assertEqual(calls, [])

    def test_injected_project_id_counts_toward_payload_limit(self):
        """Injection happens before the size check: the wire body is
        measured, same as ``post_json``."""
        calls = []
        rationale = "x" * (1024 - len(json.dumps(
            {"ops": [], "rationale": "", "project_id": self._PROJECT_ID}
        )) + 1)
        client = self._client(
            lambda request: calls.append(request) or httpx.Response(200, json={}),
            max_payload_kb=1,
            project_id=self._PROJECT_ID,
        )
        result = client.patch_json(
            "catalog/changesets/abc/", {"ops": [], "rationale": rationale}
        )
        self.assertEqual(result["error"]["code"], "payload_too_large")
        self.assertEqual(calls, [])

    def test_transport_exception_yields_transport_error_without_status(self):
        def handler(request):
            raise httpx.ConnectError("connection refused")

        result = self._client(handler).patch_json(
            "catalog/changesets/abc/", {"ops": []}
        )
        self.assertEqual(result["error"]["code"], "transport_error")
        self.assertIsNone(result["error"]["status"])
        self.assertIn("ConnectError", result["error"]["message"])

    def test_request_on_closed_client_yields_transport_error(self):
        """A request on an already-closed ``httpx.Client`` raises a plain
        ``RuntimeError`` (not ``HTTPError``) — still a transport failure,
        same mapping as the other verbs."""
        client = self._client(lambda request: httpx.Response(200, json={}))
        client._http.close()
        result = client.patch_json("catalog/changesets/abc/", {"ops": []})
        self.assertEqual(result["error"]["code"], "transport_error")
        self.assertIsNone(result["error"]["status"])
        self.assertIn("RuntimeError", result["error"]["message"])

    def test_error_status_body_returned_verbatim(self):
        envelope = {"error": "changeset cannot transition from 'x' to 'y'"}
        for status in (400, 403, 404, 409, 500):
            with self.subTest(status=status):
                client = self._client(
                    lambda request: httpx.Response(status, json=envelope)
                )
                result = client.patch_json(
                    "catalog/changesets/abc/", {"ops": []}
                )
                self.assertEqual(result, envelope)

    def test_non_json_body_yields_transport_error_with_status(self):
        client = self._client(
            lambda request: httpx.Response(502, text="<html>Bad Gateway</html>")
        )
        result = client.patch_json("catalog/changesets/abc/", {"ops": []})
        self.assertEqual(result["error"]["code"], "transport_error")
        self.assertEqual(result["error"]["status"], 502)
        self.assertIn("non-JSON", result["error"]["message"])

    def test_non_dict_json_body_is_wrapped_in_data(self):
        """The defensive scalar/array wrap applies to PATCH responses
        too — callers can rely on a dict."""
        client = self._client(
            lambda request: httpx.Response(200, json=[1, 2, 3])
        )
        result = client.patch_json("catalog/changesets/abc/", {"ops": []})
        self.assertEqual(result, {"data": [1, 2, 3]})


@unittest.skipUnless(_DEPS, "requires httpx")
class ChangesetMethodTests(_ClientCase):
    """The three named changeset methods — path, body and the read's
    deliberate lack of ``project_id``.

    ``propose_changeset``/``fix_changeset`` delegate to ``post_json``/
    ``patch_json`` (body carries ``project_id`` when configured);
    ``get_changeset`` delegates to ``get_json`` with no params — the
    changeset endpoints are user-token-gated, so no ``?project_id=``
    ever travels.
    """

    _PROJECT_ID = "12345678-1234-5678-1234-567812345678"
    _OPS = [{"op": "add_node_type", "name": "x_type", "label": "X"}]

    # -- propose_changeset -------------------------------------------------

    def test_propose_posts_to_changesets_collection(self):
        seen = {}

        def handler(request):
            seen["method"] = request.method
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content.decode())
            return httpx.Response(201, json={"id": "cs-1", "status": "valid"})

        result = self._client(handler).propose_changeset(
            self._OPS, "the why"
        )
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["path"], "/api/catalog/changesets/")
        self.assertEqual(
            seen["body"], {"ops": self._OPS, "rationale": "the why"}
        )
        self.assertEqual(result, {"id": "cs-1", "status": "valid"})

    def test_propose_injects_project_id_into_the_body(self):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content.decode())
            return httpx.Response(201, json={})

        self._client(handler, project_id=self._PROJECT_ID).propose_changeset(
            self._OPS, "the why"
        )
        self.assertEqual(seen["body"]["project_id"], self._PROJECT_ID)

    def test_propose_error_envelopes_propagate_verbatim(self):
        """400 ``invalid_request`` and the post-validation 403
        ``changeset_scopes_not_covered`` reach the caller untouched."""
        envelopes = [
            {"error": "invalid_request", "message": "rationale required"},
            {"error": "changeset_scopes_not_covered", "message": "m"},
        ]
        for envelope in envelopes:
            with self.subTest(envelope=envelope):
                client = self._client(
                    lambda request: httpx.Response(400, json=envelope)
                )
                result = client.propose_changeset(self._OPS, "r")
                self.assertEqual(result, envelope)

    # -- get_changeset -----------------------------------------------------

    def test_get_hits_the_quoted_member_path(self):
        seen = {}

        def handler(request):
            seen["method"] = request.method
            seen["path"] = request.url.path
            seen["body"] = request.content
            return httpx.Response(200, json={"id": "cs-1"})

        result = self._client(handler).get_changeset("cs-1")
        self.assertEqual(seen["method"], "GET")
        self.assertEqual(seen["path"], "/api/catalog/changesets/cs-1/")
        self.assertEqual(seen["body"], b"")
        self.assertEqual(result, {"id": "cs-1"})

    def test_get_quotes_reserved_characters_in_the_id(self):
        seen = {}

        def handler(request):
            # ``url.path`` is the decoded form — ``raw_path`` carries the
            # percent-encoded bytes that actually travel on the wire.
            seen["raw_path"] = request.url.raw_path
            return httpx.Response(200, json={})

        self._client(handler).get_changeset("a/b c")
        self.assertEqual(
            seen["raw_path"], b"/api/catalog/changesets/a%2Fb%20c/"
        )

    def test_get_quotes_every_reserved_character(self):
        """Same quoting contract as ``server._q`` — ``quote(safe="")``."""
        seen = {}

        def handler(request):
            seen["raw_path"] = request.url.raw_path
            return httpx.Response(200, json={})

        self._client(handler).get_changeset("a/b c?%ñ")
        self.assertEqual(
            seen["raw_path"],
            b"/api/catalog/changesets/a%2Fb%20c%3F%25%C3%B1/",
        )

    def test_get_sends_no_params_even_with_project_id_configured(self):
        """Changeset reads are user-token-gated: unlike the schema
        reads, ``?project_id=`` is never appended — the id lives in the
        path and the credential in the bearer header."""
        seen = {}

        def handler(request):
            seen["params"] = request.url.params
            return httpx.Response(200, json={})

        self._client(handler, project_id=self._PROJECT_ID).get_changeset(
            "cs-1"
        )
        self.assertEqual(len(seen["params"]), 0)

    def test_get_error_envelopes_propagate_verbatim(self):
        envelopes = [
            {"error": "changeset_not_found", "changeset_id": "cs-9"},
            {"error": "changeset_scopes_not_covered", "scopes": ["forms"]},
        ]
        for envelope in envelopes:
            with self.subTest(envelope=envelope):
                client = self._client(
                    lambda request: httpx.Response(404, json=envelope)
                )
                self.assertEqual(client.get_changeset("cs-9"), envelope)

    # -- fix_changeset -----------------------------------------------------

    def test_fix_patches_the_quoted_member_path(self):
        seen = {}

        def handler(request):
            seen["method"] = request.method
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json={"id": "cs-1", "status": "valid"})

        result = self._client(handler).fix_changeset(
            "cs-1", self._OPS, "new rationale"
        )
        self.assertEqual(seen["method"], "PATCH")
        self.assertEqual(seen["path"], "/api/catalog/changesets/cs-1/")
        self.assertEqual(
            seen["body"],
            {"ops": self._OPS, "rationale": "new rationale"},
        )
        self.assertEqual(result, {"id": "cs-1", "status": "valid"})

    def test_fix_quotes_reserved_characters_in_the_id(self):
        seen = {}

        def handler(request):
            seen["raw_path"] = request.url.raw_path
            return httpx.Response(200, json={})

        self._client(handler).fix_changeset("a/b c", self._OPS, "r")
        self.assertEqual(
            seen["raw_path"], b"/api/catalog/changesets/a%2Fb%20c/"
        )

    def test_fix_sends_the_new_rationale_not_the_old_one(self):
        """Every PATCH requires a fresh non-blank rationale — the wire
        body must carry exactly what the caller passed."""
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json={})

        self._client(handler).fix_changeset(
            "cs-1", self._OPS, "addresses finding at op 0"
        )
        self.assertEqual(
            seen["body"]["rationale"], "addresses finding at op 0"
        )

    def test_fix_injects_project_id_into_the_body(self):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json={})

        self._client(handler, project_id=self._PROJECT_ID).fix_changeset(
            "cs-1", self._OPS, "r"
        )
        self.assertEqual(seen["body"]["project_id"], self._PROJECT_ID)

    def test_fix_error_envelopes_propagate_verbatim(self):
        """409 transition rejection, 404 unknown id and 400 malformed
        body all propagate untouched."""
        envelopes = [
            (409, {
                "error": "changeset cannot transition from 'applied' "
                         "to 'validating'",
            }),
            (404, {"error": "changeset_not_found", "changeset_id": "cs-9"}),
            (400, {"error": "invalid_request", "message": "ops required"}),
        ]
        for status, envelope in envelopes:
            with self.subTest(status=status):
                client = self._client(
                    lambda request: httpx.Response(status, json=envelope)
                )
                result = client.fix_changeset("cs-9", self._OPS, "r")
                self.assertEqual(result, envelope)


if __name__ == "__main__":
    unittest.main()
