"""Test dei provider di generi, della cache e della mappa dei macro-generi (task C2 e C3).

Nessuna rete: tutte le chiamate HTTP sono intercettate con un http_get finto.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from spotify_calendar_helper.genres import (
    CachingGenreProvider,
    GenreMap,
    GenreProviderError,
    GenresCache,
    LastFmGenreProvider,
    MusicBrainzGenreProvider,
    RateLimiter,
    SpotifyGenreProvider,
    build_provider,
    macro_slug,
    normalize_tag,
)
from spotify_calendar_helper.config import default_settings
from tests.fakes import FakeSpotifyClient, make_track


# ---------------------------------------------------------------------------
# Utilità: risposta HTTP fittizia
# ---------------------------------------------------------------------------


def fake_response(data: Any, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = data
    resp.headers = {}
    return resp


def http_get_returning(data: Any, status: int = 200):
    """Restituisce una funzione `http_get` che risponde sempre con `data`."""
    def _get(url, params=None, headers=None, timeout=None):
        return fake_response(data, status)
    return _get


def http_get_sequence(*responses):
    """Restituisce una funzione `http_get` che risponde in sequenza (poi ripete l'ultima)."""
    responses = list(responses)
    calls = [0]
    def _get(url, params=None, headers=None, timeout=None):
        idx = min(calls[0], len(responses) - 1)
        calls[0] += 1
        resp, status = responses[idx] if isinstance(responses[idx], tuple) else (responses[idx], 200)
        return fake_response(resp, status)
    return _get


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------


def test_rate_limiter_chiama_sleep_se_troppo_presto() -> None:
    slept: list[float] = []
    clock_values = iter([0.0, 0.3, 1.2])  # t=0 prima chiamata, t=0.3 seconda (troppo presto), t=1.2 terza
    limiter = RateLimiter(1.0, clock=lambda: next(clock_values), sleep=slept.append)
    limiter.wait()       # prima: nessun sleep
    limiter.wait()       # seconda: deve dormire ~0.7
    assert slept and slept[0] == pytest.approx(0.7)


def test_rate_limiter_zero_non_dorme() -> None:
    slept: list[float] = []
    t = [0.0]
    limiter = RateLimiter(0.0, clock=lambda: t[0], sleep=slept.append)
    limiter.wait(); limiter.wait(); limiter.wait()
    assert slept == []


# ---------------------------------------------------------------------------
# SpotifyGenreProvider
# ---------------------------------------------------------------------------


def test_spotify_provider_restituisce_i_generi_dell_artista() -> None:
    client = FakeSpotifyClient(artist_genres={"a1": ["indie pop", "bedroom pop"]})
    client.seed_playlist("m", [make_track("t", artists=["Alfa"])], playlist_id="p")
    # sovrascriviamo l'artista noto con un id noto
    client._artist_names["a1"] = "Alfa"
    provider = SpotifyGenreProvider(client)
    assert provider.genres_for_artist("a1", "Alfa") == ["indie pop", "bedroom pop"]


def test_spotify_provider_lista_vuota_se_artista_senza_generi() -> None:
    client = FakeSpotifyClient(artist_genres={"a1": []})
    client._artist_names["a1"] = "Alfa"
    provider = SpotifyGenreProvider(client)
    assert provider.genres_for_artist("a1", "Alfa") == []


def test_spotify_provider_filtra_generi_vuoti() -> None:
    client = FakeSpotifyClient(artist_genres={"a1": ["rock", "", "pop"]})
    client._artist_names["a1"] = "Alfa"
    provider = SpotifyGenreProvider(client)
    assert provider.genres_for_artist("a1", "Alfa") == ["rock", "pop"]


# ---------------------------------------------------------------------------
# MusicBrainzGenreProvider
# ---------------------------------------------------------------------------


_MB_ARTIST_HIT = {
    "artists": [
        {"id": "mb-1", "name": "Test Artist", "score": 100,
         "genres": [{"name": "indie rock", "count": 5}, {"name": "alternative rock", "count": 3}],
         "tags": []}
    ]
}

_MB_ARTIST_NO_GENRES = {
    "artists": [{"id": "mb-2", "name": "Test Artist", "score": 95, "genres": [], "tags": []}]
}

_MB_DETAIL_WITH_TAGS = {
    "id": "mb-2", "name": "Test Artist",
    "genres": [], "tags": [{"name": "pop", "count": 10}, {"name": "dance", "count": 7}]
}


def make_mb(http_get, **kwargs) -> MusicBrainzGenreProvider:
    slept: list[float] = []
    return MusicBrainzGenreProvider(
        http_get=http_get,
        limiter=RateLimiter(0.0, clock=lambda: 0.0, sleep=slept.append),
        sleep=slept.append,
        **kwargs,
    )


def test_mb_restituisce_i_generi() -> None:
    provider = make_mb(http_get_returning(_MB_ARTIST_HIT))
    assert provider.genres_for_artist("mb-1", "Test Artist") == ["indie rock", "alternative rock"]


def test_mb_nessun_candidato_con_nome_diverso() -> None:
    data = {
        "artists": [{"id": "x", "name": "Altro Artista", "score": 100, "genres": [{"name": "pop", "count": 1}], "tags": []}]
    }
    provider = make_mb(http_get_returning(data))
    assert provider.genres_for_artist("x", "Test Artist") == []


def test_mb_nessun_candidato_sotto_min_score() -> None:
    data = {
        "artists": [{"id": "x", "name": "Test Artist", "score": 50, "genres": [{"name": "pop", "count": 1}], "tags": []}]
    }
    provider = make_mb(http_get_returning(data))
    assert provider.genres_for_artist("x", "Test Artist") == []


def test_mb_fallback_a_detail_se_genres_vuoti() -> None:
    provider = make_mb(http_get_sequence(_MB_ARTIST_NO_GENRES, _MB_DETAIL_WITH_TAGS))
    assert provider.genres_for_artist("mb-2", "Test Artist") == ["pop", "dance"]


def test_mb_nome_artista_vuoto_restituisce_lista_vuota() -> None:
    provider = make_mb(http_get_returning(_MB_ARTIST_HIT))
    assert provider.genres_for_artist("mb-1", "  ") == []


def test_mb_404_detail_non_fa_errore() -> None:
    provider = make_mb(http_get_sequence(_MB_ARTIST_NO_GENRES, (None, 404)))
    assert provider.genres_for_artist("mb-2", "Test Artist") == []


def test_mb_risposta_vuota() -> None:
    provider = make_mb(http_get_returning(None))
    assert provider.genres_for_artist("x", "Art") == []


def test_mb_http_500_solleva_errore() -> None:
    provider = make_mb(http_get_returning({}, 500))
    with pytest.raises(GenreProviderError):
        provider.genres_for_artist("x", "Art")


def test_mb_max_tags_rispettato() -> None:
    data = {
        "artists": [
            {"id": "x", "name": "Art", "score": 100,
             "genres": [{"name": f"genere{i}", "count": 10 - i} for i in range(8)],
             "tags": []}
        ]
    }
    provider = make_mb(http_get_returning(data), max_tags=3)
    assert len(provider.genres_for_artist("x", "Art")) == 3


def test_mb_tag_con_count_zero_scartati() -> None:
    data = {
        "artists": [
            {"id": "x", "name": "Art", "score": 100,
             "genres": [{"name": "rock", "count": 5}, {"name": "spam", "count": 0}],
             "tags": []}
        ]
    }
    provider = make_mb(http_get_returning(data))
    assert provider.genres_for_artist("x", "Art") == ["rock"]


def test_mb_retry_su_429() -> None:
    slept: list[float] = []
    call_n = [0]

    def flaky_get(url, params=None, headers=None, timeout=None):
        call_n[0] += 1
        if call_n[0] == 1:
            r = MagicMock()
            r.status_code = 429
            r.headers = {"Retry-After": "1"}
            return r
        return fake_response(_MB_ARTIST_HIT)

    # max_attempts va passato a _HttpJson tramite il provider: usiamo il default (3)
    provider = MusicBrainzGenreProvider(
        http_get=flaky_get,
        limiter=RateLimiter(0.0, clock=lambda: 0.0, sleep=lambda _: None),
        sleep=slept.append,
    )
    result = provider.genres_for_artist("mb-1", "Test Artist")
    assert result == ["indie rock", "alternative rock"]
    assert any(s >= 1.0 for s in slept)  # ha dormito per il Retry-After


def test_mb_esaurisce_i_tentativi_e_solleva_errore() -> None:
    slept: list[float] = []
    call_n = [0]

    def always_429(url, params=None, headers=None, timeout=None):
        call_n[0] += 1
        r = MagicMock()
        r.status_code = 429
        r.headers = {}
        return r

    # max_attempts default = 3; dopo 3 tentativi falliti deve sollevare GenreProviderError
    provider = MusicBrainzGenreProvider(
        http_get=always_429,
        limiter=RateLimiter(0.0, clock=lambda: 0.0, sleep=lambda _: None),
        sleep=slept.append,
    )
    with pytest.raises(GenreProviderError, match="nessuna risposta"):
        provider.genres_for_artist("x", "Art")


# ---------------------------------------------------------------------------
# LastFmGenreProvider
# ---------------------------------------------------------------------------


_LFM_RESPONSE = {
    "toptags": {
        "tag": [
            {"name": "indie pop", "count": 100},
            {"name": "bedroom pop", "count": 60},
            {"name": "seen live", "count": 90},   # rumore: deve essere scartato
            {"name": "favorites", "count": 80},    # rumore
        ]
    }
}

_LFM_EMPTY = {"toptags": {"tag": []}}
_LFM_ARTIST_NOT_FOUND = {"error": 6, "message": "Artist not found"}


def make_lfm(http_get, **kwargs) -> LastFmGenreProvider:
    slept: list[float] = []
    return LastFmGenreProvider(
        api_key="test-key",
        http_get=http_get,
        limiter=RateLimiter(0.0, clock=lambda: 0.0, sleep=lambda _: None),
        sleep=slept.append,
        **kwargs,
    )


def test_lfm_restituisce_i_tag_senza_rumore() -> None:
    provider = make_lfm(http_get_returning(_LFM_RESPONSE))
    assert provider.genres_for_artist("x", "Art") == ["indie pop", "bedroom pop"]


def test_lfm_artista_non_trovato_restituisce_lista_vuota() -> None:
    provider = make_lfm(http_get_returning(_LFM_ARTIST_NOT_FOUND))
    assert provider.genres_for_artist("x", "Art") == []


def test_lfm_errore_non_trovato_non_solleva_eccezione() -> None:
    provider = make_lfm(http_get_returning(_LFM_ARTIST_NOT_FOUND))
    assert provider.genres_for_artist("x", "Art") == []


def test_lfm_errore_generico_solleva_eccezione() -> None:
    provider = make_lfm(http_get_returning({"error": 10, "message": "Rate limit exceeded"}))
    with pytest.raises(GenreProviderError):
        provider.genres_for_artist("x", "Art")


def test_lfm_lista_vuota_di_tag() -> None:
    provider = make_lfm(http_get_returning(_LFM_EMPTY))
    assert provider.genres_for_artist("x", "Art") == []


def test_lfm_tag_singolo_come_dizionario() -> None:
    """Last.fm con un solo tag restituisce un dict, non una lista."""
    provider = make_lfm(http_get_returning({"toptags": {"tag": {"name": "pop", "count": 50}}}))
    assert provider.genres_for_artist("x", "Art") == ["pop"]


def test_lfm_nome_artista_scartato_come_tag() -> None:
    """Il nome dell'artista stesso non deve apparire tra i generi."""
    data = {
        "toptags": {"tag": [{"name": "Art", "count": 100}, {"name": "indie", "count": 80}]}
    }
    provider = make_lfm(http_get_returning(data))
    assert "art" not in [g.lower() for g in provider.genres_for_artist("x", "Art")]
    assert "indie" in provider.genres_for_artist("x", "Art")


def test_lfm_min_relative_count_filtra_i_tag_rari() -> None:
    data = {
        "toptags": {"tag": [
            {"name": "rock", "count": 100},
            {"name": "obscure", "count": 5},  # 5% di 100: sotto il 10% di default
        ]}
    }
    provider = make_lfm(http_get_returning(data))
    genres = provider.genres_for_artist("x", "Art")
    assert "rock" in genres and "obscure" not in genres


def test_lfm_max_tags_rispettato() -> None:
    data = {
        "toptags": {"tag": [{"name": f"tag{i}", "count": 100 - i} for i in range(10)]}
    }
    provider = make_lfm(http_get_returning(data), max_tags=3)
    assert len(provider.genres_for_artist("x", "Art")) == 3


def test_lfm_senza_api_key_solleva_errore() -> None:
    with pytest.raises(ValueError, match="LASTFM_API_KEY"):
        LastFmGenreProvider(api_key="")


def test_lfm_nome_vuoto_restituisce_lista_vuota() -> None:
    provider = make_lfm(http_get_returning(_LFM_RESPONSE))
    assert provider.genres_for_artist("x", "  ") == []


# ---------------------------------------------------------------------------
# GenresCache
# ---------------------------------------------------------------------------


def fixed_now(iso: str):
    dt = datetime.fromisoformat(iso)
    return lambda: dt


@pytest.fixture
def cache_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "genres_cache.json"


def test_cache_miss_su_file_mancante(cache_path: Path) -> None:
    cache = GenresCache.load(cache_path)
    assert cache.get("a1", "musicbrainz") is None
    assert cache.warning is None


def test_cache_roundtrip(cache_path: Path) -> None:
    cache = GenresCache(cache_path)
    cache.put("a1", ["indie rock"], "musicbrainz")
    cache.save()
    loaded = GenresCache.load(cache_path)
    assert loaded.get("a1", "musicbrainz") == ["indie rock"]


def test_cache_miss_se_provider_diverso(cache_path: Path) -> None:
    cache = GenresCache(cache_path)
    cache.put("a1", ["indie rock"], "musicbrainz")
    cache.save()
    loaded = GenresCache.load(cache_path)
    assert loaded.get("a1", "spotify") is None


def test_cache_miss_se_voce_scaduta(cache_path: Path) -> None:
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    cache = GenresCache(cache_path, ttl_days=30, now=lambda: now)
    cache.put("a1", ["pop"], "musicbrainz")
    cache.save()

    future = datetime(2025, 2, 5, tzinfo=timezone.utc)  # 35 giorni dopo -> scaduta
    loaded = GenresCache.load(cache_path, ttl_days=30, now=lambda: future)
    assert loaded.get("a1", "musicbrainz") is None


def test_cache_hit_prima_della_scadenza(cache_path: Path) -> None:
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    cache = GenresCache(cache_path, ttl_days=30, now=lambda: now)
    cache.put("a1", ["pop"], "musicbrainz")
    cache.save()

    fresh = datetime(2025, 1, 20, tzinfo=timezone.utc)  # 19 giorni dopo -> fresca
    loaded = GenresCache.load(cache_path, ttl_days=30, now=lambda: fresh)
    assert loaded.get("a1", "musicbrainz") == ["pop"]


def test_cache_lista_vuota_viene_ricordata(cache_path: Path) -> None:
    """Anche "nessun genere" va ricordato, così non si richede ogni run."""
    cache = GenresCache(cache_path)
    cache.put("a1", [], "musicbrainz")
    cache.save()
    loaded = GenresCache.load(cache_path)
    result = loaded.get("a1", "musicbrainz")
    assert result == []


def test_cache_file_corrotto_da_warning(cache_path: Path) -> None:
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text("{rotto", encoding="utf-8")
    cache = GenresCache.load(cache_path)
    assert cache.warning is not None
    assert cache.get("a1", "musicbrainz") is None


def test_cache_versione_diversa_da_warning(cache_path: Path) -> None:
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(json.dumps({"version": 99, "artists": {}}), encoding="utf-8")
    cache = GenresCache.load(cache_path)
    assert cache.warning is not None


def test_cache_dirty_flag(cache_path: Path) -> None:
    cache = GenresCache(cache_path)
    assert not cache.dirty
    cache.put("a1", ["rock"], "musicbrainz")
    assert cache.dirty
    cache.save()
    assert not cache.dirty


# ---------------------------------------------------------------------------
# CachingGenreProvider
# ---------------------------------------------------------------------------


class CountingProvider:
    """Provider che conta le chiamate (per verificare che la cache eviti richieste inutili)."""

    def __init__(self, genres: dict[str, list[str]] | None = None) -> None:
        self._genres = genres or {}
        self.calls: list[str] = []

    def genres_for_artist(self, artist_id: str, artist_name: str) -> list[str]:
        self.calls.append(artist_id)
        return list(self._genres.get(artist_id, []))


def test_caching_provider_seconda_chiamata_dalla_cache(tmp_path: Path) -> None:
    inner = CountingProvider({"a1": ["pop"]})
    cache = GenresCache(tmp_path / "g.json")
    provider = CachingGenreProvider(inner, cache, "musicbrainz")

    first = provider.genres_for_artist("a1", "Art")
    second = provider.genres_for_artist("a1", "Art")
    assert first == second == ["pop"]
    assert inner.calls == ["a1"]  # solo una chiamata reale
    assert provider.hits == 1 and provider.misses == 1


def test_caching_provider_autosave_ogni_n_miss(tmp_path: Path) -> None:
    inner = CountingProvider({f"a{i}": [f"g{i}"] for i in range(10)})
    cache = GenresCache(tmp_path / "g.json")
    provider = CachingGenreProvider(inner, cache, "musicbrainz", autosave_every=3)

    for i in range(7):
        provider.genres_for_artist(f"a{i}", f"Art{i}")

    # Autosave dopo 3 e 6 miss: il file deve esistere con almeno 6 voci
    loaded = GenresCache.load(tmp_path / "g.json")
    assert len(loaded.entries) >= 6


def test_caching_provider_save_manuale(tmp_path: Path) -> None:
    inner = CountingProvider({"a1": ["rock"]})
    cache = GenresCache(tmp_path / "g.json")
    provider = CachingGenreProvider(inner, cache, "musicbrainz")
    provider.genres_for_artist("a1", "Art")
    assert not (tmp_path / "g.json").exists()  # nessun autosave ancora
    provider.save()
    assert (tmp_path / "g.json").exists()


def test_caching_provider_save_senza_dirty_non_scrive(tmp_path: Path) -> None:
    inner = CountingProvider()
    cache = GenresCache(tmp_path / "g.json")
    provider = CachingGenreProvider(inner, cache, "musicbrainz")
    provider.save()  # dirty=False: non deve creare il file
    assert not (tmp_path / "g.json").exists()


# ---------------------------------------------------------------------------
# build_provider
# ---------------------------------------------------------------------------


def test_build_provider_spotify(tmp_path: Path) -> None:
    settings = default_settings(data_dir=tmp_path)
    settings.config["genres"]["provider"] = "spotify"
    client = FakeSpotifyClient(artist_genres={"a1": ["pop"]})
    client._artist_names["a1"] = "Art"
    provider = build_provider(settings, client=client, rate_limit_seconds=0.0)
    assert isinstance(provider, CachingGenreProvider)
    assert provider.genres_for_artist("a1", "Art") == ["pop"]


def test_build_provider_musicbrainz(tmp_path: Path) -> None:
    settings = default_settings(data_dir=tmp_path)
    settings.config["genres"]["provider"] = "musicbrainz"
    http_get = http_get_returning(_MB_ARTIST_HIT)
    provider = build_provider(settings, http_get=http_get, rate_limit_seconds=0.0)
    assert isinstance(provider, CachingGenreProvider)
    result = provider.genres_for_artist("mb-1", "Test Artist")
    assert "indie rock" in result


def test_build_provider_lastfm(tmp_path: Path) -> None:
    settings = default_settings(data_dir=tmp_path)
    settings.config["genres"]["provider"] = "lastfm"
    settings.lastfm_api_key = "test-key"
    http_get = http_get_returning(_LFM_RESPONSE)
    provider = build_provider(settings, http_get=http_get, rate_limit_seconds=0.0)
    result = provider.genres_for_artist("x", "Art")
    assert "indie pop" in result


def test_build_provider_sconosciuto_solleva_errore(tmp_path: Path) -> None:
    settings = default_settings(data_dir=tmp_path)
    settings.config["genres"]["provider"] = "napster"
    with pytest.raises(ValueError, match="napster"):
        build_provider(settings)


def test_build_provider_spotify_senza_client_solleva_errore(tmp_path: Path) -> None:
    settings = default_settings(data_dir=tmp_path)
    settings.config["genres"]["provider"] = "spotify"
    with pytest.raises(ValueError, match="client"):
        build_provider(settings)


# ---------------------------------------------------------------------------
# C3: normalize_tag, macro_slug, GenreMap
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Hip-Hop", "hip hop"),
        ("R&B", "r&b"),
        ("  Indie  Pop  ", "indie pop"),
        ("Cantautorato", "cantautorato"),
        ("Café con leche", "cafe con leche"),  # accenti rimossi
        ("Genre_With_Underscore", "genre with underscore"),
    ],
)
def test_normalize_tag(raw: str, expected: str) -> None:
    assert normalize_tag(raw) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Rock", "rock"),
        ("Hip Hop", "hip-hop"),
        ("R&B", "r-and-b"),
        ("Electronic/Dance", "electronic-dance"),
        ("Indie+Pop", "indie-plus-pop"),
        ("  Jazz  ", "jazz"),
    ],
)
def test_macro_slug(name: str, expected: str) -> None:
    assert macro_slug(name) == expected


SIMPLE_MAP = {
    "Rock": ["rock", "classic rock", "hard rock"],
    "Indie": ["indie pop", "indie rock", "bedroom pop"],
    "Hip Hop": ["hip-hop", "rap", "trap"],
}


def test_genre_map_esatto() -> None:
    gmap = GenreMap(SIMPLE_MAP)
    assert gmap.resolve("rock") == "Rock"
    assert gmap.resolve("indie pop") == "Indie"
    assert gmap.resolve("hip-hop") == "Hip Hop"


def test_genre_map_match_per_parola_intera() -> None:
    gmap = GenreMap(SIMPLE_MAP)
    # "italian hip hop" contiene "hip-hop" normalizzato come "hip hop"
    assert gmap.resolve("italian hip hop") == "Hip Hop"


def test_genre_map_nome_del_macro_genere_risolve_in_se_stesso() -> None:
    gmap = GenreMap(SIMPLE_MAP)
    assert gmap.resolve("Rock") == "Rock"
    assert gmap.resolve("Indie") == "Indie"


def test_genre_map_tag_non_mappato() -> None:
    gmap = GenreMap(SIMPLE_MAP)
    assert gmap.resolve("ambient") is None
    assert gmap.resolve("progressive experimental post-metal") is None


def test_genre_map_case_insensitive_e_accenti() -> None:
    gmap = GenreMap(SIMPLE_MAP)
    assert gmap.resolve("ROCK") == "Rock"
    assert gmap.resolve("Indie Pop") == "Indie"


def test_genre_map_slug() -> None:
    gmap = GenreMap(SIMPLE_MAP)
    assert gmap.slug("Hip Hop") == "hip-hop"
    assert gmap.slug("R&B") == "r-and-b"


def test_genre_map_slug_duplicato_solleva_errore() -> None:
    with pytest.raises(ValueError, match="slug"):
        GenreMap({"Hip Hop": ["rap"], "Hip-Hop": ["trap"]})


def test_genre_map_tag_in_due_macro_generi_solleva_errore() -> None:
    with pytest.raises(ValueError, match="due macro-generi"):
        GenreMap({"Rock": ["indie rock"], "Indie": ["indie rock", "indie pop"]})


def test_genre_map_macro_genere_vuoto_solleva_errore() -> None:
    with pytest.raises(ValueError):
        GenreMap({"": ["pop"]})


def test_genre_map_tag_non_stringhe_sollevano_errore() -> None:
    with pytest.raises(ValueError):
        GenreMap({"Rock": [1, 2, 3]})  # type: ignore[dict-item]


def test_genre_map_parola_piu_lunga_vince() -> None:
    """Se "hip hop" e "hip" sono entrambi sottogeneri di macro diversi,
    "hip hop" (più lungo) vince su "hip" quando risolviamo "hip hop"."""
    gmap = GenreMap({"Hip Hop": ["hip hop"], "Varie": ["hip"]})
    assert gmap.resolve("hip hop") == "Hip Hop"


def test_resolve_tags_separa_mappati_e_non() -> None:
    gmap = GenreMap(SIMPLE_MAP)
    macros, unmapped = gmap.resolve_tags(["rock", "ambient", "indie pop", "jazz"])
    assert macros == ["Rock", "Indie"]
    assert sorted(unmapped) == ["ambient", "jazz"]


def test_resolve_tags_senza_duplicati() -> None:
    gmap = GenreMap(SIMPLE_MAP)
    macros, unmapped = gmap.resolve_tags(["rock", "classic rock", "hard rock"])
    assert macros == ["Rock"]
    assert unmapped == []


def test_genre_map_load_file_non_trovato(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non trovata"):
        GenreMap.load(tmp_path / "non_esiste.toml")


def test_genre_map_load_da_file_toml(tmp_path: Path) -> None:
    toml = tmp_path / "gmap.toml"
    toml.write_text('[Rock]\ntags = ["rock", "hard rock"]\n', encoding="utf-8")
    # Il TOML usa {"Rock": {"tags": [...]}} non {"Rock": [...]}: il caricamento deve adattarsi
    # oppure il file ha il formato corretto. Verifichiamo il formato effettivo:
    # GenreMap.load aspetta {"MacroGenere": ["tag1", "tag2"]}, quindi il file TOML
    # deve usare "MacroGenere" = ["tag1", "tag2"] (array TOML a livello radice).
    toml2 = tmp_path / "gmap2.toml"
    toml2.write_text('"Rock" = ["rock", "hard rock"]\n"Pop" = ["pop", "dance pop"]\n', encoding="utf-8")
    gmap = GenreMap.load(toml2)
    assert gmap.resolve("rock") == "Rock"
    assert gmap.resolve("dance pop") == "Pop"
