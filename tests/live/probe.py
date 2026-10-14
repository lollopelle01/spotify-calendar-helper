"""Supporto per i test live (spike P0-5): login PKCE minimale e client HTTP grezzo.

NON è il client del progetto: quello è `client.py` (task A2/A3, Fase 1). Questo modulo serve solo
a sondare l'API reale e a salvare le risposte grezze; resta confinato in `tests/live/`.
Il token vive solo in memoria e non viene mai scritto su disco.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests

API = "https://api.spotify.com/v1"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
SCOPES = "playlist-read-private playlist-modify-private"
LOGIN_TIMEOUT_S = 300


class ProbeError(RuntimeError):
    """Errore durante il login o una chiamata di sondaggio."""


def login(client_id: str, redirect_uri: str) -> str:
    """Login PKCE nel browser. Restituisce l'access token (solo in memoria)."""
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    state = secrets.token_urlsafe(16)
    url = AUTH_URL + "?" + urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "state": state,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
    )
    parsed = urlparse(redirect_uri)
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if urlparse(self.path).path != parsed.path:
                self.send_response(404)
                self.end_headers()
                return
            query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            result.update(query)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Login completato, puoi chiudere questa scheda.".encode())

        def log_message(self, *_: object) -> None:
            pass

    server = HTTPServer((parsed.hostname or "127.0.0.1", parsed.port or 8888), Handler)
    server.timeout = 1
    print(f"\nApro il browser per il login. Se non si apre, usa questo URL:\n{url}\n")
    webbrowser.open(url)
    deadline = time.monotonic() + LOGIN_TIMEOUT_S
    try:
        while "code" not in result and "error" not in result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    if "error" in result:
        raise ProbeError(f"Login rifiutato: {result['error']}")
    if "code" not in result:
        raise ProbeError("Login scaduto: nessuna risposta dal browser entro 5 minuti.")
    if result.get("state") != state:
        raise ProbeError("Parametro state non valido: login interrotto.")

    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        },
        timeout=30,
    )
    if not response.ok:
        raise ProbeError(f"Scambio del token fallito: {response.status_code} {response.text}")
    return response.json()["access_token"]


class Probe:
    """Client HTTP grezzo con gestione minima del 429."""

    def __init__(self, token: str) -> None:
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {token}"
        self.saw_429 = False
        self.retry_after_header_present: bool | None = None

    def call(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        url = path if path.startswith("http") else API + path
        response = self._session.request(method, url, timeout=30, **kwargs)
        for _ in range(4):
            if response.status_code != 429:
                break
            self.saw_429 = True
            self.retry_after_header_present = "Retry-After" in response.headers
            wait = int(response.headers.get("Retry-After", "2"))
            print(f"  429 ricevuto, Retry-After={response.headers.get('Retry-After')!r}: attendo {wait + 1}s")
            time.sleep(wait + 1)
            response = self._session.request(method, url, timeout=30, **kwargs)
        return response


def dump(directory: Path, name: str, data: object) -> Path:
    """Salva una risposta grezza (dati personali: la cartella sta in data/, ignorata da git)."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def inner_of(entry: dict[str, Any]) -> dict[str, Any]:
    """Il brano di un elemento di playlist, qualunque sia la chiave (`item` o `track`)."""
    return entry.get("item") or entry.get("track") or {}
