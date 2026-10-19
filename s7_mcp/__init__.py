"""s7-author: thin MCP stdio server over the dynamic-schema REST API.

The package intentionally avoids importing optional third-party dependencies
(``mcp``, ``httpx``) at top level so ``import s7_mcp`` never fails on hosts
where the runtime requirements are not installed yet.
"""

__version__ = "0.1.0"
