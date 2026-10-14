"""Spike P0-5: sonda l'API reale di Spotify per chiudere le decisioni D1, D3, D4, D5 di AGENTS.md.

Sono test "osservativi": non asseriscono come DEVE comportarsi Spotify, registrano come SI comporta
(in `findings`) e falliscono solo se la chiamata stessa va male.

Uso (dalla radice del repo, con `.env` compilato):
    pytest -m live -s                                # D1, D3, D5 (sola lettura)
    SCH_LIVE_WRITE=1 pytest -m live -s               # in più D4: crea UNA playlist privata di prova
    SCH_LIVE_PLAYLIST="Marzo 2025" pytest -m live -s # sceglie quale playlist del calendario leggere

Le risposte grezze finiscono in data/spike/ (ignorata da git). Contengono dati personali:
per le fixture in tests/fixtures/ vanno anonimizzate a mano (task P0-6).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from tests.live.probe import Probe, dump, inner_of

pytestmark = pytest.mark.live

BATCH_SIZES = (100, 50, 25, 10, 5, 1)


def test_d5_snapshot_id_in_my_playlists(
    my_playlists: dict[str, Any], findings: dict[str, str]
) -> None:
    items = my_playlists.get("items", [])
    assert items, "GET /me/playlists non ha restituito playlist"
    present = all("snapshot_id" in playlist for playlist in items)
    print(f"\n  playlist nella prima pagina: {len(items)} (totale: {my_playlists.get('total')})")
    print(f"  chiavi di una playlist: {sorted(items[0].keys())}")
    findings["D5"] = f"snapshot_id in /me/playlists: {'SI' if present else 'NO'}"


def test_d3_playlist_items_shape(
    playlist_entries: list[dict[str, Any]], findings: dict[str, str]
) -> None:
    assert playlist_entries, "la playlist scelta è vuota"
    first = playlist_entries[0]
    print(f"\n  chiavi del primo elemento: {sorted(first.keys())}")
    key = "item" if "item" in first else "track" if "track" in first else None
    if key and isinstance(first.get(key), dict):
        print(f"  chiavi dentro `{key}`: {sorted(first[key].keys())}")
    seen = sorted({k for entry in playlist_entries for k in ("item", "track") if k in entry})
    nulls = sum(1 for entry in playlist_entries if not inner_of(entry))
    findings["D3"] = f"chiavi presenti negli elementi: {seen or 'nessuna'}; elementi nulli: {nulls}"


def test_d1_artist_genres(
    probe: Probe,
    playlist_entries: list[dict[str, Any]],
    spike_dir: Path,
    findings: dict[str, str],
) -> None:
    artist_ids: list[str] = []
    for entry in playlist_entries:
        for artist in inner_of(entry).get("artists") or []:
            artist_id = artist.get("id")
            if artist_id and artist_id not in artist_ids:
                artist_ids.append(artist_id)
    artist_ids = artist_ids[:10]
    assert artist_ids, "nessun artista trovato nei brani letti"

    artists: list[dict[str, Any]] = []
    for artist_id in artist_ids:
        response = probe.call("GET", f"/artists/{artist_id}")
        assert response.ok, f"GET /artists/<id> -> {response.status_code}: {response.text}"
        artists.append(response.json())
    dump(spike_dir, "artists", artists)

    with_key = sum(1 for a in artists if "genres" in a)
    populated = sum(1 for a in artists if a.get("genres"))
    for artist in artists:
        print(f"  {artist.get('name')!r}: genres={artist.get('genres')!r}")
    findings["D1"] = (
        f"genres popolato per {populated}/{len(artists)} artisti "
        f"(chiave presente in {with_key}/{len(artists)})"
    )


def test_d4_add_items_batch_limit(
    probe: Probe, playlist_entries: list[dict[str, Any]], findings: dict[str, str]
) -> None:
    if os.environ.get("SCH_LIVE_WRITE") != "1":
        findings["D4"] = "non testato (serve SCH_LIVE_WRITE=1)"
        pytest.skip("Scrive su Spotify: imposta SCH_LIVE_WRITE=1 per crearne la playlist di prova.")

    uri = next(
        (
            inner_of(e).get("uri")
            for e in playlist_entries
            if (inner_of(e).get("uri") or "").startswith("spotify:track:")
        ),
        None,
    )
    assert uri, "nessun URI di brano utilizzabile per il test"

    created = probe.call(
        "POST",
        "/me/playlists",
        json={
            "name": "sch-spike-ELIMINA-QUESTA",
            "public": False,
            "description": "playlist di prova dello spike, da cancellare a mano",
        },
    )
    assert created.ok, f"POST /me/playlists -> {created.status_code}: {created.text}"
    playlist = created.json()
    link = playlist.get("external_urls", {}).get("spotify", playlist["id"])
    print(f"\n  playlist di prova creata (cancellala a mano): {link}")

    accepted: int | None = None
    for size in BATCH_SIZES:
        response = probe.call(
            "POST", f"/playlists/{playlist['id']}/items", json={"uris": [uri] * size}
        )
        print(f"  batch da {size}: {response.status_code}")
        if response.ok:
            accepted = size
            break
        print(f"    {response.text[:200]}")

    findings["D4"] = (
        f"primo batch accettato tra {list(BATCH_SIZES)}: {accepted}"
        if accepted
        else "nessun batch accettato"
    )
    assert accepted, "Spotify ha rifiutato anche il batch da 1: vedi i messaggi sopra"
