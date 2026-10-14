"""Stato locale del sync: quali playlist derivate sono state create e cosa è successo ai run (task D1).

NON è la fonte di verità: quella è il marker `[sch:<kind>:<key>]` nella descrizione delle playlist
su Spotify. Lo stato è una scorciatoia: se va perso o si corrompe, il sync ritrova le playlist
dal marker e lo ricostruisce.

Formato (data/state.json, ignorato da git):

    {"version": 1,
     "playlists": {"artist:<id>": {"playlist_id", "name", "kind", "created_at", "last_sync"}},
     "runs": [{"timestamp", "command", "dry_run", "created", "updated", "unchanged", "warnings"}]}

File mancante, corrotto o di un'altra versione => stato vuoto e `warning` valorizzato (nessuna
eccezione). Scrittura atomica. Dello storico dei run si tengono gli ultimi MAX_RUNS.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import PlaylistKind

STATE_VERSION = 1
MAX_RUNS = 50
_KINDS = ("artist", "genre")


@dataclass(frozen=True)
class PlaylistRecord:
    playlist_id: str
    name: str
    kind: PlaylistKind
    created_at: str
    last_sync: str


@dataclass(frozen=True)
class RunRecord:
    timestamp: str
    command: str
    dry_run: bool = False
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    warnings: int = 0


def state_key(kind: PlaylistKind, key: str) -> str:
    return f"{kind}:{key}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _str(data: Mapping[str, Any], key: str) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise ValueError(f"campo {key!r} non è una stringa")
    return value


def _count(data: Mapping[str, Any], key: str) -> int:
    value = data.get(key, 0)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"campo {key!r} non è un intero >= 0")
    return value


def _playlist_from_dict(data: Mapping[str, Any]) -> PlaylistRecord:
    kind = _str(data, "kind")
    if kind not in _KINDS:
        raise ValueError(f"kind sconosciuto: {kind!r}")
    return PlaylistRecord(
        playlist_id=_str(data, "playlist_id"),
        name=_str(data, "name"),
        kind=kind,  # type: ignore[arg-type]
        created_at=_str(data, "created_at"),
        last_sync=_str(data, "last_sync"),
    )


def _run_from_dict(data: Mapping[str, Any]) -> RunRecord:
    dry_run = data.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise ValueError("campo 'dry_run' non è un booleano")
    return RunRecord(
        timestamp=_str(data, "timestamp"),
        command=_str(data, "command"),
        dry_run=dry_run,
        created=_count(data, "created"),
        updated=_count(data, "updated"),
        unchanged=_count(data, "unchanged"),
        warnings=_count(data, "warnings"),
    )


class State:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.playlists: dict[str, PlaylistRecord] = {}
        self.runs: list[RunRecord] = []
        self.warning: str | None = None

    @classmethod
    def load(cls, path: Path) -> State:
        state = cls(path)
        if not path.exists():
            return state
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            state.warning = (
                f"Stato locale illeggibile ({exc.__class__.__name__}): lo ricostruisco dai "
                "marker delle playlist su Spotify."
            )
            return state
        if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
            state.warning = (
                "Stato locale di un'altra versione o non valido: lo ricostruisco dai marker."
            )
            return state

        discarded = 0
        playlists = data.get("playlists", {})
        if isinstance(playlists, dict):
            for key, raw in playlists.items():
                try:
                    state.playlists[str(key)] = _playlist_from_dict(raw)
                except (KeyError, TypeError, ValueError, AttributeError):
                    discarded += 1
        else:
            discarded += 1
        runs = data.get("runs", [])
        if isinstance(runs, list):
            for raw in runs:
                try:
                    state.runs.append(_run_from_dict(raw))
                except (KeyError, TypeError, ValueError, AttributeError):
                    discarded += 1
            state.runs = state.runs[-MAX_RUNS:]
        else:
            discarded += 1
        if discarded:
            state.warning = f"{discarded} voci dello stato non valide scartate."
        return state

    # --- playlist ---

    def get(self, kind: PlaylistKind, key: str) -> PlaylistRecord | None:
        return self.playlists.get(state_key(kind, key))

    def put(
        self,
        kind: PlaylistKind,
        key: str,
        playlist_id: str,
        name: str,
        now: str | None = None,
    ) -> PlaylistRecord:
        """Registra (o aggiorna) la playlist di `kind:key`. `created_at` resta quello originale
        finché l'ID della playlist non cambia."""
        stamp = now or utc_now()
        existing = self.get(kind, key)
        created_at = existing.created_at if existing and existing.playlist_id == playlist_id else stamp
        record = PlaylistRecord(
            playlist_id=playlist_id, name=name, kind=kind, created_at=created_at, last_sync=stamp
        )
        self.playlists[state_key(kind, key)] = record
        return record

    def forget(self, kind: PlaylistKind, key: str) -> bool:
        return self.playlists.pop(state_key(kind, key), None) is not None

    # --- storico ---

    def add_run(self, run: RunRecord) -> None:
        self.runs.append(run)
        del self.runs[:-MAX_RUNS]

    @property
    def last_run(self) -> RunRecord | None:
        return self.runs[-1] if self.runs else None

    # --- persistenza ---

    def save(self) -> None:
        payload = {
            "version": STATE_VERSION,
            "playlists": {k: asdict(r) for k, r in self.playlists.items()},
            "runs": [asdict(r) for r in self.runs],
        }
        _write_json_atomic(self.path, payload)
