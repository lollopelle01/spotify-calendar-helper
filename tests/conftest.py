"""Fixture condivise. Nessun test deve toccare la rete, il vero .env o la cartella data/ del repo."""

from __future__ import annotations

from pathlib import Path

import pytest

from spotify_calendar_helper.config import Settings, default_settings
from tests.fakes import FakeGenreProvider, FakeSpotifyClient


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings con i default e i dati in una cartella temporanea."""
    return default_settings(data_dir=tmp_path / "data")


@pytest.fixture
def fake_client() -> FakeSpotifyClient:
    return FakeSpotifyClient()


@pytest.fixture
def fake_genres() -> FakeGenreProvider:
    return FakeGenreProvider()
