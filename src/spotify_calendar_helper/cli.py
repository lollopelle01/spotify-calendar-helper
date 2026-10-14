"""CLI `sch`: interfaccia a riga di comando per spotify-calendar-helper (task D4).

Comandi:
  sch auth                          login PKCE (o ri-login se il token è scaduto)
  sch calendar [filtri]             elenca le playlist del calendario riconosciute
  sch plan   artists|genres [filtri] mostra cosa verrebbe creato/aggiornato (dry-run)
  sch sync   artists|genres [filtri] applica le modifiche
  sch status                        playlist derivate note e ultime esecuzioni
  sch genres-report                 generi non mappati e artisti senza generi

Filtri comuni: --year AAAA   --from AAAA-MM   --to AAAA-MM   --all
               --dry-run     --prune           --yes (salta conferma interattiva)

Codici di uscita: 0 OK, 1 errore generale, 2 abortito dall'utente.
"""

from __future__ import annotations

import sys
from typing import NoReturn

import click

from .calendar_scan import ScanFilters, parse_year_month, scan, summarize
from .cache import CalendarCache
from .config import Settings, load_settings
from .genres import GenreMap, build_provider
from .grouping import group_by_artist, group_by_decade, group_by_genre
from .models import SyncReport
from .state import State
from .sync import SyncOptions, sync_specs

# ---------------------------------------------------------------------------
# Utilità
# ---------------------------------------------------------------------------

ITALIAN_MONTHS = [
    "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
    "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre",
]


def _abort(message: str) -> NoReturn:
    click.echo(f"Errore: {message}", err=True)
    sys.exit(1)


def _warn(message: str) -> None:
    click.echo(f"Attenzione: {message}", err=True)


def _load_settings() -> Settings:
    try:
        return load_settings()
    except SystemExit as exc:
        _abort(str(exc))


def _make_filters(year: int | None, from_: str | None, to: str | None, all_: bool) -> ScanFilters:
    try:
        from_ym = parse_year_month(from_) if from_ else None
        to_ym = parse_year_month(to) if to else None
        return ScanFilters(year=year, from_ym=from_ym, to_ym=to_ym, all=all_)
    except ValueError as exc:
        _abort(str(exc))


def _month_name(month: int) -> str:
    return ITALIAN_MONTHS[month - 1].capitalize()


def _print_warnings(warnings: list[str]) -> None:
    for w in warnings:
        _warn(w)


def _print_report(report: SyncReport, *, dry_run: bool) -> None:
    prefix = "[DRY-RUN] " if dry_run else ""
    if report.created:
        click.echo(f"\n{prefix}Playlist create ({len(report.created)}):")
        for c in report.created:
            pid = f" → {c.playlist_id}" if c.playlist_id else ""
            click.echo(f"  + {c.name}{pid}  (+{c.added} brani)")
    if report.updated:
        click.echo(f"\n{prefix}Playlist aggiornate ({len(report.updated)}):")
        for u in report.updated:
            parts = []
            if u.added:
                parts.append(f"+{u.added}")
            if u.removed:
                parts.append(f"-{u.removed}")
            diff = " ".join(parts) if parts else "marker ripristinato"
            click.echo(f"  ~ {u.name}  ({diff} brani)")
    if report.unchanged:
        click.echo(f"\n{prefix}Invariate: {len(report.unchanged)}")
    if report.warnings:
        click.echo("")
        _print_warnings(report.warnings)
    if report.skipped_tracks:
        click.echo(f"\nBrani saltati: {len(report.skipped_tracks)}")
    # playlist nuove da spostare nella cartella
    if report.created and not dry_run:
        click.echo("\n── Playlist nuove da spostare nella cartella giusta ──")
        for c in report.created:
            if c.playlist_id:
                click.echo(f"  {c.name}")
        click.echo("  (gli aggiornamenti successivi le trovano anche se le sposti)")


def _confirm_if_many(n_new: int, threshold: int, yes: bool) -> bool:
    """Chiede conferma se si stanno creando più di `threshold` playlist. Restituisce False se si abbandona."""
    if yes or n_new <= threshold:
        return True
    click.echo(
        f"\nIl piano crea {n_new} nuove playlist (soglia: {threshold}).\n"
        "Puoi ridurre il numero con --year o --from/--to."
    )
    return click.confirm("Vuoi procedere?", default=False)


# ---------------------------------------------------------------------------
# Gruppo principale
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(package_name="spotify-calendar-helper")
def main() -> None:
    """Genera e aggiorna playlist Spotify dal tuo calendario mensile."""


# ---------------------------------------------------------------------------
# Opzioni filtro condivise (usate da calendar, plan, sync)
# ---------------------------------------------------------------------------

_filter_options = [
    click.option("--year", type=int, default=None, help="Solo le playlist di un anno (es. 2025)."),
    click.option("--from", "from_", metavar="AAAA-MM", default=None, help="Dal mese incluso."),
    click.option("--to", metavar="AAAA-MM", default=None, help="Al mese incluso."),
    click.option("--all", "all_", is_flag=True, default=False, help="Tutti i mesi (nessun filtro)."),
]


def _add_filter_options(f):
    for opt in reversed(_filter_options):
        f = opt(f)
    return f


# ---------------------------------------------------------------------------
# sch auth
# ---------------------------------------------------------------------------

@main.command()
def auth() -> None:
    """Esegui il login Spotify (o rinnova il token scaduto)."""
    settings = _load_settings()
    from .auth import login  # noqa: PLC0415
    try:
        login(settings)
    except Exception as exc:  # noqa: BLE001
        _abort(str(exc))


# ---------------------------------------------------------------------------
# sch calendar
# ---------------------------------------------------------------------------

@main.command("calendar")
@_add_filter_options
def calendar_cmd(year: int | None, from_: str | None, to: str | None, all_: bool) -> None:
    """Elenca le playlist del calendario riconosciute."""
    settings = _load_settings()
    filters = _make_filters(year, from_, to, all_)

    client = _make_client(settings)
    cache = CalendarCache.load(settings.cache_path)
    result = scan(client, settings, filters, cache)
    summary = summarize(result)

    _print_warnings(result.warnings)

    if not summary.total_playlists:
        click.echo("Nessuna playlist del calendario trovata con i filtri applicati.")
        return

    click.echo(f"\nPlaylist del calendario: {summary.total_playlists}  |  Brani totali: {summary.total_tracks}")
    for year_val, months in summary.months_by_year.items():
        brani = summary.tracks_by_year.get(year_val, 0)
        click.echo(f"\n  {year_val}  ({brani} brani):")
        for m in months:
            click.echo(f"    {_month_name(m)}")

    if summary.missing_months:
        names = [f"{y}-{m:02d}" for y, m in summary.missing_months]
        click.echo(f"\nMesi mancanti ({len(names)}): {', '.join(names)}")
    if summary.duplicate_months:
        names = [f"{y}-{m:02d}" for y, m in summary.duplicate_months]
        click.echo(f"Mesi duplicati: {', '.join(names)}")
    if summary.not_owned:
        click.echo(f"Non tue (non lette): {len(summary.not_owned)}")
    if summary.ignored_ids:
        click.echo(f"Ignorate (ignore_playlist_ids): {len(summary.ignored_ids)}")
    if summary.skipped_items:
        click.echo(f"Brani saltati (local/episodi): {summary.skipped_items}")


# ---------------------------------------------------------------------------
# sch plan / sch sync  (logica condivisa)
# ---------------------------------------------------------------------------

def _run_artists_or_genres(
    kind: str,
    settings: Settings,
    filters: ScanFilters,
    options: SyncOptions,
    yes: bool,
    command: str,
) -> None:
    client = _make_client(settings)
    cache = CalendarCache.load(settings.cache_path)
    result = scan(client, settings, filters, cache)
    _print_warnings(result.warnings)

    if not result.playlists:
        click.echo("Nessuna playlist del calendario trovata con i filtri applicati.")
        return

    click.echo(f"Calendario: {len(result.playlists)} playlist  ({result.cache_hits} dalla cache, {result.cache_misses} rilette)")

    state = State.load(settings.state_path)
    if state.warning:
        _warn(state.warning)

    if kind == "artists":
        specs = group_by_artist(result.playlists, settings)
    else:
        genre_map = _load_genre_map(settings)
        provider = build_provider(settings, client=client)
        specs, genres_report = group_by_genre(result.playlists, provider, genre_map, settings)
        if hasattr(provider, "save"):
            provider.save()
        if genres_report.unmapped_genres:
            click.echo(f"Generi non mappati: {len(genres_report.unmapped_genres)} (vedi `sch genres-report`)")
        if genres_report.artists_without_genres:
            click.echo(f"Artisti senza generi: {len(genres_report.artists_without_genres)}")

    if not specs:
        click.echo("Nessuna playlist da creare o aggiornare.")
        return

    click.echo(f"Specifiche generate: {len(specs)}")

    # conferma se si creano troppe playlist
    threshold = int(settings.section("sync").get("confirm_if_creating_more_than", 20))
    existing_keys = {f"{r.kind}:{k}" for k, r in state.playlists.items()} if hasattr(state, "playlists") else set()
    new_specs = [s for s in specs if f"{s.kind}:{s.key}" not in existing_keys]
    if not options.dry_run and not _confirm_if_many(len(new_specs), threshold, yes):
        click.echo("Operazione annullata.")
        sys.exit(2)

    report = sync_specs(client, state, specs, settings, options, command=command)

    # popola skipped_tracks dal ScanResult
    if result.skipped_items:
        report.skipped_tracks.append(f"{result.skipped_items} brani non gestibili saltati in lettura")

    _print_report(report, dry_run=options.dry_run)

    total = len(report.created) + len(report.updated)
    if options.dry_run:
        click.echo(f"\nDRY-RUN: {len(report.created)} da creare, {len(report.updated)} da aggiornare, {len(report.unchanged)} invariate. Nessuna modifica applicata.")
    elif total:
        click.echo(f"\nFatto: {len(report.created)} create, {len(report.updated)} aggiornate, {len(report.unchanged)} invariate.")
    else:
        click.echo("\nNessuna modifica necessaria.")


@main.command("plan")
@click.argument("kind", type=click.Choice(["artists", "genres"]))
@_add_filter_options
@click.option("--prune", is_flag=True, default=False, help="Mostra anche i brani che verrebbero rimossi (solo con sync.mode=mirror).")
def plan_cmd(kind: str, year: int | None, from_: str | None, to: str | None, all_: bool, prune: bool) -> None:
    """Mostra cosa verrebbe creato/aggiornato senza applicare (dry-run)."""
    settings = _load_settings()
    filters = _make_filters(year, from_, to, all_)
    _run_artists_or_genres(kind, settings, filters, SyncOptions(dry_run=True, prune=prune), yes=True, command="plan")


@main.command("sync")
@click.argument("kind", type=click.Choice(["artists", "genres"]))
@_add_filter_options
@click.option("--dry-run", is_flag=True, default=False, help="Non applicare: mostra solo il piano.")
@click.option("--prune", is_flag=True, default=False, help="Rimuovi i brani spariti dal calendario (solo con sync.mode=mirror).")
@click.option("--yes", is_flag=True, default=False, help="Salta la conferma interattiva.")
def sync_cmd(kind: str, year: int | None, from_: str | None, to: str | None, all_: bool,
             dry_run: bool, prune: bool, yes: bool) -> None:
    """Crea e aggiorna le playlist derivate su Spotify."""
    settings = _load_settings()
    filters = _make_filters(year, from_, to, all_)
    _run_artists_or_genres(kind, settings, filters, SyncOptions(dry_run=dry_run, prune=prune), yes=yes, command="sync")


# ---------------------------------------------------------------------------
# sch plan decade / sch sync decade
# ---------------------------------------------------------------------------


def _run_decade(
    settings: Settings,
    filters: ScanFilters,
    options: SyncOptions,
    yes: bool,
    command: str,
) -> None:
    from .cache import AlbumYearCache

    client = _make_client(settings)
    cache = CalendarCache.load(settings.cache_path)
    result = scan(client, settings, filters, cache)
    _print_warnings(result.warnings)

    if not result.playlists:
        click.echo("Nessuna playlist del calendario trovata con i filtri applicati.")
        return

    click.echo(f"Calendario: {len(result.playlists)} playlist  ({result.cache_hits} dalla cache, {result.cache_misses} rilette)")

    album_cache = AlbumYearCache.load(settings.album_year_cache_path)
    if album_cache.warning:
        _warn(album_cache.warning)

    # Costruiamo un dict passabile a group_by_decade
    album_year_dict: dict[str, int | None] = {}

    def client_with_cache(album_id: str) -> int | None:
        hit, year = album_cache.get(album_id)
        if hit:
            return year
        year = client.get_album_year(album_id)
        album_cache.set(album_id, year)
        return year

    # Usiamo un oggetto proxy per intercettare get_album_year
    class _CachingClient:
        def __getattr__(self, name):
            return getattr(client, name)

        def get_album_year(self, album_id: str) -> int | None:
            return client_with_cache(album_id)

    specs = group_by_decade(result.playlists, _CachingClient(), settings)

    try:
        album_cache.save()
    except OSError as exc:
        _warn(f"Impossibile salvare la cache album: {exc}")

    state = State.load(settings.state_path)
    if state.warning:
        _warn(state.warning)

    if not specs:
        click.echo("Nessuna playlist per decade da creare o aggiornare.")
        return

    click.echo(f"Specifiche generate: {len(specs)}")

    threshold = int(settings.section("sync").get("confirm_if_creating_more_than", 20))
    existing_keys = {f"{r.kind}:{k}" for k, r in state.playlists.items()} if hasattr(state, "playlists") else set()
    new_specs = [s for s in specs if f"{s.kind}:{s.key}" not in existing_keys]
    if not options.dry_run and not _confirm_if_many(len(new_specs), threshold, yes):
        click.echo("Operazione annullata.")
        sys.exit(2)

    report = sync_specs(client, state, specs, settings, options, command=command)

    if result.skipped_items:
        report.skipped_tracks.append(f"{result.skipped_items} brani non gestibili saltati in lettura")

    _print_report(report, dry_run=options.dry_run)

    total = len(report.created) + len(report.updated)
    if options.dry_run:
        click.echo(f"\nDRY-RUN: {len(report.created)} da creare, {len(report.updated)} da aggiornare, {len(report.unchanged)} invariate. Nessuna modifica applicata.")
    elif total:
        click.echo(f"\nFatto: {len(report.created)} create, {len(report.updated)} aggiornate, {len(report.unchanged)} invariate.")
    else:
        click.echo("\nNessuna modifica necessaria.")


@main.command("plan-decade")
@_add_filter_options
@click.option("--prune", is_flag=True, default=False)
def plan_decade_cmd(year: int | None, from_: str | None, to: str | None, all_: bool, prune: bool) -> None:
    """Mostra le playlist per decennio di uscita che verrebbero create (dry-run)."""
    settings = _load_settings()
    filters = _make_filters(year, from_, to, all_)
    _run_decade(settings, filters, SyncOptions(dry_run=True, prune=prune), yes=True, command="plan-decade")


@main.command("sync-decade")
@_add_filter_options
@click.option("--dry-run", is_flag=True, default=False)
@click.option("--prune", is_flag=True, default=False)
@click.option("--yes", is_flag=True, default=False)
def sync_decade_cmd(year: int | None, from_: str | None, to: str | None, all_: bool,
                    dry_run: bool, prune: bool, yes: bool) -> None:
    """Crea e aggiorna le playlist per decennio di uscita degli album."""
    settings = _load_settings()
    filters = _make_filters(year, from_, to, all_)
    _run_decade(settings, filters, SyncOptions(dry_run=dry_run, prune=prune), yes=yes, command="sync-decade")


# ---------------------------------------------------------------------------
# sch status
# ---------------------------------------------------------------------------

@main.command("status")
def status_cmd() -> None:
    """Mostra le playlist derivate note e le ultime esecuzioni."""
    settings = _load_settings()
    state = State.load(settings.state_path)

    if state.warning:
        _warn(state.warning)

    if not state.playlists:
        click.echo("Nessuna playlist derivata registrata (hai già eseguito `sch sync`?).")
    else:
        by_kind: dict[str, list] = {}
        for key, record in state.playlists.items():
            by_kind.setdefault(record.kind, []).append((key, record))

        for kind in ("artist", "genre"):
            entries = by_kind.get(kind, [])
            if not entries:
                continue
            label = "Artisti" if kind == "artist" else "Generi"
            click.echo(f"\n{label} ({len(entries)}):")
            for _, record in sorted(entries, key=lambda x: x[1].name.casefold()):
                click.echo(f"  {record.name}  [{record.playlist_id}]  ultimo sync: {record.last_sync[:10]}")

    if state.runs:
        click.echo(f"\nUltimi run ({min(len(state.runs), 5)}):")
        for run in state.runs[-5:]:
            dry = " [dry-run]" if run.dry_run else ""
            click.echo(
                f"  {run.timestamp[:16]}  {run.command}{dry}  "
                f"create={run.created} aggiornate={run.updated} invariate={run.unchanged}"
                + (f"  avvisi={run.warnings}" if run.warnings else "")
            )
    else:
        click.echo("\nNessun run registrato.")


# ---------------------------------------------------------------------------
# sch genres-report
# ---------------------------------------------------------------------------

@main.command("genres-report")
@_add_filter_options
def genres_report_cmd(year: int | None, from_: str | None, to: str | None, all_: bool) -> None:
    """Mostra generi non mappati e artisti senza generi (per aggiornare genre_map.toml)."""
    settings = _load_settings()
    filters = _make_filters(year, from_, to, all_)

    client = _make_client(settings)
    cache = CalendarCache.load(settings.cache_path)
    result = scan(client, settings, filters, cache)
    _print_warnings(result.warnings)

    if not result.playlists:
        click.echo("Nessuna playlist del calendario trovata.")
        return

    genre_map = _load_genre_map(settings)
    provider = build_provider(settings, client=client)
    _, genres_report = group_by_genre(result.playlists, provider, genre_map, settings)
    if hasattr(provider, "save"):
        provider.save()

    if not genres_report.unmapped_genres and not genres_report.artists_without_genres:
        click.echo("Tutti i generi sono mappati.")
        return

    if genres_report.unmapped_genres:
        click.echo(f"\nGeneri non mappati ({len(genres_report.unmapped_genres)}) — aggiungili a genre_map.toml:")
        for tag, count in sorted(genres_report.unmapped_genres.items(), key=lambda x: -x[1]):
            click.echo(f"  {count:3d}  {tag}")

    if genres_report.artists_without_genres:
        click.echo(f"\nArtisti senza generi ({len(genres_report.artists_without_genres)}):")
        for artist in sorted(genres_report.artists_without_genres, key=lambda a: a.name.casefold()):
            click.echo(f"  {artist.name}  [{artist.id}]")


# ---------------------------------------------------------------------------
# Helpers interni (segnaposto per auth.py)
# ---------------------------------------------------------------------------

def _make_client(settings: Settings):
    """Costruisce il client Spotify reale. Richiede auth.py e client.py (Agente A)."""
    from .client import SpotifyHttpClient  # type: ignore[import]
    return SpotifyHttpClient(settings)


def _load_genre_map(settings: Settings) -> GenreMap:
    try:
        return GenreMap.load(settings.genre_map_path)
    except ValueError as exc:
        _abort(str(exc))
