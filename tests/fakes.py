"""Doppioni in memoria per i test. Nessuna rete, nessun segreto.

`FakeSpotifyClient` implementa il Protocol `SpotifyClient`; `FakeGenreProvider` implementa `GenreProvider`.

Comportamenti volutamente "scomodi", per far emergere i bug:
- l'API simulata accetta al massimo `api_max_batch` brani per richiesta di aggiunta: se chi chiama
  (o `add_batch_size`) supera il limite, si ottiene `FakeApiError` come con Spotify;
- il contenuto delle playlist di altri utenti non è leggibile (`FakeForbiddenError`);
- ogni scrittura cambia lo `snapshot_id` della playlist;
- i duplicati NON vengono scartati (come Spotify): se un sync non è idempotente, si vede.
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from typing import Sequence

from spotify_calendar_helper.models import (
    ArtistInfo,
    ArtistRef,
    PlaylistInfo,
    PlaylistItems,
    Track,
    UserInfo,
    make_marker,
    parse_marker,
)

# ---------------------------------------------------------------------------
# Costruttori di dati di prova
# ---------------------------------------------------------------------------


def make_artist(name: str, artist_id: str | None = None) -> ArtistRef:
    return ArtistRef(id=artist_id or f"artist_{name.lower().replace(' ', '_')}", name=name)


def make_track(
    track_id: str,
    name: str = "",
    artists: Sequence[ArtistRef | str] = ("Artista Uno",),
    added_at: str | None = "2025-03-15T10:00:00Z",
    album_name: str = "Album",
    album_id: str = "",
) -> Track:
    refs = tuple(a if isinstance(a, ArtistRef) else make_artist(a) for a in artists)
    return Track(
        id=track_id,
        uri=f"spotify:track:{track_id}",
        name=name or f"Brano {track_id}",
        artists=refs,
        added_at=added_at,
        album_name=album_name,
        album_id=album_id,
    )


# ---------------------------------------------------------------------------
# Errori simulati (client.py avrà i propri tipi: AuthError, RateLimitError, ApiError)
# ---------------------------------------------------------------------------


class FakeApiError(Exception):
    """Richiesta rifiutata dall'API simulata (es. batch troppo grande)."""


class FakeForbiddenError(FakeApiError):
    """Contenuto di una playlist non tua: non leggibile."""


class FakeNotFoundError(FakeApiError):
    pass


# ---------------------------------------------------------------------------
# FakeSpotifyClient
# ---------------------------------------------------------------------------


class FakeSpotifyClient:
    def __init__(
        self,
        user_id: str = "me",
        api_max_batch: int = 10,
        add_batch_size: int = 10,
        artist_genres: dict[str, Sequence[str]] | None = None,
        album_years: dict[str, int] | None = None,
    ) -> None:
        self.user_id = user_id
        self.api_max_batch = api_max_batch
        self.add_batch_size = add_batch_size
        self._playlists: dict[str, PlaylistInfo] = {}
        self._items: dict[str, list[Track]] = {}
        self._skipped: dict[str, int] = defaultdict(int)
        self._artist_genres: dict[str, tuple[str, ...]] = {
            k: tuple(v) for k, v in (artist_genres or {}).items()
        }
        self._artist_names: dict[str, str] = {}
        self._album_years: dict[str, int] = dict(album_years or {})
        self._ids = itertools.count(1)
        self._version = itertools.count(1)

        # Osservabilità per i test
        self.calls: list[tuple[str, tuple]] = []
        self.add_requests: list[int] = []  # dimensione di ogni singola richiesta di aggiunta
        self.write_count = 0  # create + add + remove + update: 0 dopo un secondo sync = idempotenza

    # --- preparazione dello scenario (non fanno parte del Protocol) ---

    def seed_playlist(
        self,
        name: str,
        tracks: Sequence[Track] = (),
        description: str = "",
        owner_id: str | None = None,
        skipped: int = 0,
        playlist_id: str | None = None,
    ) -> PlaylistInfo:
        """Aggiunge una playlist già esistente (es. una playlist mensile del calendario)."""
        pid = playlist_id or f"pl_{next(self._ids)}"
        info = PlaylistInfo(
            id=pid,
            name=name,
            description=description,
            snapshot_id=self._new_snapshot(),
            owner_id=owner_id or self.user_id,
            public=False,
            track_count=len(tracks) + skipped,
        )
        self._playlists[pid] = info
        self._items[pid] = list(tracks)
        self._skipped[pid] = skipped
        for t in tracks:
            for a in t.artists:
                self._artist_names.setdefault(a.id, a.name)
        return info

    def set_artist_genres(self, artist_id: str, genres: Sequence[str]) -> None:
        self._artist_genres[artist_id] = tuple(genres)

    def delete_playlist_for_test(self, playlist_id: str) -> None:
        """Simula una playlist cancellata a mano dall'utente."""
        self._playlists.pop(playlist_id, None)
        self._items.pop(playlist_id, None)

    def rename_for_test(self, playlist_id: str, new_name: str) -> None:
        """Simula una rinomina a mano (non cambia lo snapshot dei brani)."""
        self._playlists[playlist_id] = _replace(self._playlists[playlist_id], name=new_name)

    def uris_of(self, playlist_id: str) -> list[str]:
        return [t.uri for t in self._items[playlist_id]]

    def find_by_marker(self, kind: str, key: str) -> list[PlaylistInfo]:
        """Cerca tra le playlist per marker nella descrizione (ricerca per descrizione)."""
        marker = make_marker(kind, key)  # type: ignore[arg-type]
        return [p for p in self._playlists.values() if marker in p.description]

    def reset_counters(self) -> None:
        self.calls.clear()
        self.add_requests.clear()
        self.write_count = 0

    # --- interfaccia SpotifyClient ---

    def current_user(self) -> UserInfo:
        self.calls.append(("current_user", ()))
        return UserInfo(id=self.user_id, display_name="Utente di prova")

    def list_my_playlists(self) -> list[PlaylistInfo]:
        self.calls.append(("list_my_playlists", ()))
        # Come Spotify, restituisce anche playlist seguite di altri (owner diverso).
        return list(self._playlists.values())

    def get_playlist_items(self, playlist_id: str) -> PlaylistItems:
        self.calls.append(("get_playlist_items", (playlist_id,)))
        info = self._require(playlist_id)
        if info.owner_id != self.user_id:
            raise FakeForbiddenError(f"Contenuto non leggibile: playlist {playlist_id} non è tua")
        return PlaylistItems(
            tracks=tuple(self._items[playlist_id]), skipped=self._skipped[playlist_id]
        )

    def create_playlist(
        self, name: str, description: str = "", public: bool = False
    ) -> PlaylistInfo:
        self.calls.append(("create_playlist", (name, description, public)))
        self.write_count += 1
        pid = f"pl_{next(self._ids)}"
        info = PlaylistInfo(
            id=pid,
            name=name,
            description=description,
            snapshot_id=self._new_snapshot(),
            owner_id=self.user_id,
            public=public,
            track_count=0,
        )
        self._playlists[pid] = info
        self._items[pid] = []
        return info

    def add_items(self, playlist_id: str, uris: Sequence[str]) -> None:
        self.calls.append(("add_items", (playlist_id, tuple(uris))))
        self._require_own(playlist_id)
        size = max(1, self.add_batch_size)
        for i in range(0, len(uris), size):
            batch = list(uris[i : i + size])
            if len(batch) > self.api_max_batch:
                raise FakeApiError(
                    f"Batch di {len(batch)} brani: l'API simulata ne accetta al massimo "
                    f"{self.api_max_batch} (riduci add_batch_size)"
                )
            self.add_requests.append(len(batch))
            self.write_count += 1
            for uri in batch:
                self._items[playlist_id].append(_track_from_uri(uri))
            self._touch(playlist_id)

    def remove_items(self, playlist_id: str, uris: Sequence[str]) -> None:
        self.calls.append(("remove_items", (playlist_id, tuple(uris))))
        self._require_own(playlist_id)
        to_remove = set(uris)
        self.write_count += 1
        self._items[playlist_id] = [t for t in self._items[playlist_id] if t.uri not in to_remove]
        self._touch(playlist_id)

    def update_details(
        self,
        playlist_id: str,
        name: str | None = None,
        description: str | None = None,
    ) -> None:
        self.calls.append(("update_details", (playlist_id, name, description)))
        self._require_own(playlist_id)
        self.write_count += 1
        info = self._playlists[playlist_id]
        self._playlists[playlist_id] = _replace(
            info,
            name=info.name if name is None else name,
            description=info.description if description is None else description,
        )

    def get_artist(self, artist_id: str) -> ArtistInfo:
        self.calls.append(("get_artist", (artist_id,)))
        name = self._artist_names.get(artist_id)
        if name is None and artist_id not in self._artist_genres:
            raise FakeNotFoundError(f"Artista {artist_id} sconosciuto")
        return ArtistInfo(
            id=artist_id,
            name=name or artist_id,
            genres=self._artist_genres.get(artist_id, ()),
        )

    def get_album_year(self, album_id: str) -> int | None:
        self.calls.append(("get_album_year", (album_id,)))
        return self._album_years.get(album_id)

    # --- interni ---

    def _require(self, playlist_id: str) -> PlaylistInfo:
        try:
            return self._playlists[playlist_id]
        except KeyError:
            raise FakeNotFoundError(f"Playlist {playlist_id} inesistente") from None

    def _require_own(self, playlist_id: str) -> PlaylistInfo:
        info = self._require(playlist_id)
        if info.owner_id != self.user_id:
            raise FakeForbiddenError(f"Playlist {playlist_id} non è tua: non modificabile")
        return info

    def _new_snapshot(self) -> str:
        return f"snap_{next(self._version)}"

    def _touch(self, playlist_id: str) -> None:
        info = self._playlists[playlist_id]
        self._playlists[playlist_id] = _replace(
            info,
            snapshot_id=self._new_snapshot(),
            track_count=len(self._items[playlist_id]) + self._skipped[playlist_id],
        )


def _replace(info: PlaylistInfo, **changes) -> PlaylistInfo:
    from dataclasses import replace

    return replace(info, **changes)


def _track_from_uri(uri: str) -> Track:
    """Il fake ricostruisce un brano minimale dall'URI aggiunto (basta per confronti per URI)."""
    track_id = uri.rsplit(":", 1)[-1]
    return Track(id=track_id, uri=uri, name=track_id, artists=())


# ---------------------------------------------------------------------------
# FakeGenreProvider
# ---------------------------------------------------------------------------


class FakeGenreProvider:
    def __init__(self, genres: dict[str, Sequence[str]] | None = None) -> None:
        self._genres = {k: list(v) for k, v in (genres or {}).items()}
        self.calls: list[str] = []

    def set_genres(self, artist_id: str, genres: Sequence[str]) -> None:
        self._genres[artist_id] = list(genres)

    def genres_for_artist(self, artist_id: str, artist_name: str) -> list[str]:
        self.calls.append(artist_id)
        return list(self._genres.get(artist_id, []))


__all__ = [
    "FakeApiError",
    "FakeForbiddenError",
    "FakeNotFoundError",
    "FakeGenreProvider",
    "FakeSpotifyClient",
    "make_artist",
    "make_track",
    "make_marker",
    "parse_marker",
]
