"""Autenticazione PKCE con Spotify (task A1).

Flusso:
1. Genera code_verifier/challenge S256.
2. Apre il browser sulla pagina di autorizzazione Spotify.
3. Avvia un server HTTP minimale su 127.0.0.1 per ricevere il callback.
4. Scambia il codice con i token e li salva in `data/token.json` (permessi 0600).
5. Prima di ogni chiamata API, `ensure_token()` controlla la scadenza e fa il refresh.
   Se il refresh fallisce con `invalid_grant`, solleva `AuthError` con messaggio chiaro.

Il token viene salvato SOLO su `data/token.json`, mai altrove. Il file è ignorato da git.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from .config import Settings

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
SCOPES = (
    "playlist-read-private "
    "playlist-modify-private "
    "playlist-modify-public "
    "user-library-read"
)
LOGIN_TIMEOUT_S = 300
# Rinnova il token se scade entro questa finestra
_REFRESH_MARGIN = timedelta(minutes=5)


class AuthError(Exception):
    """Errore di autenticazione: il token non è disponibile o il refresh è fallito."""


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------

def _pkce_pair() -> tuple[str, str]:
    """Genera (code_verifier, code_challenge_S256)."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# ---------------------------------------------------------------------------
# Login nel browser
# ---------------------------------------------------------------------------

def login(settings: Settings) -> dict[str, Any]:
    """Esegue il flusso PKCE nel browser e salva i token. Restituisce il dizionario token."""
    verifier, challenge = _pkce_pair()
    state_param = secrets.token_urlsafe(16)

    parsed = urlparse(settings.redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8888
    callback_path = parsed.path or "/callback"

    auth_params = {
        "client_id": settings.client_id,
        "response_type": "code",
        "redirect_uri": settings.redirect_uri,
        "scope": SCOPES,
        "state": state_param,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }
    url = AUTH_URL + "?" + urlencode(auth_params)

    result: dict[str, str] = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if urlparse(self.path).path != callback_path:
                self.send_response(404)
                self.end_headers()
                return
            query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            result.update(query)
            body = "Login completato. Puoi chiudere questa scheda.".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            pass

    server = HTTPServer((host, port), _Handler)
    server.timeout = 1
    print(f"\nApro il browser per il login Spotify.")
    print(f"Se non si apre automaticamente, visita:\n  {url}\n")
    webbrowser.open(url)

    deadline = time.monotonic() + LOGIN_TIMEOUT_S
    try:
        while "code" not in result and "error" not in result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    if "error" in result:
        raise AuthError(f"Autorizzazione rifiutata: {result['error']}")
    if "code" not in result:
        raise AuthError("Login scaduto: nessuna risposta entro 5 minuti. Riprova con `sch auth`.")
    if result.get("state") != state_param:
        raise AuthError("Parametro `state` non valido: possibile attacco CSRF. Login interrotto.")

    token = _exchange_code(result["code"], verifier, settings)
    _save_token(token, settings.token_path)
    print("Login completato. Token salvato.")
    return token


# ---------------------------------------------------------------------------
# Scambio del codice e refresh
# ---------------------------------------------------------------------------

def _exchange_code(code: str, verifier: str, settings: Settings) -> dict[str, Any]:
    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.redirect_uri,
            "client_id": settings.client_id,
            "code_verifier": verifier,
        },
        timeout=30,
    )
    if not response.ok:
        raise AuthError(f"Scambio del codice fallito: {response.status_code} {response.text}")
    return _annotate(response.json())


def _refresh_token(refresh: str, client_id: str) -> dict[str, Any]:
    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": client_id,
        },
        timeout=30,
    )
    if not response.ok:
        if response.status_code == 400:
            data = {}
            try:
                data = response.json()
            except Exception:
                pass
            if data.get("error") == "invalid_grant":
                raise AuthError(
                    "Il refresh token è scaduto (di solito dopo 6 mesi di inattività). "
                    "Esegui `sch auth` per accedere di nuovo."
                )
        raise AuthError(f"Refresh del token fallito: {response.status_code} {response.text}")
    new = response.json()
    # Spotify può omettere il refresh_token nel rinnovo: si riusa quello vecchio
    if "refresh_token" not in new:
        new["refresh_token"] = refresh
    return _annotate(new)


def _annotate(token: dict[str, Any]) -> dict[str, Any]:
    """Aggiunge `expires_at` (timestamp UNIX) per sapere quando scade."""
    expires_in = int(token.get("expires_in", 3600))
    token["expires_at"] = int(time.time()) + expires_in
    return token


# ---------------------------------------------------------------------------
# Persistenza
# ---------------------------------------------------------------------------

def _save_token(token: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(token, indent=2), encoding="utf-8")
    os.chmod(path, 0o600)


def load_token(settings: Settings) -> dict[str, Any]:
    """Carica il token dal disco. Solleva `AuthError` se non trovato."""
    if not settings.token_path.exists():
        raise AuthError("Token non trovato. Esegui `sch auth` per accedere.")
    try:
        return json.loads(settings.token_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise AuthError(f"Token non leggibile: {exc}. Esegui `sch auth`.") from exc


def ensure_token(settings: Settings) -> str:
    """Restituisce un access token valido, facendo il refresh se necessario."""
    token = load_token(settings)
    expires_at = token.get("expires_at", 0)
    expires = datetime.fromtimestamp(expires_at, tz=timezone.utc)
    now = datetime.now(tz=timezone.utc)

    if now + _REFRESH_MARGIN < expires:
        return token["access_token"]  # ancora valido

    refresh = token.get("refresh_token")
    if not refresh:
        raise AuthError("Refresh token assente. Esegui `sch auth`.")

    new_token = _refresh_token(refresh, settings.client_id)
    _save_token(new_token, settings.token_path)
    return new_token["access_token"]
