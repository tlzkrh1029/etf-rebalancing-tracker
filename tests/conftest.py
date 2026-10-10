"""Shared pytest configuration.

The ``committed_data`` marker tags the tests that read the repository's own
committed ``data/`` and ``reports/`` (every raw issuer file, the latest
analysis, the real data directory).  They guard the parsers against the real
documents, but their assumptions can be overtaken by a surprising day, so the
daily workflow runs them after the fetch as a non-blocking check:
``pytest -m "not committed_data"`` gates the run, ``pytest -m committed_data``
reports the drift.
"""
from __future__ import annotations

import pytest

#: Substrings of a test's name (or of the fixtures it uses) that identify a
#: test reading the committed data.
COMMITTED_DATA_TESTS = (
    "every_raw_csv",
    "soxx_adr_set_is_stable",
    "raw_qqq_files",
    "real_data_directory",
    "committed",
)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "committed_data: reads the repository's committed data/ and reports/")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        name = getattr(item, "originalname", None) or item.name
        fixtures = set(getattr(item, "fixturenames", ()))
        if any(key in name for key in COMMITTED_DATA_TESTS) or fixtures & {"committed", "latest_analysis"}:
            item.add_marker(pytest.mark.committed_data)
