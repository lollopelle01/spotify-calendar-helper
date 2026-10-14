"""Fixture dei test live. Questi test sono l'UNICA eccezione alla regola "niente rete, niente .env":
leggono il vero .env, fanno il login nel browser e chiamano l'API reale di Spotify.

Non girano mai nella suite normale (vedi `addopts` in pyproject.toml): si lanciano a mano con
    pytest -m live -s
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from spotify_calendar_helper.config import Settings, load_settings
from tests.live.probe import Probe, ProbeError, dump, login


@pytest.fixture(scope="session")
def live_settings() -> Settings:
    try:
        return load_settings()
    except SystemExit as exc:  # manca SPOTIFY_CLIENT_ID
        pytest.skip(str(exc))


@pytest.fixture(scope="session")
def spike_dir(live_settings: Settings) -> Path:
    """Dove finiscono le risposte grezze: data/spike/ (ignorata da git, contiene dati personali)."""
    return live_settings.data_dir / "spike"


@pytest.fixture(scope="session")
def findings(spike_dir: Path) -> Iterator[dict[str, str]]:
    """Raccoglie gli esiti D1/D3/D4/D5; a fine sessione li stampa e li salva in summary.txt."""
    found: dict[str, str] = {}
    yield found
    order = ["D1", "D3", "D4", "D5", "429"]
    lines = [f"{key}: {found[key]}" for key in order if key in found]
    text = "\n".join(lines) if lines else "(nessun esito raccolto)"
    spike_dir.mkdir(parents=True, exist_ok=True)
    (spike_dir / "summary.txt").write_text(text + "\n", encoding="utf-8")
    print("\n" + "=" * 60)
    print("RIEPILOGO SPIKE (da riportare in AGENTS.md, sezione 4)")
    print("=" * 60)
    print(text)
    print(f"\nRisposte grezze in {spike_dir} (dati personali: anonimizza prima di fare fixture).")


@pytest.fixture(scope="session")
def probe(live_settings: Settings, findings: dict[str, str]) -> Iterator[Probe]:
    try:
        token = login(live_settings.client_id, live_settings.redirect_uri)
    except ProbeError as exc:
        pytest.fail(str(exc))
    p = Probe(token)
    me = p.call("GET", "/me")
    assert me.ok, f"GET /me -> {me.status_code}: {me.text} (account aggiunto in User Management?)"
    yield p
    if p.saw_429:
        findings["429"] = f"visto; header Retry-After presente: {p.retry_after_header_present}"


@pytest.fixture(scope="session")
def my_playlists(probe: Probe, spike_dir: Path) -> dict[str, Any]:
    response = probe.call("GET", "/me/playlists", params={"limit": 50})
    assert response.ok, f"GET /me/playlists -> {response.status_code}: {response.text}"
    page = response.json()
    dump(spike_dir, "me_playlists_page1", page)
    return page


@pytest.fixture(scope="session")
def calendar_playlist(my_playlists: dict[str, Any]) -> dict[str, Any]:
    """Una playlist del calendario: quella in SCH_LIVE_PLAYLIST (sottostringa) o la prima con un anno."""
    wanted = os.environ.get("SCH_LIVE_PLAYLIST", "").strip().lower()
    for playlist in my_playlists.get("items", []):
        name = playlist.get("name") or ""
        if wanted:
            if wanted in name.lower():
                return playlist
        elif re.search(r"\b20\d{2}\b", name):
            return playlist
    pytest.skip(
        "Nessuna playlist del calendario nella prima pagina: imposta SCH_LIVE_PLAYLIST='Marzo 2025'."
    )


@pytest.fixture(scope="session")
def playlist_entries(
    probe: Probe, calendar_playlist: dict[str, Any], spike_dir: Path
) -> list[dict[str, Any]]:
    response = probe.call(
        "GET", f"/playlists/{calendar_playlist['id']}/items", params={"limit": 50}
    )
    assert response.ok, f"GET /playlists/<id>/items -> {response.status_code}: {response.text}"
    page = response.json()
    dump(spike_dir, "playlist_items", page)
    return list(page.get("items", []))
