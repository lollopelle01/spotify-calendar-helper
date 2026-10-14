"""Interfacce (Protocol) condivise: il client Spotify e il provider di generi.

Il codice di B, C e D dipende solo da queste interfacce, mai da `client.py` direttamente.
Nei test si usano `FakeSpotifyClient` e `FakeGenreProvider` (tests/fakes.py).
"""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from .models import ArtistInfo, PlaylistInfo, PlaylistItems, UserInfo


@runtime_checkable
class SpotifyClient(Protocol):
    def current_user(self) -> UserInfo:
        """GET /me"""
        ...

    def list_my_playlists(self) -> list[PlaylistInfo]:
        """GET /me/playlists, tutte le pagine. Include `snapshot_id` e `description`."""
        ...

    def get_playlist_items(self, playlist_id: str) -> PlaylistItems:
        """GET /playlists/{id}/items, tutte le pagine.

        Local file, episodi e item nulli/non disponibili NON compaiono tra i brani:
        vengono contati in `PlaylistItems.skipped`.
        """
        ...

    def create_playlist(
        self, name: str, description: str = "", public: bool = False
    ) -> PlaylistInfo:
        """POST /me/playlists. Privata di default."""
        ...

    def add_items(self, playlist_id: str, uris: Sequence[str]) -> None:
        """POST /playlists/{id}/items. Spezza in batch da `add_batch_size`, mantenendo l'ordine."""
        ...

    def remove_items(self, playlist_id: str, uris: Sequence[str]) -> None:
        """DELETE /playlists/{id}/items. Da usare solo con --prune."""
        ...

    def update_details(
        self,
        playlist_id: str,
        name: str | None = None,
        description: str | None = None,
    ) -> None:
        """PUT /playlists/{id}. I parametri None non vengono toccati."""
        ...

    def get_artist(self, artist_id: str) -> ArtistInfo:
        """GET /artists/{id} (singolo: i batch sono stati rimossi). `genres` può essere vuoto."""
        ...

    def get_album_year(self, album_id: str) -> int | None:
        """GET /albums/{id} -> anno di uscita, o None se non trovato/non disponibile."""
        ...


@runtime_checkable
class GenreProvider(Protocol):
    def genres_for_artist(self, artist_id: str, artist_name: str) -> list[str]:
        """Generi/tag "fini" dell'artista (es. "bedroom pop"). Lista vuota se sconosciuti."""
        ...
