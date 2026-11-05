"""Thin MCP servers (stdio + streamable-http) over the REST API.

Two entry points share this package: ``s7-author`` (schema + catalog
authoring surface, ``python -m s7_mcp``) and ``s7-catalog`` (governed
catalog changesets, ``python -m s7_mcp.catalog_server``).

The package intentionally avoids importing optional third-party dependencies
(``mcp``, ``httpx``) at top level so ``import s7_mcp`` never fails on hosts
where the runtime requirements are not installed yet.
"""

__version__ = "0.2.0"
