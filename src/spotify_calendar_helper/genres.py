"""Generi degli artisti: provider, cache su disco e mappa verso i macro-generi (task C2 e C3).

Struttura:
- `GenreProvider` (Protocol in protocols.py): `genres_for_artist(artist_id, artist_name) -> list[str]`;
- provider concreti: `SpotifyGenreProvider`, `MusicBrainzGenreProvider`, `LastFmGenreProvider`;
- `CachingGenreProvider` + `GenresCache`: cache su disco con TTL; anche "nessun genere trovato"
  viene ricordato, cos\u00ec un artista sconosciuto non viene richiesto a ogni run; una voce vale solo
  per il provider che l'ha prodotta (cambiando provider in config si ricomincia);
- `build_provider(settings, ...)`: costruisce il provider scelto in `[genres] provider` con la cache;
- `GenreMap`: legge `genre_map.toml` e traduce i tag fini ("bedroom pop") nei macro-generi.

Il rate limit \u00e8 un parametro dei provider (MusicBrainz ~1 richiesta/secondo, Last.fm 4/secondo):
non c'\u00e8 una chiave in config.toml perch\u00e9 `config.py` appartiene al contratto.
Nei messaggi d'errore non compaiono mai i parametri della richiesta (c'\u00e8 la chiave Last.fm).
"""

from __future__ import annotations

import json
import re
import time
import tomllib
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .cache import write_json_atomic
from .config import DEFAULT_CONFIG, Settings
from .protocols import SpotifyClient

DEFAULT_USER_AGENT = f"spotify-calendar-helper/{__version__} ( personal tool )"
GENRES_CACHE_VERSION = 1


class GenreProviderError(Exception):
    """Il provider di generi non ha risposto in modo utilizzabile (rete, limiti, risposta strana)."""


# ---------------------------------------------------------------------------
# Rate limit e HTTP
# ---------------------------------------------------------------------------


class RateLimiter:
    """Garantisce almeno `min_interval` secondi tra due chiamate (clock e sleep iniettabili)."""

    def __init__(
        self,
        min_interval: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval = max(0.0, float(min_interval))
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None:
            remaining = self._last + self.min_interval - self._clock()
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()


class _HttpJson:
    """GET con rate limit, retry su 429/503/errori di rete e risposta JSON."""

    def __init__(
        self,
        http_get: Callable[..., Any] | None,
        limiter: RateLimiter,
        user_agent: str,
        sleep: Callable[[float], None],
        max_attempts: int = 3,
    ) -> None:
        self._http_get = http_get
        self._limiter = limiter
        self._user_agent = user_agent
        self._sleep = sleep
        self._max_attempts = max_attempts

    def _get(self) -> Callable[..., Any]:
        if self._http_get is None:
            import requests  # import tardivo: serve solo con i provider di rete

            self._http_get = requests.get
        return self._http_get

    def get_json(self, url: str, params: Mapping[str, Any], *, not_found_ok: bool = False) -> Any:
        last_problem = ""
        for attempt in range(1, self._max_attempts + 1):
            self._limiter.wait()
            try:
                response = self._get()(
                    url,
                    params=dict(params),
                    headers={"User-Agent": self._user_agent, "Accept": "application/json"},
                    timeout=30,
                )
            except OSError as exc:  # include i timeout e gli errori di requests
                last_problem = f"errore di rete ({exc.__class__.__name__})"
            else:
                status = response.status_code
                if status == 404 and not_found_ok:
                    return None
                if status in (429, 503):
                    last_problem = f"HTTP {status}"
                    self._sleep(_retry_delay(response, attempt))
                    continue
                if status >= 400:
                    raise GenreProviderError(f"{url}: HTTP {status}")
                try:
                    return response.json()
                except ValueError as exc:
                    raise GenreProviderError(f"{url}: risposta non in formato JSON") from exc
            self._sleep(min(2.0 * attempt, 10.0))
        raise GenreProviderError(f"{url}: nessuna risposta utilizzabile dopo {self._max_attempts} tentativi ({last_problem})")


def _retry_delay(response: Any, attempt: int) -> float:
    raw = getattr(response, "headers", {}).get("Retry-After")
    try:
        return min(max(float(raw), 0.0), 60.0)
    except (TypeError, ValueError):
        return min(2.0 * attempt, 10.0)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class SpotifyGenreProvider:
    """Usa `GET /artists/{id}`. Dopo le modifiche del 2026 `genres` pu\u00f2 arrivare vuoto (D1)."""

    source = "spotify"

    def __init__(self, client: SpotifyClient) -> None:
        self._client = client

    def genres_for_artist(self, artist_id: str, artist_name: str) -> list[str]:
        return [g for g in self._client.get_artist(artist_id).genres if g]


_LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')


def _lucene_escape(text: str) -> str:
    return _LUCENE_SPECIAL.sub(r"\\\1", text)


def _names_by_count(items: Any, limit: int) -> list[str]:
    """Nomi dei tag/generi ordinati per conteggio decrescente (poi per nome), scartando i voti <= 0."""
    ranked: list[tuple[int, str]] = []
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name", "")).strip().casefold()
        try:
            count = int(item.get("count", 1))
        except (TypeError, ValueError):
            count = 1
        if name and count > 0:
            ranked.append((count, name))
    ranked.sort(key=lambda p: (-p[0], p[1]))
    return list(dict.fromkeys(name for _, name in ranked))[:limit]


class MusicBrainzGenreProvider:
    """Cerca l'artista per nome (solo corrispondenza esatta e punteggio alto) e ne legge generi/tag.

    Limite del servizio: 1 richiesta al secondo, con User-Agent identificabile.
    """

    source = "musicbrainz"
    BASE = "https://musicbrainz.org/ws/2"

    def __init__(
        self,
        http_get: Callable[..., Any] | None = None,
        limiter: RateLimiter | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        sleep: Callable[[float], None] = time.sleep,
        max_tags: int = 5,
        min_score: int = 90,
    ) -> None:
        self._http = _HttpJson(http_get, limiter or RateLimiter(1.1), user_agent, sleep)
        self._max_tags = max_tags
        self._min_score = min_score

    def genres_for_artist(self, artist_id: str, artist_name: str) -> list[str]:
        wanted = artist_name.strip().casefold()
        if not wanted:
            return []
        found = self._http.get_json(
            f"{self.BASE}/artist/",
            {"query": f'artist:"{_lucene_escape(artist_name.strip())}"', "fmt": "json", "limit": 5},
        )
        candidates = [
            a
            for a in (found or {}).get("artists", [])
            if isinstance(a, Mapping)
            and str(a.get("name", "")).strip().casefold() == wanted
            and int(a.get("score", 0) or 0) >= self._min_score
        ]
        if not candidates:
            return []
        best = max(candidates, key=lambda a: int(a.get("score", 0) or 0))

        tags = self._tags(best)
        mbid = best.get("id")
        if not tags and mbid:
            detail = self._http.get_json(
                f"{self.BASE}/artist/{mbid}", {"inc": "genres tags", "fmt": "json"}, not_found_ok=True
            )
            tags = self._tags(detail or {})
        return tags

    def _tags(self, artist: Mapping[str, Any]) -> list[str]:
        return _names_by_count(artist.get("genres"), self._max_tags) or _names_by_count(
            artist.get("tags"), self._max_tags
        )


# Tag di Last.fm che non descrivono un genere.
_LASTFM_NOISE = frozenset(
    {
        "seen live", "favorites", "favourites", "favorite", "favourite", "albums i own",
        "my favorites", "love", "awesome", "good", "best", "male vocalists", "female vocalists",
        "under 2000 listeners", "spotify",
    }
)


class LastFmGenreProvider:
    """`artist.getTopTags` di Last.fm (serve `LASTFM_API_KEY`, gratuita)."""

    source = "lastfm"
    BASE = "https://ws.audioscrobbler.com/2.0/"

    def __init__(
        self,
        api_key: str,
        http_get: Callable[..., Any] | None = None,
        limiter: RateLimiter | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        sleep: Callable[[float], None] = time.sleep,
        max_tags: int = 5,
        min_relative_count: int = 10,
    ) -> None:
        if not api_key:
            raise ValueError("Manca LASTFM_API_KEY: aggiungila a .env per usare il provider lastfm.")
        self._api_key = api_key
        self._http = _HttpJson(http_get, limiter or RateLimiter(0.25), user_agent, sleep)
        self._max_tags = max_tags
        self._min_relative = min_relative_count

    def genres_for_artist(self, artist_id: str, artist_name: str) -> list[str]:
        name = artist_name.strip()
        if not name:
            return []
        data = self._http.get_json(
            self.BASE,
            {
                "method": "artist.getTopTags",
                "artist": name,
                "api_key": self._api_key,
                "format": "json",
                "autocorrect": 1,
            },
        )
        if isinstance(data, Mapping) and "error" in data:
            if data.get("error") == 6:  # artista non trovato
                return []
            raise GenreProviderError(f"Last.fm: errore {data.get('error')} ({data.get('message', '')})")

        tags = (data or {}).get("toptags", {}).get("tag", [])
        if isinstance(tags, Mapping):  # con un solo tag Last.fm restituisce un oggetto, non una lista
            tags = [tags]
        parsed = [
            (str(t.get("name", "")).strip().casefold(), _to_int(t.get("count")))
            for t in tags
            if isinstance(t, Mapping)
        ]
        top = max((c for _, c in parsed), default=0)
        keep = [
            (n, c)
            for n, c in parsed
            if n and n not in _LASTFM_NOISE and n != name.casefold() and c * 100 >= top * self._min_relative
        ]
        keep.sort(key=lambda p: (-p[1], p[0]))
        return list(dict.fromkeys(n for n, _ in keep))[: self._max_tags]


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Cache su disco
# ---------------------------------------------------------------------------


class GenresCache:
    """data/genres_cache.json: `{"version": 1, "artists": {id: {genres, fetched_at, source}}}`.

    Come le altre cache locali, un file mancante o corrotto non \u00e8 un errore (`warning`)."""

    def __init__(
        self,
        path: Path,
        ttl_days: int = 90,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = path
        self.ttl = timedelta(days=ttl_days)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.entries: dict[str, dict[str, Any]] = {}
        self.warning: str | None = None
        self.dirty = False

    @classmethod
    def load(
        cls, path: Path, ttl_days: int = 90, now: Callable[[], datetime] | None = None
    ) -> GenresCache:
        cache = cls(path, ttl_days, now)
        if not path.exists():
            return cache
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            cache.warning = f"Cache dei generi illeggibile ({exc.__class__.__name__}): riparto da zero."
            return cache
        if not isinstance(data, dict) or data.get("version") != GENRES_CACHE_VERSION:
            cache.warning = "Cache dei generi di un'altra versione o non valida: riparto da zero."
            return cache
        artists = data.get("artists")
        if not isinstance(artists, dict):
            cache.warning = "Cache dei generi senza sezione 'artists': riparto da zero."
            return cache
        discarded = 0
        for artist_id, raw in artists.items():
            if _valid_entry(raw):
                cache.entries[str(artist_id)] = {
                    "genres": list(raw["genres"]),
                    "fetched_at": raw["fetched_at"],
                    "source": raw["source"],
                }
            else:
                discarded += 1
        if discarded:
            cache.warning = f"{discarded} voci della cache dei generi non valide scartate."
        return cache

    def get(self, artist_id: str, source: str) -> list[str] | None:
        """I generi salvati, se la voce \u00e8 fresca e viene dallo stesso provider; altrimenti None."""
        entry = self.entries.get(artist_id)
        if entry is None or entry["source"] != source:
            return None
        try:
            fetched = datetime.fromisoformat(entry["fetched_at"])
        except ValueError:
            return None
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        if self._now() - fetched >= self.ttl:
            return None
        return list(entry["genres"])

    def put(self, artist_id: str, genres: Sequence[str], source: str) -> None:
        self.entries[artist_id] = {
            "genres": list(genres),
            "fetched_at": self._now().isoformat(timespec="seconds"),
            "source": source,
        }
        self.dirty = True

    def save(self) -> None:
        write_json_atomic(self.path, {"version": GENRES_CACHE_VERSION, "artists": self.entries})
        self.dirty = False


def _valid_entry(raw: Any) -> bool:
    return (
        isinstance(raw, Mapping)
        and isinstance(raw.get("genres"), list)
        and all(isinstance(g, str) for g in raw["genres"])
        and isinstance(raw.get("fetched_at"), str)
        and isinstance(raw.get("source"), str)
    )


class CachingGenreProvider:
    """Mette una cache con TTL davanti a un provider. Si salva ogni `autosave_every` richieste
    vere (per non perdere il lavoro se il run si interrompe) e con `save()` a fine run."""

    def __init__(self, inner: Any, cache: GenresCache, source: str, autosave_every: int = 25) -> None:
        self._inner = inner
        self.cache = cache
        self.source = source
        self._autosave_every = max(1, autosave_every)
        self.hits = 0
        self.misses = 0

    def genres_for_artist(self, artist_id: str, artist_name: str) -> list[str]:
        cached = self.cache.get(artist_id, self.source)
        if cached is not None:
            self.hits += 1
            return cached
        genres = list(self._inner.genres_for_artist(artist_id, artist_name))
        self.misses += 1
        self.cache.put(artist_id, genres, self.source)
        if self.misses % self._autosave_every == 0:
            self.save()
        return genres

    def save(self) -> None:
        if self.cache.dirty:
            self.cache.save()


_RATE_LIMITS = {"spotify": 0.0, "musicbrainz": 1.1, "lastfm": 0.25}


def build_provider(
    settings: Settings,
    client: SpotifyClient | None = None,
    http_get: Callable[..., Any] | None = None,
    rate_limit_seconds: float | None = None,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> CachingGenreProvider:
    """Il provider scelto in `[genres] provider`, con la cache su `data/genres_cache.json`."""
    cfg = {**DEFAULT_CONFIG["genres"], **settings.section("genres")}
    name = str(cfg["provider"]).strip().lower()
    if name not in _RATE_LIMITS:
        valid = ", ".join(sorted(_RATE_LIMITS))
        raise ValueError(f"Provider di generi sconosciuto: {name!r} (validi: {valid})")

    limiter = RateLimiter(_RATE_LIMITS[name] if rate_limit_seconds is None else rate_limit_seconds, sleep=sleep)
    inner: Any
    if name == "spotify":
        if client is None:
            raise ValueError("Il provider 'spotify' richiede un client Spotify.")
        inner = SpotifyGenreProvider(client)
    elif name == "musicbrainz":
        inner = MusicBrainzGenreProvider(http_get=http_get, limiter=limiter, sleep=sleep)
    else:
        inner = LastFmGenreProvider(settings.lastfm_api_key, http_get=http_get, limiter=limiter, sleep=sleep)

    cache = GenresCache.load(settings.genres_cache_path, ttl_days=int(cfg["cache_ttl_days"]), now=now)
    return CachingGenreProvider(inner, cache, source=name)


# ---------------------------------------------------------------------------
# C3: mappa generi fini -> macro-generi
# ---------------------------------------------------------------------------


def normalize_tag(tag: str) -> str:
    """Minuscolo, senza accenti n\u00e9 trattini: "Hip-Hop" e "hip hop" diventano la stessa cosa."""
    text = unicodedata.normalize("NFKD", tag)
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    text = text.replace("_", " ").replace("-", " ")
    text = re.sub(r"[^\w&+ ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def macro_slug(name: str) -> str:
    """Chiave stabile per il marker `[sch:genre:<slug>]`: "R&B" -> "r-and-b"."""
    text = normalize_tag(name).replace("&", " and ").replace("+", " plus ")
    slug = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return slug or "genere"


class GenreMap:
    """Macro-generi con i loro sottogeneri/tag (da `genre_map.toml`).

    Risoluzione di un tag: 1) corrispondenza esatta con un sottogenere (o con il nome del
    macro-genere); 2) altrimenti il sottogenere pi\u00f9 lungo che compare nel tag come parola intera
    ("italian hip hop" contiene "hip hop"); a parit\u00e0 vince il macro-genere scritto prima nel file.
    Nessuna corrispondenza => None (il tag finisce nel report dei generi non mappati).
    """

    def __init__(self, macros: Mapping[str, Sequence[str]]) -> None:
        self.macros: dict[str, tuple[str, ...]] = {}
        self._exact: dict[str, str] = {}
        self._entries: list[tuple[str, str]] = []  # (tag normalizzato, macro) in ordine di file
        slugs: dict[str, str] = {}

        for macro, tags in macros.items():
            if not isinstance(macro, str) or not macro.strip():
                raise ValueError("Nome di macro-genere vuoto o non valido")
            if isinstance(tags, (str, bytes)) or not isinstance(tags, Sequence):
                raise ValueError(f"Macro-genere {macro!r}: serve una lista di tag")
            if not all(isinstance(t, str) for t in tags):
                raise ValueError(f"Macro-genere {macro!r}: i tag devono essere stringhe")
            slug = macro_slug(macro)
            if slug in slugs:
                raise ValueError(f"I macro-generi {slugs[slug]!r} e {macro!r} hanno lo stesso slug {slug!r}")
            slugs[slug] = macro
            self.macros[macro] = tuple(tags)
            for raw in (macro, *tags):
                norm = normalize_tag(raw)
                if not norm:
                    continue
                owner = self._exact.get(norm)
                if owner is not None and owner != macro:
                    raise ValueError(f"Il tag {raw!r} \u00e8 in due macro-generi: {owner!r} e {macro!r}")
                if owner is None:
                    self._exact[norm] = macro
                    self._entries.append((norm, macro))

    @classmethod
    def load(cls, path: Path) -> GenreMap:
        try:
            with path.open("rb") as f:
                data = tomllib.load(f)
        except FileNotFoundError:
            raise ValueError(f"Mappa dei generi non trovata: {path}") from None
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"Mappa dei generi non valida ({path.name}): {exc}") from exc
        return cls(data)

    def slug(self, macro: str) -> str:
        return macro_slug(macro)

    def resolve(self, tag: str) -> str | None:
        norm = normalize_tag(tag)
        if not norm:
            return None
        exact = self._exact.get(norm)
        if exact is not None:
            return exact
        padded = f" {norm} "
        best: tuple[int, int, str] | None = None  # (lunghezza, -posizione, macro)
        for position, (entry, macro) in enumerate(self._entries):
            if f" {entry} " in padded:
                candidate = (len(entry), -position, macro)
                if best is None or candidate > best:
                    best = candidate
        return best[2] if best else None

    def resolve_tags(self, tags: Iterable[str]) -> tuple[list[str], list[str]]:
        """(macro-generi nell'ordine di prima comparsa, tag non mappati), entrambi senza doppioni."""
        macros: list[str] = []
        unmapped: list[str] = []
        for tag in tags:
            macro = self.resolve(tag)
            if macro is None:
                if tag not in unmapped:
                    unmapped.append(tag)
            elif macro not in macros:
                macros.append(macro)
        return macros, unmapped
