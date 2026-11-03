"""Test suite for the s7-author MCP server package (stdio and
streamable-http transports).

Run from the repository root with::

    python -m unittest s7_mcp.tests
    python -m unittest discover -s s7_mcp -v

Modules that exercise optional runtime dependencies (``mcp``/``httpx``)
skip cleanly when those are not installed.
"""

import os


def load_tests(loader, tests, pattern):
    """``unittest`` discovery protocol: ``python -m unittest s7_mcp.tests``
    runs every ``test_*.py`` module under this package with fully-qualified
    names (``s7_mcp.tests.test_*``)."""
    here = os.path.dirname(os.path.abspath(__file__))
    top_level = os.path.abspath(os.path.join(here, os.pardir, os.pardir))
    return loader.discover(here, pattern or "test*.py", top_level)
