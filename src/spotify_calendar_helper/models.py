"""Dataclass condivise: il CONTRATTO tra gli agenti.

Non modificare senza una richiesta di tipo CONTRATTO nel registro di AGENTS.md.
Tutte le strutture sono immutabili (frozen) tranne i report, che si compilano passo passo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

PlaylistKind = Literal["artist", "genre", "decade"]

# ---------------------------------------------------------------------------
# Marker nella descrizione delle playlist derivate
# ---------------------------------------------------------------------------
# Esempio: "Tutti i brani di X dal calendario. [sch:artist:4Z8W4fKeB5YxbusRsdQVPb]"
# Il marker permette di ritrovare la playlist anche se viene rinominata o se state.json va perso.

_MARKER_RE = re.compile(r"\[sch:(?P<kind>artist|genre|decade):(?P<key>[^\]\s]+)\]")


def make_marker(kind: PlaylistKind, key: str) -> str:
    """Costruisce il marker `[sch:<kind>:<key>]`. `key` non deve contenere spazi o `]`."""
    if not key or re.search(r"[\s\]]", key):
        raise ValueError(f"Chiave non valida per il marker: {key!r}")
    return f"[sch:{kind}:{key}]"


def parse_marker(description: str | None) -> tuple[PlaylistKind, str] | None:
    """Estrae (kind, key) dalla descrizione di una playlist, o None se non c'è il marker."""
    if not description:
        return None
    m = _MARKER_RE.search(description)
    if not m:
        return None
    return m.group("kind"), m.group("key")  # type: ignore[return-value]


def make_marker_any(kind: str, key: str) -> str:
    """Come make_marker ma accetta qualsiasi kind stringa (usato per 'decade')."""
    if not key or re.search(r"[\s\]]", key):
        raise ValueError(f"Chiave non valida per il marker: {key!r}")
    return f"[sch:{kind}:{key}]"


# ---------------------------------------------------------------------------
# Brani e playlist del calendario
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtistRef:
    id: str
    name: str


@dataclass(frozen=True)
class Track:
    id: str
    uri: str  # es. "spotify:track:<id>"
    name: str
    artists: tuple[ArtistRef, ...]  # l'ordine è quello di Spotify: il primo è l'artista principale
    added_at: str | None = None  # ISO 8601, come restituito da Spotify
    album_name: str = ""
    album_id: str = ""  # ID Spotify dell'album, per GET /albums/{id}

    @property
    def primary_artist(self) -> ArtistRef | None:
        return self.artists[0] if self.artists else None


@dataclass(frozen=True)
class CalendarPlaylist:
    id: str
    name: str
    year: int
    month: int  # 1..12
    snapshot_id: str | None
    tracks: tuple[Track, ...] = ()
    skipped_items: int = 0  # local file, episodi e item nulli/non disponibili saltati in lettura

    @property
    def sort_key(self) -> tuple[int, int]:
        return (self.year, self.month)


# ---------------------------------------------------------------------------
# Playlist desiderate e risultato del sync
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlaylistSpec:
    """La playlist DESIDERATA: cosa deve esistere su Spotify dopo il sync."""

    kind: PlaylistKind
    key: str  # ID artista Spotify, oppure slug del macro-genere
    name: str
    description: str  # deve contenere make_marker(kind, key)
    track_uris: tuple[str, ...]  # in ordine cronologico, senza duplicati


@dataclass(frozen=True)
class PlaylistChange:
    kind: PlaylistKind
    key: str
    name: str
    playlist_id: str | None  # None in dry-run per le playlist non ancora create
    added: int = 0
    removed: int = 0


@dataclass
class SyncReport:
    created: list[PlaylistChange] = field(default_factory=list)
    updated: list[PlaylistChange] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)  # chiavi "<kind>:<key>"
    skipped_tracks: list[str] = field(default_factory=list)  # descrizioni leggibili
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def is_noop(self) -> bool:
        """True se il run non ha creato né modificato nulla (criterio di idempotenza)."""
        return not self.created and not self.updated


@dataclass
class GenresReport:
    unmapped_genres: dict[str, int] = field(default_factory=dict)  # tag non mappato -> n. artisti
    artists_without_genres: list[ArtistRef] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Tipi di ritorno delle chiamate al client Spotify
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UserInfo:
    id: str
    display_name: str = ""


@dataclass(frozen=True)
class PlaylistInfo:
    id: str
    name: str
    description: str
    snapshot_id: str | None
    owner_id: str
    public: bool | None = None
    track_count: int | None = None


@dataclass(frozen=True)
class PlaylistItems:
    tracks: tuple[Track, ...]
    skipped: int = 0  # local file, episodi, item nulli/non disponibili


@dataclass(frozen=True)
class ArtistInfo:
    id: str
    name: str
    genres: tuple[str, ...] = ()
