import sys

import pytest


@pytest.fixture(autouse=True)
def _fresh_search_cache(monkeypatch):
    """Tests start with the search cache off and empty, so one search's answer is never served to another.
    tests/test_search_cache.py turns it on for itself."""
    server = sys.modules.get("mcp_server")
    if server is not None:
        monkeypatch.setattr(server, "SEARCH_CACHE_SECONDS", 0)
        server.clear_search_cache()
    yield
