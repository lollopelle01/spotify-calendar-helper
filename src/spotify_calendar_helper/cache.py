"""Cache locale del contenuto delle playlist del calendario (task B2).

Una playlist mensile NON cambia finché non cambia il suo `snapshot_id`: se coincide con quello
salvato, i brani si leggono da qui invece che da Spotify.

Formato del file (data/calendar_cache.json, ignorato da git perché contiene dati personali):

    {"version": 1,
     "playlists": {"<playlist_id>": {"snapshot_id": "...", "name": "...", "skipped_items": 0,
                                      "tracks": [{"id", "uri", "name", "artists": [{"id", "name"}],
                                                  "added_at", "album_name"}]}}}

Un file mancante, illeggibile, corrotto o di un'altra versione NON è un errore: la cache riparte
vuota e `warning` spiega cosa è successo. La scrittura è atomica (file temporaneo + rinomina).
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import ArtistRef, Track

CACHE_VERSION = 1


@dataclass(frozen=True)
class CacheEntry:
    playlist_id: str
    snapshot_id: str | None
    name: str
    tracks: tuple[Track, ...]
    skipped_items: int = 0


def _require_str(data: Mapping[str, Any], key: str) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise ValueError(f"campo {key!r} non è una stringa")
    return value


def track_to_dict(track: Track) -> dict[str, Any]:
    return {
        "id": track.id,
        "uri": track.uri,
        "name": track.name,
        "artists": [{"id": a.id, "name": a.name} for a in track.artists],
        "added_at": track.added_at,
        "album_name": track.album_name,
        "album_id": track.album_id,
    }


def track_from_dict(data: Mapping[str, Any]) -> Track:
    """Ricostruisce un Track; solleva KeyError/TypeError/ValueError se i dati sono malformati."""
    artists_raw = data["artists"]
    if not isinstance(artists_raw, list):
        raise ValueError("campo 'artists' non è una lista")
    artists = tuple(
        ArtistRef(id=_require_str(a, "id"), name=_require_str(a, "name")) for a in artists_raw
    )
    added_at = data.get("added_at")
    if added_at is not None and not isinstance(added_at, str):
        raise ValueError("campo 'added_at' non è una stringa")
    album_name = data.get("album_name", "")
    if not isinstance(album_name, str):
        raise ValueError("campo 'album_name' non è una stringa")
    album_id = data.get("album_id", "")
    if not isinstance(album_id, str):
        album_id = ""
    return Track(
        id=_require_str(data, "id"),
        uri=_require_str(data, "uri"),
        name=_require_str(data, "name"),
        artists=artists,
        added_at=added_at,
        album_name=album_name,
        album_id=album_id,
    )


def _entry_from_dict(playlist_id: str, data: Mapping[str, Any]) -> CacheEntry:
    snapshot_id = data["snapshot_id"]
    if snapshot_id is not None and not isinstance(snapshot_id, str):
        raise ValueError("campo 'snapshot_id' non è una stringa")
    skipped = data.get("skipped_items", 0)
    if not isinstance(skipped, int) or isinstance(skipped, bool) or skipped < 0:
        raise ValueError("campo 'skipped_items' non è un intero >= 0")
    tracks_raw = data["tracks"]
    if not isinstance(tracks_raw, list):
        raise ValueError("campo 'tracks' non è una lista")
    return CacheEntry(
        playlist_id=playlist_id,
        snapshot_id=snapshot_id,
        name=_require_str(data, "name"),
        tracks=tuple(track_from_dict(t) for t in tracks_raw),
        skipped_items=skipped,
    )


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    """Scrive il JSON in un file temporaneo nella stessa cartella e poi lo rinomina."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_name, 0o600)  # contiene dati personali
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


class CalendarCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: dict[str, CacheEntry] = {}
        self.warning: str | None = None

    @classmethod
    def load(cls, path: Path) -> CalendarCache:
        cache = cls(path)
        if not path.exists():
            return cache
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            cache.warning = f"Cache del calendario illeggibile ({exc.__class__.__name__}): riparto da zero."
            return cache
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            cache.warning = "Cache del calendario di un'altra versione o non valida: riparto da zero."
            return cache
        playlists = data.get("playlists")
        if not isinstance(playlists, dict):
            cache.warning = "Cache del calendario senza sezione 'playlists': riparto da zero."
            return cache
        discarded = 0
        for playlist_id, raw in playlists.items():
            try:
                cache.entries[str(playlist_id)] = _entry_from_dict(str(playlist_id), raw)
            except (KeyError, TypeError, ValueError, AttributeError):
                discarded += 1
        if discarded:
            cache.warning = f"{discarded} voci della cache non valide scartate."
        return cache

    def lookup(self, playlist_id: str, snapshot_id: str | None) -> CacheEntry | None:
        """La voce salvata, solo se lo `snapshot_id` coincide. Senza snapshot niente cache."""
        if snapshot_id is None:
            return None
        entry = self.entries.get(playlist_id)
        if entry is None or entry.snapshot_id != snapshot_id:
            return None
        return entry

    def store(
        self,
        playlist_id: str,
        snapshot_id: str | None,
        name: str,
        tracks: Iterable[Track],
        skipped_items: int = 0,
    ) -> CacheEntry:
        entry = CacheEntry(
            playlist_id=playlist_id,
            snapshot_id=snapshot_id,
            name=name,
            tracks=tuple(tracks),
            skipped_items=skipped_items,
        )
        self.entries[playlist_id] = entry
        return entry

    def discard_except(self, keep_ids: Iterable[str]) -> int:
        """Elimina le voci di playlist che non esistono più. Restituisce quante ne ha tolte."""
        keep = set(keep_ids)
        stale = [pid for pid in self.entries if pid not in keep]
        for pid in stale:
            del self.entries[pid]
        return len(stale)

    def save(self) -> None:
        payload = {
            "version": CACHE_VERSION,
            "playlists": {
                pid: {
                    "snapshot_id": e.snapshot_id,
                    "name": e.name,
                    "skipped_items": e.skipped_items,
                    "tracks": [track_to_dict(t) for t in e.tracks],
                }
                for pid, e in self.entries.items()
            },
        }
        write_json_atomic(self.path, payload)


# ---------------------------------------------------------------------------
# Cache anni album (per group_by_decade)
# ---------------------------------------------------------------------------

ALBUM_CACHE_VERSION = 1


class AlbumYearCache:
    """Cache persistente album_id -> anno di uscita (o None se sconosciuto).

    Salvata in data/album_year_cache.json (ignorata da git).
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, int | None] = {}
        self._dirty = False
        self.warning: str | None = None

    @classmethod
    def load(cls, path: Path) -> AlbumYearCache:
        cache = cls(path)
        if not path.exists():
            return cache
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            cache.warning = f"Cache album illeggibile ({exc.__class__.__name__}): riparto da zero."
            return cache
        if not isinstance(raw, dict) or raw.get("version") != ALBUM_CACHE_VERSION:
            cache.warning = "Cache album di versione diversa: riparto da zero."
            return cache
        data = raw.get("albums", {})
        if not isinstance(data, dict):
            return cache
        for album_id, year in data.items():
            if year is None or isinstance(year, int):
                cache._data[str(album_id)] = year
        return cache

    def get(self, album_id: str) -> tuple[bool, int | None]:
        """(hit, year). Se hit=False l'album non è ancora in cache."""
        if album_id in self._data:
            return True, self._data[album_id]
        return False, None

    def set(self, album_id: str, year: int | None) -> None:
        if self._data.get(album_id, ...) != year:  # type: ignore[comparison-overlap]
            self._data[album_id] = year
            self._dirty = True

    def save(self) -> None:
        if not self._dirty:
            return
        payload = {"version": ALBUM_CACHE_VERSION, "albums": self._data}
        write_json_atomic(self.path, payload)
        self._dirty = False
