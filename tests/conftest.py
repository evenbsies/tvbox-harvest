"""Shared test configuration.

Tests run fully offline. The probe and cleaning stages are pure functions over already-fetched
bytes precisely so they can be regression-tested against recorded fixtures -- that is the whole
reason the fetch layer is a separate concern from the parsing logic.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

# Make `import harvest` work without requiring an editable install, so `pytest` is enough.
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("HARVEST_ROOT", str(REPO_ROOT))
# Never let a test accidentally reach the network.
os.environ.setdefault("HARVEST_OFFLINE", "0")

import pytest  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture()
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(autouse=True)
def _clear_blacklist_caches():
    """Blacklists and category maps are lru_cached; a test that edits them must not leak."""
    yield
    try:
        from harvest.clean.blacklist import reload_blacklists
        from harvest.clean.classify import reload_category_map

        reload_blacklists()
        reload_category_map()
    except Exception:
        pass
