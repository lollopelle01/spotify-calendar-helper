"""Test end-to-end (task I-2): integrazione completa con FakeSpotifyClient.

Calendario sintetico di 36 mesi (2023-01 → 2025-12):
  - 12 artisti, di cui 4 esclusivamente di un anno, 1 presente in tutti e tre gli anni
  - 2 artisti con lo stesso nome (omonimi)
  - generi assegnati via FakeGenreProvider (copre i principali macro-generi di genre_map.toml)
  - brani con più artisti, brani ripetuti in mesi diversi

Scenari coperti:
  E2E-1  Primo sync artists: tutte le playlist attese vengono create, nessun duplicato.
  E2E-2  Idempotenza: un secondo sync immediato non produce nessuna modifica.
  E2E-3  Aggiunta di un mese: solo i brani nuovi vengono aggiunti alle playlist interessate.
  E2E-4  Idempotenza dopo l'aggiunta.
  E2E-5  State.json perso: il sync riconosce le playlist esistenti dal marker, senza duplicati.
  E2E-6  Playlist rinominata a mano: viene comunque aggiornata (il marker la identifica).
  E2E-7  Primo sync genres via MusicBrainz.
  E2E-8  Idempotenza genres.
  E2E-9  dry-run artists: nessuna scrittura su Spotify, nessun salvataggio dello stato.
  E2E-10 --prune ignorato in modalità additive.
  E2E-11 Playlist del calendario non tua: segnalata, non letta, non blocca il resto.
  E2E-12 Filtro --year: vengono processati solo i mesi dell'anno richiesto.
  E2E-13 sync artisti su anni separati.
  E2E-14 ScanResult.cache_hits: al secondo scan lo snapshot_id invariato usa la cache.
  E2E-15 sch calendar summary: mesi mancanti rilevati correttamente.
  E2E-16 Scenario composito: tutto insieme (senza genres: API Spotify non restituisce genres).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spotify_calendar_helper.cache import CalendarCache
from spotify_calendar_helper.calendar_scan import ScanFilters, scan, summarize
from spotify_calendar_helper.config import Settings, default_settings
from spotify_calendar_helper.genres import GenreMap
from spotify_calendar_helper.grouping import group_by_artist, group_by_genre
from spotify_calendar_helper.models import ArtistRef, Track, make_marker, parse_marker
from spotify_calendar_helper.state import State
from spotify_calendar_helper.sync import SyncOptions, sync_specs
from tests.fakes import FakeGenreProvider, FakeSpotifyClient, make_artist, make_track


# ---------------------------------------------------------------------------
# Costruzione del calendario sintetico
# ---------------------------------------------------------------------------

ARTISTS: dict[str, ArtistRef] = {
    "a_rock":   make_artist("The Rock Band",  "artist_rock"),
    "a_indie":  make_artist("Indie Artist",   "artist_indie"),
    "a_pop":    make_artist("Pop Star",       "artist_pop"),
    "a_jazz":   make_artist("Jazz Trio",      "artist_jazz"),
    "a_rnb":    make_artist("Soul Singer",    "artist_rnb"),
    "a_hiphop": make_artist("Rapper One",     "artist_hiphop"),
    "a_elec":   make_artist("DJ Electronic",  "artist_elec"),
    "a_folk":   make_artist("Folk Teller",    "artist_folk"),
    # Due artisti con lo stesso nome (omonimi)
    "a_homo1":  make_artist("Omonimo",        "artist_homo1"),
    "a_homo2":  make_artist("Omonimo",        "artist_homo2"),
    # Un artista presente in tutti e tre gli anni
    "a_all":    make_artist("Always Present", "artist_all"),
    # Un artista con meno brani della soglia (non deve generare playlist)
    "a_rare":   make_artist("Raro Artist",    "artist_rare"),
}

GENRES: dict[str, list[str]] = {
    "artist_rock":   ["rock", "hard rock"],
    "artist_indie":  ["indie rock", "dream pop"],
    "artist_pop":    ["pop", "dance pop"],
    "artist_jazz":   ["jazz", "bossa nova"],
    "artist_rnb":    ["soul", "r&b"],
    "artist_hiphop": ["hip hop", "rap"],
    "artist_elec":   ["electronic", "house"],
    "artist_folk":   ["folk", "folk rock"],
    "artist_homo1":  ["rock"],
    "artist_homo2":  ["indie"],
    "artist_all":    ["pop", "rock"],
    "artist_rare":   ["jazz"],
}

# Soglie di default: min_tracks = 4 (artisti), 8 (generi)
# a_rare: 2 brani totali → non qualifica per artisti né per generi


def _t(
    tid: str,
    artist_key: str,
    name: str | None = None,
    added_at: str | None = None,
    extra_artists: list[str] | None = None,
) -> Track:
    primary = ARTISTS[artist_key]
    extras = [ARTISTS[k] for k in (extra_artists or [])]
    return make_track(
        tid,
        name=name or f"Brano {tid}",
        artists=[primary, *extras],
        added_at=added_at or "2023-01-01T10:00:00Z",
    )


def _build_calendar() -> list[tuple[str, list[Track]]]:
    """Restituisce una lista di (nome_playlist, brani) per 36 mesi."""
    calendar: list[tuple[str, list[Track]]] = []

    # --- 2023: rock, indie, omonimi, artista-always-present ---
    months_2023 = {
        1:  [_t("r01", "a_rock"),  _t("r02", "a_rock"), _t("r03", "a_rock"), _t("r04", "a_rock"),
             _t("al01", "a_all"),  _t("al02", "a_all")],
        2:  [_t("r05", "a_rock"),  _t("i01", "a_indie"), _t("i02", "a_indie"),
             _t("al03", "a_all")],
        3:  [_t("r06", "a_rock"),  _t("i03", "a_indie"), _t("i04", "a_indie"),
             _t("al04", "a_all"),  _t("al05", "a_all")],
        4:  [_t("r07", "a_rock"),  _t("i05", "a_indie"), _t("i06", "a_indie"),
             _t("h1a", "a_homo1"), _t("h1b", "a_homo1"), _t("h1c", "a_homo1"),
             _t("h1d", "a_homo1"), _t("h2a", "a_homo2"), _t("h2b", "a_homo2"),
             _t("h2c", "a_homo2"), _t("h2d", "a_homo2"),
             _t("al06", "a_all")],
        5:  [_t("r08", "a_rock"),  _t("al07", "a_all"),
             _t("rare1", "a_rare"), _t("rare2", "a_rare")],   # rare sotto soglia
        6:  [_t("r09", "a_rock"),  _t("i07", "a_indie"), _t("al08", "a_all")],
        7:  [_t("r10", "a_rock"),  _t("i08", "a_indie"), _t("al09", "a_all")],
        8:  [_t("r11", "a_rock"),  _t("al10", "a_all")],
        9:  [_t("r12", "a_rock"),  _t("al11", "a_all")],
        10: [_t("r13", "a_rock"),  _t("i09", "a_indie"), _t("al12", "a_all")],
        11: [_t("r14", "a_rock"),  _t("i10", "a_indie"), _t("al13", "a_all")],
        12: [_t("r15", "a_rock"),  _t("al14", "a_all")],
    }
    for month, tracks in sorted(months_2023.items()):
        calendar.append((f"2023-{month:02d}", tracks))

    # --- 2024: pop, jazz, rnb, hiphop (4 artisti esclusivamente di quest'anno) ---
    months_2024 = {
        1:  [_t("p01", "a_pop"),   _t("p02", "a_pop"),   _t("p03", "a_pop"),   _t("p04", "a_pop"),
             _t("al15", "a_all")],
        2:  [_t("p05", "a_pop"),   _t("j01", "a_jazz"),  _t("j02", "a_jazz"),
             _t("al16", "a_all")],
        3:  [_t("p06", "a_pop"),   _t("j03", "a_jazz"),  _t("j04", "a_jazz"),
             _t("al17", "a_all")],
        4:  [_t("p07", "a_pop"),   _t("j05", "a_jazz"),  _t("al18", "a_all")],
        5:  [_t("p08", "a_pop"),   _t("j06", "a_jazz"),  _t("s01", "a_rnb"),  _t("s02", "a_rnb"),
             _t("al19", "a_all")],
        6:  [_t("p09", "a_pop"),   _t("s03", "a_rnb"),   _t("s04", "a_rnb"),
             _t("al20", "a_all")],
        7:  [_t("p10", "a_pop"),   _t("s05", "a_rnb"),   _t("hh1", "a_hiphop"),
             _t("hh2", "a_hiphop"), _t("al21", "a_all")],
        8:  [_t("p11", "a_pop"),   _t("s06", "a_rnb"),   _t("hh3", "a_hiphop"),
             _t("hh4", "a_hiphop"), _t("al22", "a_all")],
        9:  [_t("p12", "a_pop"),   _t("s07", "a_rnb"),   _t("hh5", "a_hiphop"),
             _t("al23", "a_all")],
        10: [_t("p13", "a_pop"),   _t("s08", "a_rnb"),   _t("hh6", "a_hiphop"),
             _t("al24", "a_all")],
        11: [_t("p14", "a_pop"),   _t("s09", "a_rnb"),   _t("hh7", "a_hiphop"),
             _t("al25", "a_all")],
        12: [_t("p15", "a_pop"),   _t("s10", "a_rnb"),   _t("hh8", "a_hiphop"),
             _t("al26", "a_all")],
    }
    for month, tracks in sorted(months_2024.items()):
        calendar.append((f"2024-{month:02d}", tracks))

    # --- 2025: electronic, folk; brano con due artisti; brano già visto in 2023 (r01) ---
    months_2025 = {
        1:  [_t("e01", "a_elec"),  _t("e02", "a_elec"),  _t("f01", "a_folk"),
             _t("al27", "a_all")],
        2:  [_t("e03", "a_elec"),  _t("e04", "a_elec"),  _t("f02", "a_folk"),
             _t("al28", "a_all"),
             # brano con due artisti (rock + indie): primary_artist_only=True → solo rock
             _t("multi1", "a_rock", extra_artists=["a_indie"])],
        3:  [_t("e05", "a_elec"),  _t("f03", "a_folk"),  _t("al29", "a_all"),
             # r01 già visto in 2023-01 → non deve essere duplicato
             _t("r01", "a_rock")],
        4:  [_t("e06", "a_elec"),  _t("f04", "a_folk"),  _t("al30", "a_all")],
        5:  [_t("e07", "a_elec"),  _t("f05", "a_folk"),  _t("al31", "a_all")],
        6:  [_t("e08", "a_elec"),  _t("f06", "a_folk"),  _t("al32", "a_all")],
        7:  [_t("e09", "a_elec"),  _t("f07", "a_folk"),  _t("al33", "a_all")],
        8:  [_t("e10", "a_elec"),  _t("f08", "a_folk"),  _t("al34", "a_all")],
        9:  [_t("e11", "a_elec"),  _t("f09", "a_folk"),  _t("al35", "a_all")],
        10: [_t("e12", "a_elec"),  _t("f10", "a_folk"),  _t("al36", "a_all")],
        11: [_t("e13", "a_elec"),  _t("f11", "a_folk")],
        12: [_t("e14", "a_elec"),  _t("f12", "a_folk")],
    }
    for month, tracks in sorted(months_2025.items()):
        calendar.append((f"2025-{month:02d}", tracks))

    return calendar


# ---------------------------------------------------------------------------
# Fixture locali (nomi diversi da conftest per evitare conflitti)
# ---------------------------------------------------------------------------


@pytest.fixture()
def e2e_settings(tmp_path: Path) -> Settings:
    """Settings con i default e i dati in una cartella temporanea."""
    return default_settings(data_dir=tmp_path / "data")


@pytest.fixture()
def e2e_genre_map() -> GenreMap:
    repo_root = Path(__file__).resolve().parents[1]
    map_path = repo_root / "genre_map.toml"
    return GenreMap.load(map_path)


@pytest.fixture()
def e2e_client() -> FakeSpotifyClient:
    fc = FakeSpotifyClient(user_id="user_test", api_max_batch=10, add_batch_size=10)
    for name, tracks in _build_calendar():
        fc.seed_playlist(name, tracks)
    return fc


@pytest.fixture()
def e2e_provider() -> FakeGenreProvider:
    return FakeGenreProvider(genres=GENRES)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _do_sync_artists(
    client: FakeSpotifyClient,
    settings: Settings,
    state: State,
    *,
    year: int | None = None,
    dry_run: bool = False,
    prune: bool = False,
):
    all_ = year is None
    filters = ScanFilters(year=year, all=all_)
    cache = CalendarCache.load(settings.cache_path)
    result = scan(client, settings, filters, cache)
    specs = group_by_artist(result.playlists, settings)
    return sync_specs(client, state, specs, settings, SyncOptions(dry_run=dry_run, prune=prune))


def _do_sync_genres(
    client: FakeSpotifyClient,
    settings: Settings,
    state: State,
    provider: FakeGenreProvider,
    genre_map: GenreMap,
    *,
    year: int | None = None,
    dry_run: bool = False,
):
    all_ = year is None
    filters = ScanFilters(year=year, all=all_)
    cache = CalendarCache.load(settings.cache_path)
    result = scan(client, settings, filters, cache)
    specs, _ = group_by_genre(result.playlists, provider, genre_map, settings)
    return sync_specs(client, state, specs, settings, SyncOptions(dry_run=dry_run))


def _artist_pids(client: FakeSpotifyClient) -> dict[str, str]:
    """Restituisce {artist_id: playlist_id} per le playlist derivate di tipo artist."""
    result = {}
    for pid, info in client._playlists.items():
        parsed = parse_marker(info.description)
        if parsed and parsed[0] == "artist":
            result[parsed[1]] = pid
    return result


def _genre_pids(client: FakeSpotifyClient) -> dict[str, str]:
    """Restituisce {genre_slug: playlist_id} per le playlist derivate di tipo genre."""
    result = {}
    for pid, info in client._playlists.items():
        parsed = parse_marker(info.description)
        if parsed and parsed[0] == "genre":
            result[parsed[1]] = pid
    return result


# ---------------------------------------------------------------------------
# E2E-1: primo sync artists
# ---------------------------------------------------------------------------


def test_e2e1_primo_sync_artists(e2e_client, e2e_settings):
    state = State.load(e2e_settings.state_path)
    report = _do_sync_artists(e2e_client, e2e_settings, state)

    expected_ids = {
        "artist_rock", "artist_indie", "artist_pop", "artist_jazz",
        "artist_rnb", "artist_hiphop", "artist_elec", "artist_folk",
        "artist_homo1", "artist_homo2", "artist_all",
    }
    assert "artist_rare" not in {c.key for c in report.created}, "a_rare non dovrebbe qualificare"

    created_keys = {c.key for c in report.created}
    assert created_keys == expected_ids, (
        f"Artisti creati: {created_keys}\nAttesi: {expected_ids}"
    )
    assert not report.updated
    assert not report.unchanged

    # Nessun duplicato: ogni artista ha esattamente una playlist
    ap = _artist_pids(e2e_client)
    assert set(ap) == expected_ids

    # Brano duplicato (r01 in 2023-01 e 2025-03): deve essere presente una sola volta
    rock_pid = ap["artist_rock"]
    uris = e2e_client.uris_of(rock_pid)
    assert uris.count("spotify:track:r01") == 1, "r01 duplicato nella playlist rock"

    # Brano con due artisti (multi1): primary_artist_only=True → solo rock, non indie
    assert "spotify:track:multi1" in e2e_client.uris_of(ap["artist_rock"])
    assert "spotify:track:multi1" not in e2e_client.uris_of(ap["artist_indie"])

    # Omonimi: le due playlist hanno nomi diversi
    name1 = e2e_client._playlists[ap["artist_homo1"]].name
    name2 = e2e_client._playlists[ap["artist_homo2"]].name
    assert name1.casefold() != name2.casefold(), "Le playlist degli omonimi hanno lo stesso nome"

    # Lo stato è stato salvato
    state2 = State.load(e2e_settings.state_path)
    assert set(state2.playlists) >= {f"artist:{a}" for a in expected_ids}


# ---------------------------------------------------------------------------
# E2E-2: idempotenza dopo il primo sync
# ---------------------------------------------------------------------------


def test_e2e2_idempotenza_artists(e2e_client, e2e_settings):
    state = State.load(e2e_settings.state_path)
    _do_sync_artists(e2e_client, e2e_settings, state)
    e2e_client.reset_counters()

    state2 = State.load(e2e_settings.state_path)
    report2 = _do_sync_artists(e2e_client, e2e_settings, state2)

    assert report2.is_noop, f"Il secondo sync ha prodotto modifiche: {report2}"
    assert e2e_client.write_count == 0, f"Scritture nel secondo sync: {e2e_client.write_count}"


# ---------------------------------------------------------------------------
# E2E-3: aggiunta di un mese e nuovo sync
# ---------------------------------------------------------------------------


def test_e2e3_aggiunta_mese(e2e_client, e2e_settings):
    state = State.load(e2e_settings.state_path)
    _do_sync_artists(e2e_client, e2e_settings, state)
    ap_after_first = _artist_pids(e2e_client)

    # Aggiunge gennaio 2026 con brani di rock, pop e un artista nuovo
    new_artist = make_artist("New Artist 2026", "artist_new2026")
    new_tracks = [
        make_track("new01", artists=[ARTISTS["a_rock"]], added_at="2026-01-05T10:00:00Z"),
        make_track("new02", artists=[ARTISTS["a_pop"]],  added_at="2026-01-10T10:00:00Z"),
        make_track("new03", artists=[new_artist],         added_at="2026-01-15T10:00:00Z"),
        make_track("new04", artists=[new_artist],         added_at="2026-01-20T10:00:00Z"),
        make_track("new05", artists=[new_artist],         added_at="2026-01-25T10:00:00Z"),
        make_track("new06", artists=[new_artist],         added_at="2026-01-28T10:00:00Z"),
    ]
    e2e_client.seed_playlist("2026-01", new_tracks)

    state2 = State.load(e2e_settings.state_path)
    report3 = _do_sync_artists(e2e_client, e2e_settings, state2)

    updated_keys = {u.key for u in report3.updated}
    assert "artist_rock" in updated_keys, "rock non aggiornato"
    assert "artist_pop"  in updated_keys, "pop non aggiornato"
    assert "artist_new2026" in {c.key for c in report3.created}, "nuovo artista non creato"

    # Le playlist già esistenti non vengono ricreate
    ap_after = _artist_pids(e2e_client)
    for aid, pid in ap_after_first.items():
        assert ap_after.get(aid) == pid, f"Playlist di {aid} ha cambiato ID"

    # r01 ancora una sola volta in rock
    assert e2e_client.uris_of(ap_after["artist_rock"]).count("spotify:track:r01") == 1


# ---------------------------------------------------------------------------
# E2E-4: idempotenza dopo l'aggiunta
# ---------------------------------------------------------------------------


def test_e2e4_idempotenza_dopo_aggiunta(e2e_client, e2e_settings):
    state = State.load(e2e_settings.state_path)
    _do_sync_artists(e2e_client, e2e_settings, state)
    e2e_client.seed_playlist("2026-01", [
        make_track("new01", artists=[ARTISTS["a_rock"]], added_at="2026-01-05T10:00:00Z"),
        make_track("new02", artists=[ARTISTS["a_pop"]],  added_at="2026-01-10T10:00:00Z"),
    ])
    state2 = State.load(e2e_settings.state_path)
    _do_sync_artists(e2e_client, e2e_settings, state2)
    e2e_client.reset_counters()

    state3 = State.load(e2e_settings.state_path)
    report = _do_sync_artists(e2e_client, e2e_settings, state3)

    assert report.is_noop
    assert e2e_client.write_count == 0


# ---------------------------------------------------------------------------
# E2E-5: state.json perso → ricostruzione dal marker
# ---------------------------------------------------------------------------


def test_e2e5_state_perso(e2e_client, e2e_settings):
    state = State.load(e2e_settings.state_path)
    _do_sync_artists(e2e_client, e2e_settings, state)
    ap_before = _artist_pids(e2e_client)
    n_playlists_before = len(e2e_client._playlists)

    # Cancella lo state.json
    e2e_settings.state_path.unlink(missing_ok=True)

    state2 = State.load(e2e_settings.state_path)
    # Il file mancante non è un warning (State.load con file assente restituisce stato vuoto,
    # senza warning, che è il comportamento atteso: non era corrotto, semplicemente assente)

    report = _do_sync_artists(e2e_client, e2e_settings, state2)

    # Nessuna nuova playlist: tutte trovate dal marker
    assert not report.created, f"Create nuove playlist invece di riconoscerle: {report.created}"
    assert len(e2e_client._playlists) == n_playlists_before

    # Gli ID delle playlist derivate non sono cambiati
    ap_after = _artist_pids(e2e_client)
    for aid, pid in ap_before.items():
        assert ap_after.get(aid) == pid, f"Playlist di {aid} ha cambiato ID dopo ricostruzione"


# ---------------------------------------------------------------------------
# E2E-6: playlist rinominata a mano
# ---------------------------------------------------------------------------


def test_e2e6_rinomina_a_mano(e2e_client, e2e_settings):
    state = State.load(e2e_settings.state_path)
    _do_sync_artists(e2e_client, e2e_settings, state)
    ap = _artist_pids(e2e_client)
    rock_pid = ap["artist_rock"]

    # L'utente rinomina la playlist a mano
    e2e_client.rename_for_test(rock_pid, "I miei preferiti Rock")

    # Aggiunge un brano nuovo nella playlist 2023-01 per generare un "update"
    new_track = make_track("new_r99", artists=[ARTISTS["a_rock"]], added_at="2023-01-31T10:00:00Z")
    cal_pl = next(
        info for info in e2e_client._playlists.values()
        if info.name == "2023-01"
    )
    e2e_client._items[cal_pl.id].append(new_track)
    e2e_client._touch(cal_pl.id)

    state2 = State.load(e2e_settings.state_path)
    report = _do_sync_artists(e2e_client, e2e_settings, state2)

    # La playlist deve essere stata aggiornata, non ricreata
    assert not any(c.key == "artist_rock" for c in report.created), "rock è stata ricreata"
    all_keys = {u.key for u in report.updated} | set(report.unchanged)
    assert "artist_rock" in all_keys

    # L'ID non deve essere cambiato
    ap2 = _artist_pids(e2e_client)
    assert ap2["artist_rock"] == rock_pid


# ---------------------------------------------------------------------------
# E2E-7: primo sync genres [DISABILITATO]
# ---------------------------------------------------------------------------


def test_e2e7_primo_sync_genres(e2e_client, e2e_settings, e2e_provider, e2e_genre_map):
    state = State.load(e2e_settings.state_path)
    report = _do_sync_genres(e2e_client, e2e_settings, state, e2e_provider, e2e_genre_map)

    # Con min_tracks=8, almeno alcuni generi devono qualificare
    assert report.created, "Nessuna playlist di genere creata"

    # Tutte le playlist create hanno il marker corretto
    gp = _genre_pids(e2e_client)
    for c in report.created:
        assert c.key in gp, f"Slug {c.key} non trovato tra le playlist"
        desc = e2e_client._playlists[gp[c.key]].description
        assert make_marker("genre", c.key) in desc


# ---------------------------------------------------------------------------
# E2E-8: idempotenza genres [DISABILITATO]
# ---------------------------------------------------------------------------


def test_e2e8_idempotenza_genres(e2e_client, e2e_settings, e2e_provider, e2e_genre_map):
    state = State.load(e2e_settings.state_path)
    _do_sync_genres(e2e_client, e2e_settings, state, e2e_provider, e2e_genre_map)
    e2e_client.reset_counters()

    state2 = State.load(e2e_settings.state_path)
    report2 = _do_sync_genres(e2e_client, e2e_settings, state2, e2e_provider, e2e_genre_map)

    assert report2.is_noop, f"Secondo sync genres ha prodotto modifiche: {report2}"
    assert e2e_client.write_count == 0


# ---------------------------------------------------------------------------
# E2E-9: dry-run artists
# ---------------------------------------------------------------------------


def test_e2e9_dry_run_artists(e2e_client, e2e_settings):
    state = State.load(e2e_settings.state_path)
    n_playlists_before = len(e2e_client._playlists)
    e2e_client.reset_counters()

    report = _do_sync_artists(e2e_client, e2e_settings, state, dry_run=True)

    # Nessuna scrittura su Spotify
    assert e2e_client.write_count == 0, f"dry-run ha scritto {e2e_client.write_count} volte"
    assert len(e2e_client._playlists) == n_playlists_before

    # Il report segnala le playlist che verrebbero create
    assert report.created, "Il dry-run non ha riportato nessuna creazione"
    assert all(c.playlist_id is None for c in report.created), (
        "Le voci di dry-run non devono avere un playlist_id"
    )

    # Lo state.json non viene creato/aggiornato
    assert not e2e_settings.state_path.exists()


# ---------------------------------------------------------------------------
# E2E-10: --prune ignorato in modalità additive
# ---------------------------------------------------------------------------


def test_e2e10_prune_ignorato_in_additive(e2e_client, e2e_settings):
    state = State.load(e2e_settings.state_path)
    _do_sync_artists(e2e_client, e2e_settings, state)
    e2e_client.reset_counters()

    state2 = State.load(e2e_settings.state_path)
    report = _do_sync_artists(e2e_client, e2e_settings, state2, prune=True)

    # Deve esserci un avviso per --prune con mode=additive
    assert any("prune" in w.lower() or "mirror" in w.lower() for w in report.warnings), (
        "Nessun avviso per --prune in modalità additive"
    )
    # Nessun brano rimosso
    assert not any(u.removed > 0 for u in report.updated), "Brani rimossi con mode=additive"


# ---------------------------------------------------------------------------
# E2E-11: playlist del calendario non tua
# ---------------------------------------------------------------------------


def test_e2e11_playlist_non_tua(e2e_client, e2e_settings):
    # Aggiunge una playlist mensile di un altro utente
    e2e_client.seed_playlist(
        "2026-02",
        [make_track("other1", artists=[ARTISTS["a_rock"]], added_at="2026-02-01T10:00:00Z")],
        owner_id="altro_utente",
    )

    filters = ScanFilters(all=True)
    cache = CalendarCache.load(e2e_settings.cache_path)
    result = scan(e2e_client, e2e_settings, filters, cache)

    assert "2026-02" in result.not_owned
    assert any("2026-02" in w or "non" in w.lower() for w in result.warnings)
    # Non deve bloccare la scansione delle altre playlist
    assert result.playlists


# ---------------------------------------------------------------------------
# E2E-12: filtro --year
# ---------------------------------------------------------------------------


def test_e2e12_filtro_year(e2e_client, e2e_settings):
    state = State.load(e2e_settings.state_path)
    # Sync solo su 2024
    report = _do_sync_artists(e2e_client, e2e_settings, state, year=2024)

    created_keys = {c.key for c in report.created}

    # Gli artisti esclusivamente del 2024 devono qualificare
    assert "artist_pop"    in created_keys
    assert "artist_jazz"   in created_keys
    assert "artist_rnb"    in created_keys
    assert "artist_hiphop" in created_keys

    # Gli artisti esclusivamente del 2023 non devono essere creati
    assert "artist_rock"  not in created_keys, "Rock creato filtrando solo 2024"
    assert "artist_indie" not in created_keys, "Indie creato filtrando solo 2024"
    # Gli artisti esclusivamente del 2025 non devono essere creati
    assert "artist_elec"  not in created_keys, "Electronic creato filtrando solo 2024"
    assert "artist_folk"  not in created_keys, "Folk creato filtrando solo 2024"


# ---------------------------------------------------------------------------
# E2E-13: sync artisti e generi su anni separati
# ---------------------------------------------------------------------------


def test_e2e13_sync_anni_separati(e2e_client, e2e_settings):
    state = State.load(e2e_settings.state_path)

    # Primo sync artisti solo 2023
    _do_sync_artists(e2e_client, e2e_settings, state, year=2023)
    ap_2023 = dict(_artist_pids(e2e_client))

    # Secondo sync artisti solo 2024 (aggiunge artisti esclusivi del 2024)
    state2 = State.load(e2e_settings.state_path)
    _do_sync_artists(e2e_client, e2e_settings, state2, year=2024)
    ap_after_2024 = _artist_pids(e2e_client)

    # Le playlist del 2023 non devono avere cambiato ID
    for aid, pid in ap_2023.items():
        assert ap_after_2024.get(aid) == pid, f"Playlist di {aid} ha cambiato ID"

    # (sync genres su anni separati rimosso: API Spotify non restituisce più 'genres' da feb 2026)


# ---------------------------------------------------------------------------
# E2E-14: cache del calendario
# ---------------------------------------------------------------------------


def test_e2e14_cache_hits(e2e_client, e2e_settings):
    filters = ScanFilters(all=True)

    # Primo scan: tutti cache_misses
    cache1 = CalendarCache.load(e2e_settings.cache_path)
    result1 = scan(e2e_client, e2e_settings, filters, cache1)
    assert result1.cache_misses == len(result1.playlists)
    assert result1.cache_hits == 0

    # Secondo scan: snapshot_id invariato → tutti cache_hits
    cache2 = CalendarCache.load(e2e_settings.cache_path)
    result2 = scan(e2e_client, e2e_settings, filters, cache2)
    assert result2.cache_hits == len(result2.playlists)
    assert result2.cache_misses == 0

    # I brani sono gli stessi
    uris1 = {t.uri for pl in result1.playlists for t in pl.tracks}
    uris2 = {t.uri for pl in result2.playlists for t in pl.tracks}
    assert uris1 == uris2


# ---------------------------------------------------------------------------
# E2E-15: calendar summary — mesi mancanti
# ---------------------------------------------------------------------------


def test_e2e15_mesi_mancanti():
    """Calendario con un buco (manca marzo 2024): il summary lo segnala."""
    fc = FakeSpotifyClient(user_id="user_test")
    a = make_artist("A", "a1")
    fc.seed_playlist("2024-01", [make_track("t1", artists=[a])])
    fc.seed_playlist("2024-02", [make_track("t2", artists=[a])])
    fc.seed_playlist("2024-04", [make_track("t3", artists=[a])])

    settings = default_settings(data_dir=Path("/tmp/e2e_mesi_mancanti"))
    result = scan(fc, settings, ScanFilters(year=2024))
    summary = summarize(result)

    assert summary.total_playlists == 3
    assert (2024, 3) in summary.missing_months, "Marzo 2024 non segnalato come mancante"
    assert (2024, 1) not in summary.missing_months
    assert (2024, 2) not in summary.missing_months
    assert (2024, 4) not in summary.missing_months


# ---------------------------------------------------------------------------
# E2E-16: scenario composito (tutto insieme)
# ---------------------------------------------------------------------------


def test_e2e16_scenario_completo(e2e_client, e2e_settings):
    """Filo unico: primo sync → idempotenza → aggiunta mese → idempotenza.

    La parte genres è stata rimossa: l'API Spotify non restituisce più 'genres' (feb 2026).
    """

    # --- run 1: artists ---
    state = State.load(e2e_settings.state_path)
    r1 = _do_sync_artists(e2e_client, e2e_settings, state)
    assert r1.created, "Nessuna playlist creata al primo run"

    # --- run 2: idempotenza ---
    state = State.load(e2e_settings.state_path)
    r2 = _do_sync_artists(e2e_client, e2e_settings, state)
    assert r2.is_noop

    # --- run 3: aggiunta di 2026-01 ---
    extra = [
        make_track(f"x{i}", artists=[ARTISTS["a_rock"]], added_at=f"2026-01-{i+1:02d}T10:00:00Z")
        for i in range(3)
    ]
    e2e_client.seed_playlist("2026-01", extra)
    state = State.load(e2e_settings.state_path)
    r3 = _do_sync_artists(e2e_client, e2e_settings, state)
    assert any(u.key == "artist_rock" for u in r3.updated), "rock non aggiornato"
    assert not any(c.key == "artist_rock" for c in r3.created), "rock ricreato"

    # --- run 4: idempotenza dopo aggiunta ---
    state = State.load(e2e_settings.state_path)
    r4 = _do_sync_artists(e2e_client, e2e_settings, state)
    assert r4.is_noop

    # --- verifica finale: nessun duplicato artisti ---
    ap = _artist_pids(e2e_client)
    artist_keys_in_playlists = list(ap.keys())
    assert len(artist_keys_in_playlists) == len(set(artist_keys_in_playlists))
