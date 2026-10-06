from __future__ import annotations

from collections.abc import Iterator

import pytest

from fixture_server import serve_fixture


@pytest.fixture(scope="session")
def fixture_site() -> Iterator[str]:
    """Base URL (no trailing slash) of the local fixture website."""
    with serve_fixture() as base_url:
        yield base_url
