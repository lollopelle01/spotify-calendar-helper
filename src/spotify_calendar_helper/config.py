"""Caricamento di impostazioni e segreti.

I segreti (Client ID, chiave Last.fm) arrivano SOLO dal file .env o dalle variabili d'ambiente.
Le regole di funzionamento arrivano da config.toml (o, in mancanza, config.example.toml),
sovrapposte ai valori di default definiti in DEFAULT_CONFIG.
"""

from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]

# Deve restare allineato a config.example.toml.
DEFAULT_CONFIG: dict[str, dict[str, Any]] = {
    "calendar": {
        "name_regex": "",
        "ignore_playlist_ids": [],
    },
    "artists": {
        "min_tracks": 4,
        "primary_artist_only": True,
        "playlist_prefix": "Artista - ",
        "pinned": [],
    },
    "decades": {
        "min_tracks": 4,
        "playlist_prefix": "",
    },
    "genres": {
        "enabled": True,
        "provider": "musicbrainz",
        "min_tracks": 8,
        "playlist_prefix": "Genere - ",
        "map_file": "genre_map.toml",
        "multi_genre_per_track": True,
        "cache_ttl_days": 90,
    },
    "sync": {
        "mode": "additive",
        "public": False,
        "add_batch_size": 10,
        "confirm_if_creating_more_than": 20,
        "dedupe_same_song": False,
    },
}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Unisce `override` sopra `base` (ricorsivo sui dizionari), senza modificare gli input."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


@dataclass
class Settings:
    client_id: str
    redirect_uri: str
    config: dict[str, Any]
    data_dir: Path
    lastfm_api_key: str = ""
    root: Path = field(default=ROOT)

    # --- percorsi dei dati locali (tutti sotto data/, ignorata da git) ---

    @property
    def token_path(self) -> Path:
        return self.data_dir / "token.json"

    @property
    def cache_path(self) -> Path:
        return self.data_dir / "calendar_cache.json"

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def genres_cache_path(self) -> Path:
        return self.data_dir / "genres_cache.json"

    @property
    def album_year_cache_path(self) -> Path:
        return self.data_dir / "album_year_cache.json"

    @property
    def genre_map_path(self) -> Path:
        return self.root / str(self.section("genres")["map_file"])

    # --- accesso alle sezioni di configurazione ---

    def section(self, name: str) -> dict[str, Any]:
        """Restituisce una sezione di config già completata con i default."""
        return self.config.get(name, {})

    @property
    def add_batch_size(self) -> int:
        return int(self.section("sync")["add_batch_size"])

    @property
    def public_playlists(self) -> bool:
        return bool(self.section("sync")["public"])


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Legge un file TOML e lo sovrappone ai default. Senza `path` usa config.toml o l'esempio."""
    if path is None:
        # Cerca prima in data/config.toml, poi in config.toml nella root, poi nell'esempio
        data_config = ROOT / "data" / "config.toml"
        root_config = ROOT / "config.toml"
        if data_config.exists():
            path = data_config
        elif root_config.exists():
            path = root_config
        else:
            path = ROOT / "config.example.toml"
    with path.open("rb") as f:
        user_config = tomllib.load(f)
    return _merge(DEFAULT_CONFIG, user_config)


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")

    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    if not client_id:
        raise SystemExit(
            "Manca SPOTIFY_CLIENT_ID. Copia .env.example in .env e inserisci il Client ID."
        )
    redirect_uri = os.environ.get(
        "SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback"
    ).strip()

    return Settings(
        client_id=client_id,
        redirect_uri=redirect_uri,
        config=load_config(),
        data_dir=ROOT / "data",
        lastfm_api_key=os.environ.get("LASTFM_API_KEY", "").strip(),
    )


def default_settings(data_dir: Path | None = None) -> Settings:
    """Settings con i soli default e nessun segreto: pensata per i test (niente .env, niente rete)."""
    return Settings(
        client_id="test-client-id",
        redirect_uri="http://127.0.0.1:8888/callback",
        config=copy.deepcopy(DEFAULT_CONFIG),
        data_dir=data_dir or (ROOT / "data"),
    )
