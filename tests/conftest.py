"""Shared fixtures. Tests never need a car, an adapter, a display or the network."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Point the app's data directory at a temp folder for every test."""
    data = tmp_path / "opentune-data"
    monkeypatch.setenv("OPENTUNE_DATA_DIR", str(data))
    return data


@pytest.fixture
def b58():
    from opentune.engines import B58

    return B58


@pytest.fixture
def b48():
    from opentune.engines import B48

    return B48


@pytest.fixture(params=["B58", "B48"])
def profile(request):
    from opentune.engines import get_profile

    return get_profile(request.param)
