"""Entry point for ``python -m s7_mcp`` — starts the stdio MCP server."""

import sys

try:
    from .server import main
except ImportError as exc:
    if exc.name and exc.name.split(".", 1)[0] in ("mcp", "httpx"):
        print(
            f"s7-author: missing dependency {exc.name!r} — install the package "
            "requirements first: pip install -r s7_mcp/requirements.txt",
            file=sys.stderr,
        )
        sys.exit(2)
    raise

if __name__ == "__main__":
    main()
