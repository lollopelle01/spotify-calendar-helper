"""Test del riconoscimento dei nomi delle playlist mensili (task B1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from spotify_calendar_helper.cache import CalendarCache
from spotify_calendar_helper.calendar_scan import (
    ScanFilters,
    month_number,
    parse_playlist_name,
    parse_year_month,
    scan,
    summarize,
)
from spotify_calendar_helper.config import Settings, default_settings
from spotify_calendar_helper.models import PlaylistInfo, make_marker
from tests.fakes import FakeSpotifyClient, make_track


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("2025-03", (2025, 3)),
        ("2025-03 Preferiti", (2025, 3)),
        ("2025/03", (2025, 3)),
        ("2025.3", (2025, 3)),
        ("2025-3", (2025, 3)),
        ("2025-10", (2025, 10)),
        ("2025-12", (2025, 12)),
        ("03/2025", (2025, 3)),
        ("3/2025", (2025, 3)),
        ("03-2025", (2025, 3)),
        ("Marzo 2025", (2025, 3)),
        ("marzo 2025", (2025, 3)),
        ("MARZO 2025", (2025, 3)),
        ("Gennaio 2024", (2024, 1)),
        ("Febbraio 2024", (2024, 2)),
        ("Aprile 2023", (2023, 4)),
        ("Maggio 2023", (2023, 5)),
        ("Giugno 2022", (2022, 6)),
        ("Luglio 2022", (2022, 7)),
        ("Agosto 2021", (2021, 8)),
        ("Settembre 2021", (2021, 9)),
        ("Ottobre 2020", (2020, 10)),
        ("Novembre 2020", (2020, 11)),
        ("Dicembre 2019", (2019, 12)),
        ("Mar 2025", (2025, 3)),
        ("Mar. 2025", (2025, 3)),
        ("Sett 2025", (2025, 9)),
        ("Dic 2024", (2024, 12)),
        ("2025 Marzo", (2025, 3)),
        ("Marzo 2025 - preferiti", (2025, 3)),
        ("  Marzo 2025  ", (2025, 3)),
    ],
)
def test_nomi_riconosciuti(name: str, expected: tuple[int, int]) -> None:
    assert parse_playlist_name(name) == expected


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "Playlist 2025",
        "Roadtrip",
        "Marzo",
        "2025",
        "13/2025",
        "2025-13",
        "2025-00",
        "00/2025",
        "Estate 2025",
        "Preferiti Marzo 2025",  # la data deve stare all'inizio
        "2025-03-15",  # una data completa non è un mese
        "Marzo 2025 (vecchia)",  # copia/variante tra parentesi
        "Marzo 1850",
        "Marzolino 2025",
        "2025-031",
    ],
)
def test_nomi_non_riconosciuti(name: str) -> None:
    assert parse_playlist_name(name) is None


def test_regex_personalizzata_con_nome_del_mese() -> None:
    rx = r"^Cal (?P<month>\w+) (?P<year>\d{4})$"
    assert parse_playlist_name("Cal Marzo 2025", rx) == (2025, 3)


def test_regex_personalizzata_con_mese_numerico() -> None:
    rx = r"^Cal (?P<month>\w+) (?P<year>\d{4})$"
    assert parse_playlist_name("Cal 3 2025", rx) == (2025, 3)


def test_regex_personalizzata_ha_la_precedenza() -> None:
    rx = r"(?P<year>\d{4})_(?P<month>\d{2})"
    assert parse_playlist_name("mix_2024_07_estate", rx) == (2024, 7)


def test_se_la_regex_non_corrisponde_si_provano_i_formati_integrati() -> None:
    rx = r"^Cal (?P<month>\w+) (?P<year>\d{4})$"
    assert parse_playlist_name("Marzo 2025", rx) == (2025, 3)


def test_regex_personalizzata_con_mese_non_valido() -> None:
    rx = r"^Cal (?P<month>\w+) (?P<year>\d{4})$"
    assert parse_playlist_name("Cal Pippo 2025", rx) is None


def test_regex_non_valida_solleva_errore_chiaro() -> None:
    with pytest.raises(ValueError, match="name_regex non valida"):
        parse_playlist_name("Marzo 2025", "(")


def test_regex_senza_gruppi_nominati_solleva_errore() -> None:
    with pytest.raises(ValueError, match="year"):
        parse_playlist_name("Marzo 2025", r"^(\d{4})-(\d{2})$")


@pytest.mark.parametrize(
    ("text", "expected"),
    [("03", 3), ("3", 3), ("12", 12), ("Marzo", 3), ("mar.", 3), ("13", None), ("0", None), ("x", None)],
)
def test_month_number(text: str, expected: int | None) -> None:
    assert month_number(text) == expected


# ---------------------------------------------------------------------------
# B3: scan()
# ---------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return default_settings(data_dir=tmp_path)


@pytest.fixture
def client() -> FakeSpotifyClient:
    c = FakeSpotifyClient()
    # Volutamente in ordine sparso e mischiate a playlist che non sono del calendario.
    c.seed_playlist("Maggio 2025", [make_track("m5")], playlist_id="p_2025_05")
    c.seed_playlist("Preferiti", [make_track("x")], playlist_id="p_pref")
    c.seed_playlist("2024-12", [make_track("d4")], playlist_id="p_2024_12")
    c.seed_playlist("Marzo 2025", [make_track("m3a"), make_track("m3b")], playlist_id="p_2025_03")
    c.seed_playlist("Roadtrip", [], playlist_id="p_road")
    return c


def test_scan_trova_e_ordina_per_anno_e_mese(client: FakeSpotifyClient, settings: Settings) -> None:
    result = scan(client, settings)
    assert [(p.year, p.month) for p in result.playlists] == [(2024, 12), (2025, 3), (2025, 5)]
    assert [p.id for p in result.playlists] == ["p_2024_12", "p_2025_03", "p_2025_05"]
    assert sorted(result.unrecognized) == ["Preferiti", "Roadtrip"]


def test_scan_legge_i_brani(client: FakeSpotifyClient, settings: Settings) -> None:
    marzo = next(p for p in scan(client, settings).playlists if p.month == 3)
    assert [t.id for t in marzo.tracks] == ["m3a", "m3b"]
    assert marzo.snapshot_id is not None


def test_scan_su_account_vuoto(settings: Settings) -> None:
    result = scan(FakeSpotifyClient(), settings)
    assert result.playlists == [] and result.warnings == []


def test_ignore_playlist_ids(client: FakeSpotifyClient, settings: Settings) -> None:
    settings.config["calendar"]["ignore_playlist_ids"] = ["p_2025_03", "p_pref"]
    result = scan(client, settings)
    assert [p.id for p in result.playlists] == ["p_2024_12", "p_2025_05"]
    # "Preferiti" non era comunque del calendario: ignorate sono solo quelle riconosciute.
    assert result.ignored_ids == ["p_2025_03"]


def test_playlist_derivate_non_sono_calendario(client: FakeSpotifyClient, settings: Settings) -> None:
    descr = f"Brani di X. {make_marker('artist', 'x1')}"
    client.seed_playlist("Marzo 2025 - copia", [], description=descr, playlist_id="p_derivata")
    ids = [p.id for p in scan(client, settings).playlists]
    assert "p_derivata" not in ids


def test_playlist_non_tua_segnalata_e_non_letta(client: FakeSpotifyClient, settings: Settings) -> None:
    client.seed_playlist("Giugno 2025", [make_track("z")], owner_id="altro", playlist_id="p_altrui")
    client.reset_counters()
    result = scan(client, settings)
    assert result.not_owned == ["Giugno 2025"]
    assert any("non è tua" in w for w in result.warnings)
    assert ("get_playlist_items", ("p_altrui",)) not in client.calls
    assert "p_altrui" not in [p.id for p in result.playlists]


def test_brani_saltati_contati(settings: Settings) -> None:
    c = FakeSpotifyClient()
    c.seed_playlist("Marzo 2025", [make_track("a")], skipped=2)
    c.seed_playlist("Aprile 2025", [make_track("b")], skipped=3)
    result = scan(c, settings)
    assert [p.skipped_items for p in result.playlists] == [2, 3]
    assert result.skipped_items == 5


def test_due_playlist_per_lo_stesso_mese_avvisa(settings: Settings) -> None:
    c = FakeSpotifyClient()
    c.seed_playlist("Marzo 2025", [make_track("a")])
    c.seed_playlist("2025-03 extra", [make_track("b")])
    result = scan(c, settings)
    assert len(result.playlists) == 2
    assert any("2025-03" in w for w in result.warnings)


def test_regex_personalizzata_nella_scansione(settings: Settings) -> None:
    settings.config["calendar"]["name_regex"] = r"^Cal (?P<month>\w+) (?P<year>\d{4})$"
    c = FakeSpotifyClient()
    c.seed_playlist("Cal Marzo 2025", [make_track("a")])
    assert [(p.year, p.month) for p in scan(c, settings).playlists] == [(2025, 3)]


def test_regex_non_valida_fallisce_prima_di_chiamare_spotify(settings: Settings) -> None:
    settings.config["calendar"]["name_regex"] = "("
    c = FakeSpotifyClient()
    with pytest.raises(ValueError, match="name_regex non valida"):
        scan(c, settings)
    assert c.calls == []


# --- filtri ---------------------------------------------------------------


def months(client: FakeSpotifyClient, settings: Settings, filters: ScanFilters) -> list[tuple[int, int]]:
    return [(p.year, p.month) for p in scan(client, settings, filters).playlists]


def test_filtro_per_anno(client: FakeSpotifyClient, settings: Settings) -> None:
    assert months(client, settings, ScanFilters(year=2025)) == [(2025, 3), (2025, 5)]


def test_filtro_intervallo_inclusivo(client: FakeSpotifyClient, settings: Settings) -> None:
    f = ScanFilters(from_ym=(2025, 3), to_ym=(2025, 5))
    assert months(client, settings, f) == [(2025, 3), (2025, 5)]


def test_filtro_solo_from_e_solo_to(client: FakeSpotifyClient, settings: Settings) -> None:
    assert months(client, settings, ScanFilters(from_ym=(2025, 4))) == [(2025, 5)]
    assert months(client, settings, ScanFilters(to_ym=(2025, 3))) == [(2024, 12), (2025, 3)]


def test_filtro_all_prende_tutto(client: FakeSpotifyClient, settings: Settings) -> None:
    assert len(months(client, settings, ScanFilters(all=True))) == 3


def test_filtro_non_legge_le_playlist_escluse(client: FakeSpotifyClient, settings: Settings) -> None:
    client.reset_counters()
    scan(client, settings, ScanFilters(year=2024))
    reads = [c for c in client.calls if c[0] == "get_playlist_items"]
    assert reads == [("get_playlist_items", ("p_2024_12",))]


def test_filtri_incompatibili() -> None:
    with pytest.raises(ValueError):
        ScanFilters(year=2025, from_ym=(2025, 1))
    with pytest.raises(ValueError):
        ScanFilters(all=True, year=2025)
    with pytest.raises(ValueError):
        ScanFilters(all=True, to_ym=(2025, 1))


@pytest.mark.parametrize(
    ("text", "expected"), [("2024-01", (2024, 1)), ("2025-12", (2025, 12)), (" 2025-06 ", (2025, 6))]
)
def test_parse_year_month(text: str, expected: tuple[int, int]) -> None:
    assert parse_year_month(text) == expected


@pytest.mark.parametrize("text", ["2024", "2024-13", "2024-00", "24-01", "01-2024", "", "marzo"])
def test_parse_year_month_non_valido(text: str) -> None:
    with pytest.raises(ValueError, match="AAAA-MM"):
        parse_year_month(text)


# --- cache ----------------------------------------------------------------


def reads(client: FakeSpotifyClient) -> list[str]:
    return [args[0] for name, args in client.calls if name == "get_playlist_items"]


def test_cache_primo_giro_miss_secondo_giro_hit(client: FakeSpotifyClient, settings: Settings) -> None:
    cache = CalendarCache.load(settings.cache_path)
    first = scan(client, settings, cache=cache)
    assert (first.cache_hits, first.cache_misses) == (0, 3)
    assert settings.cache_path.exists()

    client.reset_counters()
    second = scan(client, settings, cache=CalendarCache.load(settings.cache_path))
    assert (second.cache_hits, second.cache_misses) == (3, 0)
    assert reads(client) == []
    assert [p.tracks for p in second.playlists] == [p.tracks for p in first.playlists]


def test_cache_rilegge_solo_la_playlist_cambiata(client: FakeSpotifyClient, settings: Settings) -> None:
    scan(client, settings, cache=CalendarCache.load(settings.cache_path))
    client.add_items("p_2025_03", ["spotify:track:nuovo"])
    client.reset_counters()

    result = scan(client, settings, cache=CalendarCache.load(settings.cache_path))
    assert (result.cache_hits, result.cache_misses) == (2, 1)
    assert reads(client) == ["p_2025_03"]
    marzo = next(p for p in result.playlists if p.month == 3)
    assert "spotify:track:nuovo" in [t.uri for t in marzo.tracks]


class NoSnapshotClient(FakeSpotifyClient):
    """Simula D5 = "snapshot_id assente" in GET /me/playlists."""

    def list_my_playlists(self) -> list[PlaylistInfo]:
        from dataclasses import replace

        return [replace(p, snapshot_id=None) for p in super().list_my_playlists()]


def test_senza_snapshot_si_rilegge_sempre(settings: Settings) -> None:
    c = NoSnapshotClient()
    c.seed_playlist("Marzo 2025", [make_track("a")], playlist_id="p1")
    scan(c, settings, cache=CalendarCache.load(settings.cache_path))
    c.reset_counters()
    result = scan(c, settings, cache=CalendarCache.load(settings.cache_path))
    assert (result.cache_hits, result.cache_misses) == (0, 1)
    assert reads(c) == ["p1"]


def test_cache_elimina_le_playlist_sparite(client: FakeSpotifyClient, settings: Settings) -> None:
    scan(client, settings, cache=CalendarCache.load(settings.cache_path))
    client.delete_playlist_for_test("p_2024_12")
    scan(client, settings, cache=CalendarCache.load(settings.cache_path))
    assert set(CalendarCache.load(settings.cache_path).entries) == {"p_2025_03", "p_2025_05"}


def test_cache_con_filtro_non_perde_le_altre_playlist(client: FakeSpotifyClient, settings: Settings) -> None:
    scan(client, settings, cache=CalendarCache.load(settings.cache_path))
    scan(client, settings, ScanFilters(year=2025), cache=CalendarCache.load(settings.cache_path))
    assert "p_2024_12" in CalendarCache.load(settings.cache_path).entries


def test_cache_corrotta_non_blocca_la_scansione(client: FakeSpotifyClient, settings: Settings) -> None:
    settings.cache_path.parent.mkdir(parents=True, exist_ok=True)
    settings.cache_path.write_text("{rotto", encoding="utf-8")
    result = scan(client, settings, cache=CalendarCache.load(settings.cache_path))
    assert len(result.playlists) == 3
    assert result.cache_misses == 3
    assert result.warnings  # l'avviso della cache arriva fino al report


def test_errore_di_salvataggio_cache_diventa_avviso(
    client: FakeSpotifyClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args, **kwargs):
        raise OSError("disco pieno")

    monkeypatch.setattr("spotify_calendar_helper.cache.os.replace", boom)
    result = scan(client, settings, cache=CalendarCache.load(settings.cache_path))
    assert len(result.playlists) == 3
    assert any("disco pieno" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# B4: summarize()
# ---------------------------------------------------------------------------


def test_riepilogo_calendario_vuoto(settings: Settings) -> None:
    s = summarize(scan(FakeSpotifyClient(), settings))
    assert s.total_playlists == 0 and s.total_tracks == 0
    assert s.years == () and s.missing_months == () and s.duplicate_months == ()


def test_riepilogo_anni_mesi_e_brani(client: FakeSpotifyClient, settings: Settings) -> None:
    s = summarize(scan(client, settings))
    assert s.years == (2024, 2025)
    assert s.months_by_year == {2024: (12,), 2025: (3, 5)}
    assert s.tracks_by_year == {2024: 1, 2025: 3}
    assert (s.total_playlists, s.total_tracks) == (3, 4)
    assert sorted(s.unrecognized) == ["Preferiti", "Roadtrip"]


def test_riepilogo_buchi_attraversano_gli_anni(client: FakeSpotifyClient, settings: Settings) -> None:
    s = summarize(scan(client, settings))
    # Da 2024-12 a 2025-05 mancano gennaio, febbraio e aprile 2025.
    assert s.missing_months == ((2025, 1), (2025, 2), (2025, 4))


def test_riepilogo_senza_buchi(settings: Settings) -> None:
    c = FakeSpotifyClient()
    for name in ("2025-01", "2025-02", "2025-03"):
        c.seed_playlist(name, [make_track(name)])
    assert summarize(scan(c, settings)).missing_months == ()


def test_riepilogo_un_solo_mese_non_ha_buchi(settings: Settings) -> None:
    c = FakeSpotifyClient()
    c.seed_playlist("Marzo 2025", [make_track("a")])
    assert summarize(scan(c, settings)).missing_months == ()


def test_riepilogo_con_filtro_calcola_i_buchi_sul_filtro(client: FakeSpotifyClient, settings: Settings) -> None:
    s = summarize(scan(client, settings, ScanFilters(year=2025)))
    assert s.missing_months == ((2025, 4),)


def test_riepilogo_mesi_duplicati_ignorate_non_lette_e_saltati(settings: Settings) -> None:
    c = FakeSpotifyClient()
    c.seed_playlist("Marzo 2025", [make_track("a")], skipped=2)
    c.seed_playlist("2025-03 extra", [make_track("b")], skipped=1)
    c.seed_playlist("Aprile 2025", [make_track("c")], playlist_id="p_ignorata")
    c.seed_playlist("Maggio 2025", [make_track("d")], owner_id="altro")
    settings.config["calendar"]["ignore_playlist_ids"] = ["p_ignorata"]
    s = summarize(scan(c, settings))
    assert s.duplicate_months == ((2025, 3),)
    assert s.ignored_ids == ("p_ignorata",)
    assert s.not_owned == ("Maggio 2025",)
    assert s.skipped_items == 3
    assert s.total_playlists == 2 and s.months_by_year == {2025: (3,)}
