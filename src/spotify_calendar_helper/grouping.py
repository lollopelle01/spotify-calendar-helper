"""Raggruppamento dei brani del calendario in playlist per artista (task C1).

Funzione pura: nessuna rete, nessun file. Riceve il calendario già letto e restituisce le
`PlaylistSpec` DESIDERATE, pronte per il sync (task D2).

Scelte documentate:
- ordine dei brani: cronologico per playlist mensile (anno, mese), poi per `added_at` dentro il mese
  (i brani senza `added_at` vanno in fondo al loro mese, mantenendo l'ordine originale);
- un brano presente in più mesi conta una sola volta, alla sua prima comparsa;
- brani senza artisti o senza URI vengono ignorati (non c'è nulla su cui raggrupparli);
- `primary_artist_only = true`: il brano va solo nella playlist del primo artista;
  `false`: va nella playlist di tutti gli artisti coinvolti;
- `sync.dedupe_same_song = true`: stessa canzone (titolo normalizzato + artista principale) in
  versioni diverse conta una volta sola; la normalizzazione toglie solo "remaster", "mono",
  "stereo", "deluxe", "bonus", "album/single version" e "radio edit", MAI "remix" o "live";
- due artisti con lo stesso nome ricevono un suffisso " (<inizio dell'ID>)" per distinguerle;
- i nomi vengono ripuliti (spazi, caratteri di controllo) e accorciati a MAX_NAME_LENGTH.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from typing import Any

from .config import DEFAULT_CONFIG, Settings
from .genres import GenreMap
from .models import ArtistRef, CalendarPlaylist, GenresReport, PlaylistSpec, Track, make_marker
from .protocols import GenreProvider

MAX_NAME_LENGTH = 100  # limite prudente per il nome di una playlist
_ELLIPSIS = "…"
_SPACES = re.compile(r"\s+")
_CONTROLS = re.compile(r"[\r\n\t]+")


def sanitize_playlist_name(name: str, max_length: int = MAX_NAME_LENGTH) -> str:
    """Toglie caratteri di controllo e spazi doppi, accorcia con «…» se troppo lungo."""
    text = _CONTROLS.sub(" ", unicodedata.normalize("NFC", name))
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf"))
    text = _SPACES.sub(" ", text).strip() or "Senza nome"
    if len(text) > max_length:
        text = text[: max_length - 1].rstrip() + _ELLIPSIS
    return text


# ---------------------------------------------------------------------------
# Brani in ordine cronologico, senza duplicati
# ---------------------------------------------------------------------------

_NOISE = re.compile(
    r"remaster|\bmono\b|\bstereo\b|deluxe|bonus|album version|single version|radio edit",
    re.IGNORECASE,
)
_BRACKETS = re.compile(r"\s*[\(\[][^()\[\]]*[\)\]]")
_DASH_SUFFIX = re.compile(r"\s+-\s+(?P<tail>.+)$")


def _strip_noisy_brackets(match: re.Match[str]) -> str:
    return "" if _NOISE.search(match.group(0)) else match.group(0)


def _strip_noisy_suffix(match: re.Match[str]) -> str:
    return "" if _NOISE.search(match.group("tail")) else match.group(0)


def normalize_title(title: str) -> str:
    text = unicodedata.normalize("NFKC", title).casefold()
    text = _BRACKETS.sub(_strip_noisy_brackets, text)
    text = _DASH_SUFFIX.sub(_strip_noisy_suffix, text)
    return _SPACES.sub(" ", text).strip()


def _tracks_by_added_at(playlist: CalendarPlaylist) -> list[Track]:
    indexed = list(enumerate(playlist.tracks))
    indexed.sort(key=lambda p: (p[1].added_at is None, p[1].added_at or "", p[0]))
    return [track for _, track in indexed]


def chronological_unique_tracks(
    calendar: Sequence[CalendarPlaylist], dedupe_same_song: bool = False
) -> list[Track]:
    seen_ids: set[str] = set()
    seen_songs: set[tuple[str, str]] = set()
    result: list[Track] = []
    for playlist in sorted(calendar, key=lambda p: p.sort_key):
        for track in _tracks_by_added_at(playlist):
            if not track.artists or not track.uri or track.id in seen_ids:
                continue
            seen_ids.add(track.id)
            if dedupe_same_song:
                song = (normalize_title(track.name), track.artists[0].id)
                if song in seen_songs:
                    continue
                seen_songs.add(song)
            result.append(track)
    return result


# ---------------------------------------------------------------------------
# Playlist per artista
# ---------------------------------------------------------------------------


def _usable_id(artist_id: str) -> bool:
    """Un ID vuoto (es. local file) o con spazi/`]` non può finire in un marker."""
    return bool(artist_id) and not re.search(r"[\s\]]", artist_id)


def _unique_names(
    qualifying: dict[str, ArtistRef], prefix: str
) -> dict[str, str]:
    """Nome per ogni artista; in caso di omonimi aggiunge un suffisso con l'inizio dell'ID."""
    base = {aid: sanitize_playlist_name(prefix + a.name) for aid, a in qualifying.items()}
    by_name: dict[str, list[str]] = {}
    for aid, name in base.items():
        by_name.setdefault(name.casefold(), []).append(aid)

    names = dict(base)
    for ids in by_name.values():
        if len(ids) < 2:
            continue
        ids.sort()
        length = 6
        while True:
            candidates = {}
            for aid in ids:
                suffix = f" ({aid[:length]})"
                stem = sanitize_playlist_name(
                    prefix + qualifying[aid].name, MAX_NAME_LENGTH - len(suffix)
                )
                candidates[aid] = stem + suffix
            unique = len({n.casefold() for n in candidates.values()}) == len(ids)
            if unique or length >= max(len(a) for a in ids):
                names.update(candidates)
                break
            length += 2
    return names


def group_by_artist(calendar: Sequence[CalendarPlaylist], settings: Settings) -> list[PlaylistSpec]:
    cfg = {**DEFAULT_CONFIG["artists"], **settings.section("artists")}
    dedupe = bool(settings.section("sync").get("dedupe_same_song", False))
    primary_only = bool(cfg["primary_artist_only"])
    min_tracks = int(cfg["min_tracks"])
    prefix = str(cfg["playlist_prefix"])
    pinned_names = {n.casefold().strip() for n in cfg.get("pinned", []) if isinstance(n, str)}

    artists: dict[str, ArtistRef] = {}
    uris_by_artist: dict[str, list[str]] = {}
    for track in chronological_unique_tracks(calendar, dedupe_same_song=dedupe):
        targets = track.artists[:1] if primary_only else track.artists
        counted: set[str] = set()
        for artist in targets:
            if not _usable_id(artist.id) or artist.id in counted:
                continue
            counted.add(artist.id)
            artists.setdefault(artist.id, artist)
            uris_by_artist.setdefault(artist.id, []).append(track.uri)

    qualifying = {
        aid: artists[aid]
        for aid, uris in uris_by_artist.items()
        if len(uris) >= min_tracks
        or artists[aid].name.casefold().strip() in pinned_names
    }
    names = _unique_names(qualifying, prefix)

    specs = [
        PlaylistSpec(
            kind="artist",
            key=aid,
            name=names[aid],
            description=(
                f"Tutti i brani di {sanitize_playlist_name(artist.name)} presenti nel calendario. "
                f"{make_marker('artist', aid)}"
            ),
            track_uris=tuple(uris_by_artist[aid]),
        )
        for aid, artist in qualifying.items()
    ]
    specs.sort(key=lambda s: (s.name.casefold(), s.key))
    return specs


# ---------------------------------------------------------------------------
# C5: Playlist per decade
# ---------------------------------------------------------------------------


def _decade_label(year: int) -> str:
    """Es. 1984 -> '1980', 2003 -> '2000', 2021 -> '2020'."""
    return str((year // 10) * 10)


def _decade_key(year: int) -> str:
    """Chiave stabile per il marker: '1980', '1990', '2000', ..."""
    return str((year // 10) * 10)


def group_by_decade(
    calendar: Sequence[CalendarPlaylist],
    client: Any,
    settings: Settings,
    album_year_cache: dict[str, int | None] | None = None,
) -> list[PlaylistSpec]:
    """Raggruppa i brani del calendario in playlist per decennio di uscita dell'album.

    Chiama GET /albums/{id} per ogni album unico trovato (con cache su disco
    in data/album_year_cache.json). I brani senza album_id o con anno sconosciuto
    vengono ignorati silenziosamente.
    """
    from .protocols import SpotifyClient  # import locale per evitare circolarità

    cfg = {**DEFAULT_CONFIG["artists"], **settings.section("artists")}
    dedupe = bool(settings.section("sync").get("dedupe_same_song", False))
    prefix = str(settings.section("decades").get("playlist_prefix", ""))
    min_tracks = int(settings.section("decades").get("min_tracks", 4))

    if album_year_cache is None:
        album_year_cache = {}

    # Risolve l'anno per ogni album_id unico
    def resolve_year(album_id: str) -> int | None:
        if album_id in album_year_cache:
            return album_year_cache[album_id]
        year = client.get_album_year(album_id)
        album_year_cache[album_id] = year
        return year

    decade_uris: dict[str, list[str]] = {}  # decade_key -> [uri, ...]
    for track in chronological_unique_tracks(calendar, dedupe_same_song=dedupe):
        if not track.album_id or not track.uri:
            continue
        year = resolve_year(track.album_id)
        if year is None:
            continue
        dk = _decade_key(year)
        decade_uris.setdefault(dk, []).append(track.uri)

    specs: list[PlaylistSpec] = []
    for dk, uris in decade_uris.items():
        if len(uris) < min_tracks:
            continue
        label = _decade_label(int(dk))
        name = sanitize_playlist_name(prefix + label)
        specs.append(
            PlaylistSpec(
                kind="decade",
                key=dk,
                name=name,
                description=(
                    f"Brani del calendario pubblicati negli {label}. "
                    f"{make_marker('decade', dk)}"
                ),
                track_uris=tuple(uris),
            )
        )
    specs.sort(key=lambda s: s.key)
    return specs


def group_by_genre(
    calendar: "Sequence[CalendarPlaylist]",
    provider: GenreProvider,
    genre_map: GenreMap,
    settings: Settings,
) -> tuple[list[PlaylistSpec], GenresReport]:
    """Raggruppa i brani del calendario in playlist per macro-genere (C4).

    Flusso:
    1. Come `group_by_artist` individua gli artisti qualificanti (>= min_tracks sopra soglia generi).
    2. Chiede al provider i generi di ciascun artista e li mappa ai macro-generi.
    3. Costruisce le `PlaylistSpec` con i brani di tutti gli artisti che appartengono al macro-genere,
       aggiungendo il brano al macro-genere anche se viene da un artista secondario
       (se `multi_genre_per_track = true`).

    La funzione è pura rispetto a Spotify (nessuna chiamata di rete diretta: le fa il `provider`).
    """
    cfg = {**DEFAULT_CONFIG["genres"], **settings.section("genres")}
    dedupe = bool(settings.section("sync").get("dedupe_same_song", False))
    primary_only = bool(settings.section("artists").get("primary_artist_only", True))
    min_tracks = int(cfg["min_tracks"])
    prefix = str(cfg["playlist_prefix"])
    multi_genre = bool(cfg["multi_genre_per_track"])

    # --- passo 1: brani in ordine cronologico, dedup ---
    all_tracks = chronological_unique_tracks(calendar, dedupe_same_song=dedupe)

    # --- passo 2: conta brani per artista (per la soglia), raccoglie generi ---
    artist_info: dict[str, ArtistRef] = {}
    track_count: dict[str, int] = {}
    for track in all_tracks:
        if not track.artists or not track.uri:
            continue
        seen_in_track: set[str] = set()
        targets = track.artists[:1] if primary_only else track.artists
        for artist in targets:
            if not _usable_id(artist.id) or artist.id in seen_in_track:
                continue
            seen_in_track.add(artist.id)
            artist_info.setdefault(artist.id, artist)
            track_count[artist.id] = track_count.get(artist.id, 0) + 1

    qualifying_ids = {aid for aid, n in track_count.items() if n >= min_tracks}

    report = GenresReport()
    artist_macros: dict[str, list[str]] = {}  # artist_id -> lista di macro-generi

    for aid in qualifying_ids:
        artist = artist_info[aid]
        try:
            raw_genres = provider.genres_for_artist(aid, artist.name)
        except Exception:
            raw_genres = []
        macros, unmapped = genre_map.resolve_tags(raw_genres)
        artist_macros[aid] = macros
        for tag in unmapped:
            report.unmapped_genres[tag] = report.unmapped_genres.get(tag, 0) + 1
        if not macros:
            report.artists_without_genres.append(artist)

    # --- passo 3: costruisce le playlist per macro-genere ---
    macro_uris: dict[str, list[str]] = {}
    for track in all_tracks:
        if not track.artists or not track.uri:
            continue
        seen_macros: set[str] = set()
        # Usa tutti gli artisti del brano per trovare macro-generi, ma solo quelli qualificanti
        for artist in track.artists:
            if not _usable_id(artist.id) or artist.id not in artist_macros:
                continue
            for macro in artist_macros[artist.id]:
                if macro in seen_macros:
                    continue
                seen_macros.add(macro)
                macro_uris.setdefault(macro, []).append(track.uri)
                if not multi_genre:
                    break
            if not multi_genre and seen_macros:
                break

    # Applica la soglia `min_tracks` anche ai macro-generi
    specs: list[PlaylistSpec] = []
    for macro, uris in macro_uris.items():
        if len(uris) < min_tracks:
            continue
        slug = genre_map.slug(macro)
        name = sanitize_playlist_name(prefix + macro)
        specs.append(
            PlaylistSpec(
                kind="genre",
                key=slug,
                name=name,
                description=(
                    f"Brani del calendario nel genere {sanitize_playlist_name(macro)}. "
                    f"{make_marker('genre', slug)}"
                ),
                track_uris=tuple(uris),
            )
        )
    specs.sort(key=lambda s: (s.name.casefold(), s.key))
    return specs, report
