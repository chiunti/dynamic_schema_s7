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


if __name__ == "__main__":
    unittest.main()
