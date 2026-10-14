"""Test della configurazione: default, sovrapposizione del config utente, percorsi."""

from __future__ import annotations

from pathlib import Path

from spotify_calendar_helper.config import (
    DEFAULT_CONFIG,
    ROOT,
    default_settings,
    load_config,
)


def test_config_example_coincide_con_i_default():
    """config.example.toml e DEFAULT_CONFIG non devono divergere."""
    example = load_config(ROOT / "config.example.toml")
    assert example == DEFAULT_CONFIG


def test_config_parziale_viene_completata(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('[artists]\nmin_tracks = 2\n', encoding="utf-8")
    cfg = load_config(path)
    assert cfg["artists"]["min_tracks"] == 2
    assert cfg["artists"]["playlist_prefix"] == "Artista - "  # default mantenuto
    assert cfg["sync"]["add_batch_size"] == 10
    assert cfg["genres"]["provider"] == "musicbrainz"


def test_default_non_vengono_mutati(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('[calendar]\nignore_playlist_ids = ["x"]\n', encoding="utf-8")
    load_config(path)
    assert DEFAULT_CONFIG["calendar"]["ignore_playlist_ids"] == []


def test_percorsi_dei_dati(tmp_path: Path):
    s = default_settings(data_dir=tmp_path / "data")
    assert s.token_path == tmp_path / "data" / "token.json"
    assert s.cache_path == tmp_path / "data" / "calendar_cache.json"
    assert s.state_path == tmp_path / "data" / "state.json"
    assert s.genres_cache_path == tmp_path / "data" / "genres_cache.json"
    assert s.genre_map_path.name == "genre_map.toml"


def test_scorciatoie():
    s = default_settings()
    assert s.add_batch_size == 10
    assert s.public_playlists is False
