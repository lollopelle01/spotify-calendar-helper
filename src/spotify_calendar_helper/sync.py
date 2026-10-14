"""Sync: porta Spotify nello stato "desiderato" descritto dalle `PlaylistSpec` (task D2 e D3).

Riconoscimento (D2), in quest'ordine, per ogni specifica:
1. l'ID registrato in `state.json`, se la playlist esiste ancora tra le tue;
2. il marker `[sch:<kind>:<key>]` nella descrizione di una playlist TUA (recupero se lo stato
   è perso o la playlist è stata cancellata e ricreata a mano);
3. nessuna => da creare.
Se più playlist tue hanno lo stesso marker se ne usa una sola e si avvisa. `PlaylistInfo` non ha
una data di creazione, quindi la scelta è: quella con più brani, a parità la prima nell'elenco.

Applicazione (D3):
- si aggiungono SOLO i brani mancanti (confronto per URI), nell'ordine cronologico della specifica;
- nome, descrizione e immagine delle playlist esistenti non si toccano, tranne il ripristino del
  marker se manca (accodato alla descrizione esistente, senza sostituirla);
- si rimuove qualcosa SOLO con `prune` E `sync.mode = "mirror"`: i brani presenti nella playlist
  ma non più nella specifica. Con `mode = "additive"` il flag viene ignorato con un avviso;
- `dry_run` non scrive nulla (né su Spotify né in `state.json`) e riporta cosa succederebbe;
- le playlist create portano il marker fin dall'inizio: se il run si interrompe a metà, il run
  successivo le ritrova dal marker invece di crearne di duplicate.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .config import Settings
from .models import (
    PlaylistChange,
    PlaylistInfo,
    PlaylistSpec,
    SyncReport,
    make_marker,
    parse_marker,
)
from .protocols import SpotifyClient
from .state import RunRecord, State, utc_now


@dataclass(frozen=True)
class SyncOptions:
    dry_run: bool = False
    prune: bool = False


@dataclass(frozen=True)
class Match:
    playlist: PlaylistInfo | None
    via: str  # "state" | "marker" | "none"
    warnings: tuple[str, ...] = ()


def _spec_label(spec: PlaylistSpec) -> str:
    return f"{spec.kind}:{spec.key}"


def _unique(uris: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(uris))


def find_existing(
    spec: PlaylistSpec,
    state: State,
    playlists: Sequence[PlaylistInfo],
    user_id: str,
) -> Match:
    """Trova la playlist di `spec` tra quelle dell'utente (vedi docstring del modulo)."""
    mine = [p for p in playlists if p.owner_id == user_id]
    by_id = {p.id: p for p in mine}
    warnings: list[str] = []

    record = state.get(spec.kind, spec.key)
    if record is not None and record.playlist_id in by_id:
        return Match(by_id[record.playlist_id], "state")

    marked = [p for p in mine if parse_marker(p.description) == (spec.kind, spec.key)]
    if marked:
        # max() restituisce il primo tra i pari: più brani vince, poi l'ordine dell'elenco.
        chosen = max(marked, key=lambda p: p.track_count or 0)
        if len(marked) > 1:
            ids = ", ".join(p.id for p in marked)
            warnings.append(
                f"{_spec_label(spec)}: {len(marked)} playlist hanno lo stesso marker ({ids}); "
                f"uso {chosen.id}. Elimina a mano le altre."
            )
        if record is not None:
            warnings.append(
                f"{_spec_label(spec)}: la playlist registrata nello stato non esiste più, "
                f"ricollego {chosen.id} dal marker."
            )
        return Match(chosen, "marker", tuple(warnings))

    if record is not None:
        warnings.append(
            f"{_spec_label(spec)}: la playlist registrata ({record.playlist_id}) non esiste più "
            "e non ne ho trovate con il marker: ne creo una nuova."
        )
    return Match(None, "none", tuple(warnings))


def _description_with_marker(description: str, spec: PlaylistSpec) -> str:
    marker = make_marker(spec.kind, spec.key)
    return f"{description.rstrip()} {marker}".strip()


def sync_specs(
    client: SpotifyClient,
    state: State,
    specs: Sequence[PlaylistSpec],
    settings: Settings,
    options: SyncOptions | None = None,
    command: str = "sync",
) -> SyncReport:
    options = options or SyncOptions()
    report = SyncReport(dry_run=options.dry_run)

    prune = options.prune
    if prune and str(settings.section("sync").get("mode", "additive")) != "mirror":
        report.warnings.append(
            "--prune ignorato: serve sync.mode = \"mirror\" in config.toml (ora è \"additive\")."
        )
        prune = False

    user_id = client.current_user().id
    playlists = client.list_my_playlists()
    seen: set[str] = set()

    try:
        for spec in specs:
            label = _spec_label(spec)
            if label in seen:
                report.warnings.append(f"{label}: specifica duplicata, la ignoro.")
                continue
            seen.add(label)

            match = find_existing(spec, state, playlists, user_id)
            report.warnings.extend(match.warnings)
            uris = _unique(spec.track_uris)

            if match.playlist is None:
                _create(client, state, spec, uris, settings, options, report)
            else:
                _update(client, state, spec, uris, match, options, prune, report)
    finally:
        if not options.dry_run:
            state.add_run(
                RunRecord(
                    timestamp=utc_now(),
                    command=command,
                    dry_run=False,
                    created=len(report.created),
                    updated=len(report.updated),
                    unchanged=len(report.unchanged),
                    warnings=len(report.warnings),
                )
            )
            try:
                state.save()
            except OSError as exc:
                report.warnings.append(f"Non riesco a salvare lo stato locale: {exc}")
    return report


def _create(
    client: SpotifyClient,
    state: State,
    spec: PlaylistSpec,
    uris: list[str],
    settings: Settings,
    options: SyncOptions,
    report: SyncReport,
) -> None:
    if options.dry_run:
        report.created.append(
            PlaylistChange(spec.kind, spec.key, spec.name, None, added=len(uris))
        )
        return
    info = client.create_playlist(spec.name, spec.description, public=settings.public_playlists)
    state.put(spec.kind, spec.key, info.id, info.name)  # prima dell'aggiunta: vedi docstring
    if uris:
        client.add_items(info.id, uris)
    report.created.append(PlaylistChange(spec.kind, spec.key, info.name, info.id, added=len(uris)))


def _update(
    client: SpotifyClient,
    state: State,
    spec: PlaylistSpec,
    uris: list[str],
    match: Match,
    options: SyncOptions,
    prune: bool,
    report: SyncReport,
) -> None:
    info = match.playlist
    assert info is not None
    existing = [t.uri for t in client.get_playlist_items(info.id).tracks]
    existing_set = set(existing)
    desired = set(uris)

    missing = [u for u in uris if u not in existing_set]
    extra = [u for u in _unique(existing) if u not in desired] if prune else []
    needs_marker = make_marker(spec.kind, spec.key) not in (info.description or "")

    if not options.dry_run:
        if missing:
            client.add_items(info.id, missing)
        if extra:
            client.remove_items(info.id, extra)
        if needs_marker:
            client.update_details(
                info.id, description=_description_with_marker(info.description or "", spec)
            )
        state.put(spec.kind, spec.key, info.id, info.name)

    if missing or extra or needs_marker:
        report.updated.append(
            PlaylistChange(
                spec.kind, spec.key, info.name, info.id, added=len(missing), removed=len(extra)
            )
        )
    else:
        report.unchanged.append(_spec_label(spec))
