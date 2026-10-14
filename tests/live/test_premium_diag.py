"""Diagnostica Premium (P0-5 bloccato): capisce esattamente dove e perché arriva il 403.

Questi test NON fanno asserzioni sul comportamento atteso di Spotify: registrano quello che
l'API risponde e stampano una diagnosi leggibile. Falliscono solo se c'è un errore di rete
o il login stesso non funziona.

Uso:
    pytest -m live -s tests/live/test_premium_diag.py

I risultati finiscono in data/spike/premium_diag.json (ignorata da git, dati personali).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from tests.live.probe import Probe, dump

pytestmark = pytest.mark.live


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _log(label: str, response: Any) -> dict[str, Any]:
    """Stampa status, headers rilevanti e body; restituisce un dict serializzabile."""
    headers = dict(response.headers)
    relevant = {
        k: v for k, v in headers.items()
        if k.lower() in {
            "www-authenticate", "retry-after", "content-type",
            "x-robots-tag", "x-error-code", "x-error",
        }
    }
    try:
        body = response.json()
    except Exception:
        body = response.text[:500]

    print(f"\n  [{label}]  HTTP {response.status_code}")
    if relevant:
        print(f"  headers rilevanti: {relevant}")
    print(f"  body: {json.dumps(body, ensure_ascii=False, indent=2)[:600]}")

    return {
        "label": label,
        "status": response.status_code,
        "headers": relevant,
        "body": body,
    }


# ---------------------------------------------------------------------------
# Test 1 — /me: chi sei e che tipo di account hai
# ---------------------------------------------------------------------------

def test_diag_01_me(probe: Probe, spike_dir: Path, findings: dict[str, str]) -> None:
    """Chiama GET /me e registra product, country e il tuo display_name.

    Questo è il primo posto dove Spotify potrebbe rispondere 403 se l'account
    non è riconosciuto come Premium dall'app.
    """
    resp = probe.call("GET", "/me")
    result = _log("GET /me", resp)
    dump(spike_dir, "diag_me", result)

    if resp.ok:
        data = resp.json()
        product = data.get("product", "ASSENTE")
        country = data.get("country", "?")
        name = data.get("display_name", "?")
        print(f"\n  display_name : {name}")
        print(f"  product      : {product}  ← deve essere 'premium' per le app in Dev Mode")
        print(f"  country      : {country}")
        findings["DIAG_me"] = f"OK — product={product!r}, country={country!r}"
    else:
        findings["DIAG_me"] = f"ERRORE {resp.status_code} — vedi diag_me.json"


# ---------------------------------------------------------------------------
# Test 2 — /me/playlists: lettura (scope read-only)
# ---------------------------------------------------------------------------

def test_diag_02_read_playlists(probe: Probe, spike_dir: Path, findings: dict[str, str]) -> None:
    """GET /me/playlists: verifica che la lettura delle playlist funzioni.

    Se /me è andato bene ma questo dà 403, il problema è nello scope
    playlist-read-private o nell'account non premium.
    """
    resp = probe.call("GET", "/me/playlists", params={"limit": 1})
    result = _log("GET /me/playlists", resp)
    dump(spike_dir, "diag_playlists_read", result)

    if resp.ok:
        total = resp.json().get("total", "?")
        findings["DIAG_playlists_read"] = f"OK — totale playlist: {total}"
    else:
        findings["DIAG_playlists_read"] = f"ERRORE {resp.status_code}"


# ---------------------------------------------------------------------------
# Test 3 — POST /me/playlists: scrittura (scope modify)
# ---------------------------------------------------------------------------

def test_diag_03_create_playlist(probe: Probe, spike_dir: Path, findings: dict[str, str]) -> None:
    """POST /me/playlists: crea una playlist privata di prova.

    Questo è l'endpoint che più spesso dà 403 agli account non-Premium.
    Se fallisce qui ma non nei test precedenti, il blocco è sulla scrittura.

    La playlist creata si chiama 'sch-diag-ELIMINA-QUESTA': cancellala a mano.
    """
    resp = probe.call(
        "POST",
        "/me/playlists",
        json={
            "name": "sch-diag-ELIMINA-QUESTA",
            "public": False,
            "description": "playlist di diagnostica, cancella a mano",
        },
    )
    result = _log("POST /me/playlists", resp)
    dump(spike_dir, "diag_create_playlist", result)

    if resp.ok:
        data = resp.json()
        link = data.get("external_urls", {}).get("spotify", data.get("id", "?"))
        print(f"\n  ✓ playlist creata: {link}")
        print("  ⚠ RICORDA DI CANCELLARLA A MANO in Spotify")
        findings["DIAG_create"] = f"OK — id={data.get('id')}"
        # Salva l'id per il test successivo (aggiunta brani)
        probe._diag_playlist_id = data.get("id")  # type: ignore[attr-defined]
    else:
        findings["DIAG_create"] = f"ERRORE {resp.status_code}"
        probe._diag_playlist_id = None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Test 4 — POST /playlists/{id}/items: aggiunta brani (solo se test 3 ok)
# ---------------------------------------------------------------------------

def test_diag_04_add_items(probe: Probe, spike_dir: Path, findings: dict[str, str]) -> None:
    """POST /playlists/{id}/items: aggiunge un brano di prova alla playlist creata nel test 3.

    Testa se il blocco è sulla creazione o sull'aggiunta. Il brano usato è
    'Never Gonna Give You Up' di Rick Astley (URI pubblicamente noto).
    """
    playlist_id = getattr(probe, "_diag_playlist_id", None)
    if not playlist_id:
        pytest.skip("Test 3 non ha creato la playlist: skip.")

    test_uri = "spotify:track:4uLU6hMCjMI75M1A2tKUQC"  # Never Gonna Give You Up
    resp = probe.call(
        "POST",
        f"/playlists/{playlist_id}/items",
        json={"uris": [test_uri]},
    )
    result = _log(f"POST /playlists/{playlist_id}/items", resp)
    dump(spike_dir, "diag_add_items", result)

    if resp.ok:
        findings["DIAG_add_items"] = "OK"
    else:
        findings["DIAG_add_items"] = f"ERRORE {resp.status_code}"


# ---------------------------------------------------------------------------
# Test 5 — GET /artists/{id}: recupero generi (D1, solo lettura)
# ---------------------------------------------------------------------------

def test_diag_05_get_artist(probe: Probe, spike_dir: Path, findings: dict[str, str]) -> None:
    """GET /artists/{id}: recupera i generi di un artista noto.

    Usa l'ID di Radiohead (noto, stabile). Non dipende dal Premium: è un endpoint
    pubblico, ma post-febbraio 2026 potrebbe aver cambiato comportamento.
    """
    radiohead_id = "4Z8W4fKeB5YxbusRsdQVPb"
    resp = probe.call("GET", f"/artists/{radiohead_id}")
    result = _log(f"GET /artists/{radiohead_id}", resp)
    dump(spike_dir, "diag_artist", result)

    if resp.ok:
        data = resp.json()
        genres = data.get("genres", "CHIAVE_ASSENTE")
        print(f"\n  Radiohead genres: {genres}")
        findings["DIAG_artist"] = (
            f"OK — genres={'popolato' if genres else 'vuoto o assente'}: {genres!r}"
        )
    else:
        findings["DIAG_artist"] = f"ERRORE {resp.status_code}"


# ---------------------------------------------------------------------------
# Test 6 — Stampa diagnosi finale
# ---------------------------------------------------------------------------

def test_diag_06_summary(spike_dir: Path, findings: dict[str, str]) -> None:
    """Raccoglie tutti i risultati DIAG_* e stampa una diagnosi leggibile.

    Non fa nulla di nuovo: serve solo a dare un riepilogo chiaro alla fine.
    """
    diag_keys = [k for k in findings if k.startswith("DIAG_")]
    errori = [k for k in diag_keys if "ERRORE" in findings[k]]
    ok = [k for k in diag_keys if k not in errori]

    print("\n" + "=" * 60)
    print("DIAGNOSI PREMIUM")
    print("=" * 60)
    for k in diag_keys:
        simbolo = "✓" if k in ok else "✗"
        print(f"  {simbolo}  {k}: {findings[k]}")

    if not errori:
        print("\n  Tutto OK: l'account è riconosciuto come Premium dall'API.")
        print("  Puoi procedere con lo spike completo (pytest -m live -s tests/live/test_spike.py).")
    else:
        print(f"\n  Blocco rilevato in: {', '.join(errori)}")
        if "DIAG_me" in errori:
            print("  → Il login è andato a buon fine ma GET /me dà errore.")
            print("    Controlla che il tuo account sia in User Management dell'app Spotify.")
        elif "DIAG_create" in errori and "DIAG_me" not in errori:
            print("  → Lettura OK, scrittura KO.")
            print("    Il blocco è su playlist-modify-private: tipico di account non-Premium.")
            print("    Verifica il campo 'product' in data/spike/diag_me.json.")
        elif "DIAG_playlists_read" in errori:
            print("  → Anche la lettura delle playlist è bloccata.")
            print("    Controlla che lo scope playlist-read-private sia stato concesso durante il login.")

    all_results = {k: findings[k] for k in diag_keys}
    dump(spike_dir, "premium_diag_summary", all_results)
