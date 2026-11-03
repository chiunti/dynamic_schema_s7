"""Tests for the streamable-http transport of ``s7_mcp.server``.

Covers the per-request credential resolution (``_request_headers`` /
``_resolve_credentials``), the bounded-LRU ``_clients`` cache keyed by
``(token, project_id)``, ``main(argv)`` transport dispatch and the
``ctx``-free ``inputSchema`` contract. Everything is mocked — no port is
ever bound and no HTTP request is performed.
"""

import contextlib
import importlib.util
import inspect
import io
import threading
import time
import types
import unittest
from unittest.mock import MagicMock, call, patch

_DEPS = (
    importlib.util.find_spec("mcp") is not None
    and importlib.util.find_spec("httpx") is not None
)

# Pure-stdlib module — importable without mcp/httpx, like test_config.py.
from s7_mcp.config import S7Config

if _DEPS:
    import s7_mcp.server as server

    try:
        from starlette.datastructures import Headers
    except ImportError:  # pragma: no cover - starlette ships with the mcp SDK
        class Headers(dict):
            """Minimal case-insensitive header map used as fallback."""

            def __init__(self, raw):
                super().__init__({k.lower(): v for k, v in raw.items()})

            def get(self, key, default=None):
                return super().get(key.lower(), default)

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

TEMPLATE_RESOURCES = ("catalog_resource", "example_resource", "schema_resource")
STATIC_RESOURCES = ("datatypes_resource", "catalog_grammar_resource")


def _ctx_with_request(request):
    """Build a ``Context`` stand-in exposing ``request_context.request``."""
    return types.SimpleNamespace(
        request_context=types.SimpleNamespace(request=request)
    )


def _ctx_with_headers(headers):
    """Build a ``Context`` stand-in whose request carries ``headers``."""
    return _ctx_with_request(types.SimpleNamespace(headers=headers))


class _OrphanContext:
    """``Context`` stand-in outside a request: ``request_context`` raises.

    The real SDK ``Context.request_context`` property raises ``ValueError``
    when no request is active (stdio, or a bare context) — both paths must
    resolve to the env credential.
    """

    @property
    def request_context(self):
        raise ValueError("request context is not available")


@unittest.skipUnless(_DEPS, "requires mcp + httpx")
class RequestHeadersTests(unittest.TestCase):
    """``_request_headers`` digs the inbound headers out of the ctx, or None."""

    def test_none_ctx_returns_none(self):
        self.assertIsNone(server._request_headers(None))

    def test_request_context_value_error_returns_none(self):
        self.assertIsNone(server._request_headers(_OrphanContext()))

    def test_missing_request_context_attribute_returns_none(self):
        """A ctx stand-in without ``request_context`` raises AttributeError
        on access — same None resolution."""
        self.assertIsNone(server._request_headers(types.SimpleNamespace()))

    def test_request_none_returns_none(self):
        """Under stdio the request is ``None`` — env-only credential."""
        self.assertIsNone(server._request_headers(_ctx_with_request(None)))

    def test_request_context_missing_request_returns_none(self):
        """``request_context.request`` raising AttributeError is caught too."""
        ctx = types.SimpleNamespace(request_context=types.SimpleNamespace())
        self.assertIsNone(server._request_headers(ctx))

    def test_request_without_headers_attribute_returns_none(self):
        ctx = _ctx_with_request(types.SimpleNamespace())
        self.assertIsNone(server._request_headers(ctx))

    def test_headers_object_returned_verbatim(self):
        headers = Headers({"authorization": "Bearer tok"})
        result = server._request_headers(_ctx_with_headers(headers))
        self.assertIs(result, headers)


@unittest.skipUnless(_DEPS, "requires mcp + httpx")
class ResolveCredentialsTests(unittest.TestCase):
    """``_resolve_credentials``: headers win field by field, never promoted."""

    ENV = S7Config(
        base_url="http://backend.test",
        token="env-token",
        project_id="env-project",
    )

    def _resolve(self, ctx, config=None):
        return server._resolve_credentials(ctx, config or self.ENV)

    # -- env fallback ------------------------------------------------------

    def test_none_ctx_returns_the_env_pair(self):
        self.assertEqual(
            self._resolve(None), ("env-token", "env-project")
        )

    def test_orphan_ctx_returns_the_env_pair(self):
        self.assertEqual(
            self._resolve(_OrphanContext()), ("env-token", "env-project")
        )

    def test_request_none_returns_the_env_pair(self):
        self.assertEqual(
            self._resolve(_ctx_with_request(None)),
            ("env-token", "env-project"),
        )

    # -- per-field header precedence ----------------------------------------

    def test_bearer_authorization_wins_over_env_token(self):
        ctx = _ctx_with_headers(Headers({"Authorization": "Bearer req-token"}))
        self.assertEqual(self._resolve(ctx), ("req-token", "env-project"))

    def test_project_header_wins_over_env_project(self):
        ctx = _ctx_with_headers(Headers({"X-S7-Project-Id": "req-project"}))
        self.assertEqual(self._resolve(ctx), ("env-token", "req-project"))

    def test_authorization_only_keeps_env_project(self):
        """Field-by-field: a present ``Authorization`` overrides the token
        while the absent project header falls back to ``S7_PROJECT_ID``."""
        ctx = _ctx_with_headers(Headers({"Authorization": "Bearer req-token"}))
        token, project_id = self._resolve(ctx)
        self.assertEqual(token, "req-token")
        self.assertEqual(project_id, "env-project")

    def test_project_header_only_keeps_env_token(self):
        """Field-by-field, the other direction: a present project header
        overrides ``S7_PROJECT_ID`` while ``S7_TOKEN`` stays."""
        ctx = _ctx_with_headers(Headers({"X-S7-Project-Id": "req-project"}))
        token, project_id = self._resolve(ctx)
        self.assertEqual(token, "env-token")
        self.assertEqual(project_id, "req-project")

    def test_both_headers_win_field_by_field(self):
        ctx = _ctx_with_headers(
            Headers(
                {
                    "Authorization": "Bearer req-token",
                    "X-S7-Project-Id": "req-project",
                }
            )
        )
        self.assertEqual(self._resolve(ctx), ("req-token", "req-project"))

    # -- Authorization parsing ----------------------------------------------

    def test_bearer_scheme_is_case_insensitive(self):
        for scheme in ("bearer", "BEARER", "Bearer", "bEaReR"):
            with self.subTest(scheme=scheme):
                ctx = _ctx_with_headers(
                    Headers({"Authorization": f"{scheme} req-token"})
                )
                self.assertEqual(self._resolve(ctx)[0], "req-token")

    def test_bearer_parsing_is_whitespace_flexible(self):
        for raw in ("Bearer   req-token", "  Bearer req-token  ",
                    "Bearer\treq-token"):
            with self.subTest(raw=raw):
                ctx = _ctx_with_headers(Headers({"Authorization": raw}))
                self.assertEqual(self._resolve(ctx)[0], "req-token")

    def test_non_bearer_scheme_resolves_no_token(self):
        """A non-Bearer Authorization is an unresolvable credential — it is
        never silently promoted to ``S7_TOKEN``; the backend answers its
        own 401 verbatim."""
        for raw in ("Basic dXNlcjpwYXNz", "Token req-token", "Digest x"):
            with self.subTest(raw=raw):
                ctx = _ctx_with_headers(Headers({"Authorization": raw}))
                self.assertIsNone(self._resolve(ctx)[0])

    def test_empty_or_bare_bearer_resolves_no_token(self):
        for raw in ("", "   ", "Bearer", "Bearer   "):
            with self.subTest(raw=raw):
                ctx = _ctx_with_headers(Headers({"Authorization": raw}))
                self.assertIsNone(self._resolve(ctx)[0])

    def test_unresolvable_authorization_does_not_fall_back_to_env(self):
        """The resolved ``None`` token is not promoted to the env credential
        even though ``S7_TOKEN`` is set."""
        ctx = _ctx_with_headers(Headers({"Authorization": "Basic abc"}))
        token, project_id = self._resolve(ctx)
        self.assertIsNone(token)
        self.assertEqual(project_id, "env-project")

    # -- X-S7-Project-Id parsing --------------------------------------------

    def test_project_header_is_stripped(self):
        ctx = _ctx_with_headers(
            Headers({"X-S7-Project-Id": "  req-project  "})
        )
        self.assertEqual(self._resolve(ctx)[1], "req-project")

    def test_malformed_project_header_travels_verbatim(self):
        """No UUID validation on this side — the backend answers its own
        400/403."""
        ctx = _ctx_with_headers(
            Headers({"X-S7-Project-Id": "not-a-uuid"})
        )
        self.assertEqual(self._resolve(ctx)[1], "not-a-uuid")

    def test_blank_project_header_resolves_none_not_env(self):
        """A present-but-blank project header overrides the env value to
        ``None`` — field-by-field precedence, never promotion."""
        ctx = _ctx_with_headers(Headers({"X-S7-Project-Id": "   "}))
        self.assertEqual(self._resolve(ctx), ("env-token", None))

    def test_header_names_are_case_insensitive(self):
        """Wire headers arrive in any casing — Starlette normalizes lookup."""
        ctx = _ctx_with_headers(
            Headers(
                {
                    "AUTHORIZATION": "Bearer req-token",
                    "X-S7-PROJECT-ID": "req-project",
                }
            )
        )
        self.assertEqual(self._resolve(ctx), ("req-token", "req-project"))


@unittest.skipUnless(_DEPS, "requires mcp + httpx")
class GetClientCacheTests(unittest.TestCase):
    """``_get_client`` caches ``S7Client`` instances per resolved
    ``(token, project_id)`` pair; the env credential is one entry."""

    ENV = S7Config(
        base_url="http://backend.test",
        token="env-token",
        project_id="env-project",
    )

    def setUp(self):
        clients_patcher = patch.dict(server._clients, clear=True)
        clients_patcher.start()
        self.addCleanup(clients_patcher.stop)
        config_patcher = patch.object(
            server, "get_config", return_value=self.ENV
        )
        self.get_config = config_patcher.start()
        self.addCleanup(config_patcher.stop)
        self.addCleanup(self._close_clients)

    @staticmethod
    def _close_clients():
        for client in server._clients.values():
            client._http.close()

    def _bearer_ctx(self, token, project=None):
        headers = {"Authorization": f"Bearer {token}"}
        if project is not None:
            headers["X-S7-Project-Id"] = project
        return _ctx_with_headers(Headers(headers))

    # -- caching -----------------------------------------------------------

    def test_none_ctx_caches_under_the_env_credential(self):
        client = server._get_client()
        self.assertIs(client, server._get_client())
        self.assertEqual(
            set(server._clients), {("env-token", "env-project")}
        )

    def test_same_resolved_credential_returns_same_instance(self):
        first = server._get_client(self._bearer_ctx("req-token", "proj"))
        second = server._get_client(self._bearer_ctx("req-token", "proj"))
        self.assertIs(first, second)
        self.assertEqual(len(server._clients), 1)

    def test_distinct_tokens_get_distinct_clients(self):
        client_a = server._get_client(self._bearer_ctx("token-a"))
        client_b = server._get_client(self._bearer_ctx("token-b"))
        self.assertIsNot(client_a, client_b)
        self.assertEqual(
            set(server._clients), {("token-a", "env-project"),
                                   ("token-b", "env-project")}
        )

    def test_distinct_projects_get_distinct_clients(self):
        client_a = server._get_client(self._bearer_ctx("token-a", "p1"))
        client_b = server._get_client(self._bearer_ctx("token-a", "p2"))
        self.assertIsNot(client_a, client_b)
        self.assertEqual(len(server._clients), 2)

    def test_header_credential_equal_to_env_shares_the_env_entry(self):
        """A request carrying exactly the env credential hits the same cache
        entry — the env credential is not special-cased."""
        env_client = server._get_client()
        header_client = server._get_client(
            self._bearer_ctx("env-token", "env-project")
        )
        self.assertIs(env_client, header_client)
        self.assertEqual(len(server._clients), 1)

    def test_orphan_ctx_uses_the_env_entry(self):
        """A ctx outside a request resolves the env credential — same cache
        entry as ``ctx=None``."""
        self.assertIs(
            server._get_client(_OrphanContext()), server._get_client()
        )

    # -- resolved credential reaches the client ----------------------------

    def test_bearer_header_client_sends_that_token(self):
        client = server._get_client(self._bearer_ctx("req-token"))
        self.assertEqual(
            client._http.headers.get("authorization"), "Bearer req-token"
        )
        self.assertEqual(client.project_id, "env-project")

    def test_project_header_reflects_in_client_project_id(self):
        client = server._get_client(
            self._bearer_ctx("req-token", "req-project")
        )
        self.assertEqual(client.project_id, "req-project")

    def test_env_client_sends_the_env_bearer(self):
        client = server._get_client()
        self.assertEqual(
            client._http.headers.get("authorization"), "Bearer env-token"
        )
        self.assertEqual(client.project_id, "env-project")

    def test_non_bearer_authorization_client_sends_no_authorization(self):
        """An unresolvable Authorization yields a token-less client — the
        backend answers its own 401 verbatim."""
        ctx = _ctx_with_headers(Headers({"Authorization": "Basic abc"}))
        client = server._get_client(ctx)
        self.assertIsNone(client._http.headers.get("authorization"))

    def test_tokenless_env_sends_no_authorization(self):
        config_patcher = patch.object(
            server,
            "get_config",
            return_value=S7Config(
                base_url="http://backend.test", token=None, project_id=None
            ),
        )
        config_patcher.start()
        self.addCleanup(config_patcher.stop)
        client = server._get_client()
        self.assertIsNone(client._http.headers.get("authorization"))
        self.assertIsNone(client.project_id)
        self.assertEqual(set(server._clients), {(None, None)})

    # -- config resolution ---------------------------------------------------

    def test_config_resolved_with_require_token_false(self):
        """Under the shared handler path the env token is only a fallback —
        ``S7_TOKEN`` must not be required to build a client."""
        server._get_client(self._bearer_ctx("req-token"))
        self.get_config.assert_called_with(require_token=False)

    def test_config_resolved_per_call_clients_cached(self):
        """Every ``_get_client`` re-reads the environment; only the built
        client is cached."""
        server._get_client()
        server._get_client(self._bearer_ctx("req-token"))
        self.assertEqual(self.get_config.call_count, 2)
        self.assertEqual(len(server._clients), 2)

    def test_non_credential_config_fields_preserved(self):
        """The client is built from ``replace(config, token=, project_id=)``
        — non-credential fields (base_url, limits) pass through untouched."""
        with patch.object(server, "S7Client") as cls:
            server._get_client(self._bearer_ctx("req-token", "req-project"))
        config = cls.call_args[0][0]
        self.assertEqual(config.token, "req-token")
        self.assertEqual(config.project_id, "req-project")
        self.assertEqual(config.base_url, self.ENV.base_url)
        self.assertEqual(config.max_payload_kb, self.ENV.max_payload_kb)
        self.assertEqual(config.timeout_s, self.ENV.timeout_s)

    # -- bounded LRU ------------------------------------------------------

    @staticmethod
    def _clients_by_token(cls):
        """Wire a mocked ``S7Client`` class to return a fresh named mock per
        call, remembered by the resolved token."""
        made = {}

        def _make(config):
            client = MagicMock(name=f"S7Client({config.token})")
            made[config.token] = client
            return client

        cls.side_effect = _make
        return made

    def test_insert_past_the_bound_evicts_and_closes_the_oldest(self):
        """Beyond ``_MAX_CACHED_CLIENTS`` the least-recently-used entry is
        evicted and its ``httpx`` pool is closed."""
        with patch.object(server, "_MAX_CACHED_CLIENTS", 2), patch.object(
            server, "S7Client"
        ) as cls:
            made = self._clients_by_token(cls)
            server._get_client(self._bearer_ctx("token-a"))
            server._get_client(self._bearer_ctx("token-b"))
            server._get_client(self._bearer_ctx("token-c"))
        made["token-a"]._http.close.assert_called_once_with()
        made["token-b"]._http.close.assert_not_called()
        made["token-c"]._http.close.assert_not_called()
        self.assertEqual(
            set(server._clients),
            {("token-b", "env-project"), ("token-c", "env-project")},
        )

    def test_hit_refreshes_the_entry_against_eviction(self):
        """A cache hit moves the entry to the most-recently-used end — it
        survives the eviction a later insert triggers."""
        with patch.object(server, "_MAX_CACHED_CLIENTS", 2), patch.object(
            server, "S7Client"
        ) as cls:
            made = self._clients_by_token(cls)
            server._get_client(self._bearer_ctx("token-a"))
            server._get_client(self._bearer_ctx("token-b"))
            server._get_client(self._bearer_ctx("token-a"))  # hit → refresh
            server._get_client(self._bearer_ctx("token-c"))
        made["token-b"]._http.close.assert_called_once_with()
        made["token-a"]._http.close.assert_not_called()
        self.assertEqual(
            set(server._clients),
            {("token-a", "env-project"), ("token-c", "env-project")},
        )

    def test_env_entry_is_evicted_like_any_other(self):
        """The env credential is one LRU entry — no special-casing: once it
        is the least recently used it is evicted and closed."""
        with patch.object(server, "_MAX_CACHED_CLIENTS", 2), patch.object(
            server, "S7Client"
        ) as cls:
            made = self._clients_by_token(cls)
            server._get_client()  # ("env-token", "env-project")
            server._get_client(self._bearer_ctx("token-a"))
            server._get_client(self._bearer_ctx("token-b"))
        made["env-token"]._http.close.assert_called_once_with()
        self.assertEqual(
            set(server._clients),
            {("token-a", "env-project"), ("token-b", "env-project")},
        )

    def test_env_entry_refreshes_on_hit(self):
        """Hitting the env credential refreshes its LRU position too — it
        survives the eviction a later insert triggers."""
        with patch.object(server, "_MAX_CACHED_CLIENTS", 2), patch.object(
            server, "S7Client"
        ) as cls:
            made = self._clients_by_token(cls)
            server._get_client()
            server._get_client(self._bearer_ctx("token-a"))
            server._get_client()  # env hit → most recently used
            server._get_client(self._bearer_ctx("token-b"))
        made["token-a"]._http.close.assert_called_once_with()
        made["env-token"]._http.close.assert_not_called()
        self.assertEqual(
            set(server._clients),
            {("env-token", "env-project"), ("token-b", "env-project")},
        )

    def test_eviction_close_failure_still_serves_the_request(self):
        """A pool that raises on ``close()`` is evicted anyway — a broken
        pool must not break the request that needed the slot."""
        with patch.object(server, "_MAX_CACHED_CLIENTS", 1), patch.object(
            server, "S7Client"
        ) as cls:
            made = self._clients_by_token(cls)
            server._get_client(self._bearer_ctx("token-a"))
            made["token-a"]._http.close.side_effect = RuntimeError("broken")
            client = server._get_client(self._bearer_ctx("token-b"))
        self.assertIs(client, made["token-b"])
        self.assertEqual(set(server._clients), {("token-b", "env-project")})

    def test_zero_bound_never_evicts_from_an_empty_cache(self):
        """A degenerate bound (``0``, reachable only via patching) cannot
        evict — an empty cache has no oldest entry to pop, so the miss
        builds and inserts its client normally instead of raising
        ``KeyError``."""
        with patch.object(server, "_MAX_CACHED_CLIENTS", 0), patch.object(
            server, "S7Client"
        ) as cls:
            made = self._clients_by_token(cls)
            client = server._get_client(self._bearer_ctx("token-a"))
        self.assertIs(client, made["token-a"])
        self.assertEqual(
            set(server._clients), {("token-a", "env-project")}
        )

    def test_evicted_credential_reinserts_a_fresh_client(self):
        """Re-requesting an evicted credential builds a new ``S7Client`` —
        never the evicted instance whose pool was already closed."""
        with patch.object(server, "_MAX_CACHED_CLIENTS", 1), patch.object(
            server, "S7Client"
        ) as cls:
            made = self._clients_by_token(cls)
            first = server._get_client(self._bearer_ctx("token-a"))
            server._get_client(self._bearer_ctx("token-b"))  # evicts token-a
            second = server._get_client(self._bearer_ctx("token-a"))
        self.assertIs(second, made["token-a"])
        self.assertIsNot(second, first)
        first._http.close.assert_called_once_with()
        second._http.close.assert_not_called()

    def test_each_insert_past_the_bound_evicts_the_oldest(self):
        """Every insert past the bound evicts exactly the oldest entry —
        four inserts under bound=2 close the two evicted pools."""
        with patch.object(server, "_MAX_CACHED_CLIENTS", 2), patch.object(
            server, "S7Client"
        ) as cls:
            made = self._clients_by_token(cls)
            for token in ("token-a", "token-b", "token-c", "token-d"):
                server._get_client(self._bearer_ctx(token))
        made["token-a"]._http.close.assert_called_once_with()
        made["token-b"]._http.close.assert_called_once_with()
        made["token-c"]._http.close.assert_not_called()
        made["token-d"]._http.close.assert_not_called()
        self.assertEqual(
            set(server._clients),
            {("token-c", "env-project"), ("token-d", "env-project")},
        )

    def test_concurrent_first_requests_build_a_single_client(self):
        """Two racing ``_get_client`` calls for one new credential build
        exactly one ``S7Client`` — the loser hits the cache, so no pool is
        orphaned."""
        built = []

        def _make(config):
            time.sleep(0.01)  # widen the get-or-create window
            client = MagicMock(name="S7Client")
            built.append(client)
            return client

        barrier = threading.Barrier(2)
        results, errors = [], []

        def _worker():
            try:
                barrier.wait()
                results.append(server._get_client(self._bearer_ctx("tok")))
            except Exception as exc:  # surfaced below, not lost in-thread
                errors.append(exc)

        with patch.object(server, "S7Client", side_effect=_make) as cls:
            threads = [threading.Thread(target=_worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(cls.call_count, 1)
        self.assertEqual(len(results), 2)
        self.assertIs(results[0], built[0])
        self.assertIs(results[1], built[0])
        self.assertEqual(len(server._clients), 1)


@unittest.skipUnless(_DEPS, "requires mcp + httpx")
class MainTransportTests(unittest.TestCase):
    """``main(argv)`` — transport dispatch, config gating and argparse.

    ``mcp.run`` and ``get_config`` are always mocked: no port is bound and
    no event loop is started.
    """

    def _run_main(self, argv):
        with patch.object(server, "get_config") as get_config, patch.object(
            server.mcp, "run"
        ) as run:
            server.main(argv)
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
        """mcp 1.x compat: when ``run`` rejects the bind kwargs the host/port
        go on ``mcp.settings`` and ``run`` is retried with the transport
        only."""
        run = MagicMock(side_effect=[TypeError("unexpected kwargs"), None])
        settings = types.SimpleNamespace(host="127.0.0.1", port=8000)
        with patch.object(server, "get_config"), patch.object(
            server.mcp, "run", run
        ), patch.object(server.mcp, "settings", settings):
            server.main(
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
            server, "get_config", side_effect=SystemExit(2)
        ), patch.object(server.mcp, "run") as run:
            with self.assertRaises(SystemExit) as cm:
                server.main(["--transport", "streamable-http"])
        self.assertEqual(cm.exception.code, 2)
        run.assert_not_called()

    def test_invalid_transport_is_an_argparse_error(self):
        """Unknown transport: ``SystemExit(2)`` with usage on stderr —
        config and run are never reached, stdout stays clean."""
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(server, "get_config") as get_config, patch.object(
            server.mcp, "run"
        ) as run, contextlib.redirect_stdout(
            stdout
        ), contextlib.redirect_stderr(
            stderr
        ):
            with self.assertRaises(SystemExit) as cm:
                server.main(["--transport", "bogus"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("invalid choice", stderr.getvalue())
        self.assertIn("usage:", stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")
        get_config.assert_not_called()
        run.assert_not_called()

    def test_help_exits_zero_with_usage_on_stdout(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
            stderr
        ):
            with self.assertRaises(SystemExit) as cm:
                server.main(["--help"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("usage:", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")


@unittest.skipUnless(_DEPS, "requires mcp + httpx")
class ContextInjectionTests(unittest.TestCase):
    """``ctx`` is SDK-injected only — never part of any ``inputSchema``."""

    def _tools_registry(self):
        manager = getattr(server.mcp, "_tool_manager", None)
        tools = getattr(manager, "_tools", None)
        return tools if isinstance(tools, dict) else {}

    def _templates_registry(self):
        manager = getattr(server.mcp, "_resource_manager", None)
        templates = getattr(manager, "_templates", None)
        return templates if isinstance(templates, dict) else {}

    def test_every_tool_signature_has_trailing_ctx(self):
        for name in EXPECTED_TOOLS:
            with self.subTest(tool=name):
                params = inspect.signature(
                    getattr(server, name)
                ).parameters
                self.assertEqual(list(params)[-1], "ctx")
                self.assertIs(params["ctx"].default, None)

    def test_every_template_resource_signature_has_trailing_ctx(self):
        for name in TEMPLATE_RESOURCES:
            with self.subTest(resource=name):
                params = inspect.signature(
                    getattr(server, name)
                ).parameters
                self.assertEqual(list(params)[-1], "ctx")
                self.assertIs(params["ctx"].default, None)

    def test_static_resources_take_no_ctx(self):
        """v2 limitation: URI-template-less resources get no injected
        context — they resolve env-only and must not declare ``ctx``."""
        for name in STATIC_RESOURCES:
            with self.subTest(resource=name):
                params = inspect.signature(
                    getattr(server, name)
                ).parameters
                self.assertNotIn("ctx", params)

    def test_ctx_absent_from_every_tool_input_schema(self):
        """Via the SDK registry: no tool exposes ``ctx`` in ``properties``
        or ``required`` — it is transport plumbing, not agent input."""
        tools = self._tools_registry()
        if not tools:  # registry layout not introspectable on this SDK
            self.skipTest("tool registry not introspectable")
        self.assertEqual(set(tools), EXPECTED_TOOLS)
        for name, tool in tools.items():
            with self.subTest(tool=name):
                schema = tool.parameters
                self.assertNotIn("ctx", schema.get("properties", {}))
                self.assertNotIn("ctx", schema.get("required", []))

    def test_ctx_absent_from_template_resource_schemas(self):
        """Resource templates expose only their URI variables — ``ctx``
        stays out of their parameter schema too."""
        templates = self._templates_registry()
        if not templates:
            self.skipTest("template registry not introspectable")
        self.assertEqual(len(templates), len(TEMPLATE_RESOURCES))
        for uri, template in templates.items():
            with self.subTest(template=uri):
                schema = template.parameters
                self.assertNotIn("ctx", schema.get("properties", {}))
                self.assertNotIn("ctx", schema.get("required", []))

    def test_registered_surface_declares_ctx_as_context_kwarg(self):
        """The SDK marks the injected parameter on every tool and template —
        that is what keeps it out of the wire schema."""
        tools = self._tools_registry()
        for name, tool in tools.items():
            context_kwarg = getattr(tool, "context_kwarg", None)
            if context_kwarg is None:
                continue
            with self.subTest(tool=name):
                self.assertEqual(context_kwarg, "ctx")
        templates = self._templates_registry()
        for uri, template in templates.items():
            context_kwarg = getattr(template, "context_kwarg", None)
            if context_kwarg is None:
                continue
            with self.subTest(template=uri):
                self.assertEqual(context_kwarg, "ctx")


if __name__ == "__main__":
    unittest.main()
