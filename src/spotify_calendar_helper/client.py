"""Client HTTP per l'API Spotify (task A2 e A3).

Implementa il Protocol `SpotifyClient` definito in `protocols.py`.
Tutto passa da questa classe: retry, rate limit, paginazione, batch.

Endpoint usati (post-Feb 2026):
  GET  /me
  GET  /me/playlists                (paginato, include snapshot_id)
  GET  /playlists/{id}/items        (paginato; chiave item > track per D3)
  POST /me/playlists
  POST /playlists/{id}/items        (batch da add_batch_size)
  DELETE /playlists/{id}/items
  PUT  /playlists/{id}
  GET  /artists/{id}                (singolo; batch rimossi nel 2024)

Errori tipizzati: `AuthError` (da auth.py), `RateLimitError`, `ApiError`.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import requests

from .auth import AuthError, ensure_token
from .config import Settings
from .models import ArtistInfo, ArtistRef, PlaylistInfo, PlaylistItems, Track, UserInfo

API = "https://api.spotify.com/v1"
_MAX_RETRIES = 5
_BACKOFF_BASE = 1.0
_BACKOFF_CAP = 30.0


class RateLimitError(Exception):
    """429 Too Many Requests: esauriti i tentativi."""


class ApiError(Exception):
    """Risposta 4xx/5xx non gestita dal retry."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


# ---------------------------------------------------------------------------
# Sessione HTTP con retry
# ---------------------------------------------------------------------------

class _Session:
    """Sessione requests con Bearer token, retry su 5xx e 429, timeout."""

    def __init__(
        self,
        token_getter: Callable[[], str],
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = _MAX_RETRIES,
    ) -> None:
        self._token_getter = token_getter
        self._sleep = sleep
        self._max_retries = max_retries
        self._session = requests.Session()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token_getter()}"}

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                resp = self._session.request(
                    method, url, headers=self._headers(), timeout=30, **kwargs
                )
            except requests.RequestException as exc:
                last_exc = exc
                self._sleep(min(_BACKOFF_BASE * (2 ** (attempt - 1)), _BACKOFF_CAP))
                continue

            if resp.status_code == 401:
                raise AuthError(
                    "Token non valido o scaduto. Esegui `sch auth` per accedere di nuovo."
                )
            if resp.status_code == 429:
                wait = _retry_after(resp, attempt)
                if attempt == self._max_retries:
                    raise RateLimitError(
                        f"Rate limit esaurito dopo {self._max_retries} tentativi."
                    )
                self._sleep(wait)
                continue
            if resp.status_code >= 500:
                self._sleep(min(_BACKOFF_BASE * (2 ** (attempt - 1)), _BACKOFF_CAP))
                continue
            return resp

        if last_exc is not None:
            raise ApiError(0, f"Errore di rete: {last_exc}")
        raise RateLimitError(f"Rate limit esaurito dopo {self._max_retries} tentativi.")

    def get(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", _url(path), **kwargs)

    def post(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", _url(path), **kwargs)

    def put(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("PUT", _url(path), **kwargs)

    def delete(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("DELETE", _url(path), **kwargs)


def _url(path: str) -> str:
    return path if path.startswith("http") else API + path


def _retry_after(resp: requests.Response, attempt: int) -> float:
    raw = resp.headers.get("Retry-After")
    try:
        return min(max(float(raw), 0.0), 60.0)
    except (TypeError, ValueError):
        return min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_CAP)


# ---------------------------------------------------------------------------
# Paginazione
# ---------------------------------------------------------------------------

def _paginate(session: _Session, first_page: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Itera sugli `items` di una risposta paginata di Spotify."""
    page = first_page
    while True:
        for item in page.get("items", []):
            yield item
        next_url = page.get("next")
        if not next_url:
            break
        resp = session.get(next_url)
        _check(resp)
        page = resp.json()


def _check(resp: requests.Response) -> None:
    if not resp.ok:
        try:
            msg = resp.json().get("error", {}).get("message", resp.text)
        except Exception:
            msg = resp.text
        raise ApiError(resp.status_code, msg)


# ---------------------------------------------------------------------------
# Parser delle risposte
# ---------------------------------------------------------------------------

def _parse_artist_ref(data: dict[str, Any]) -> ArtistRef:
    return ArtistRef(id=data.get("id") or "", name=data.get("name") or "")


def _parse_track(item: dict[str, Any]) -> Track | None:
    """Converte un elemento della pagina items in Track. Restituisce None se non è un brano."""
    # D3: supporta sia "item" (nuovi endpoint) che "track" (risposta legacy)
    inner = item.get("item") or item.get("track")
    if not inner or not isinstance(inner, dict):
        return None
    track_type = inner.get("type", "")
    if track_type not in ("track", ""):
        return None  # episodio o tipo sconosciuto
    track_id = inner.get("id")
    uri = inner.get("uri", "")
    if not track_id or not uri or uri.startswith("spotify:local:"):
        return None  # local file o item non disponibile
    artists = tuple(
        _parse_artist_ref(a)
        for a in inner.get("artists", [])
        if isinstance(a, dict) and a.get("id")
    )
    return Track(
        id=track_id,
        uri=uri,
        name=inner.get("name") or "",
        artists=artists,
        added_at=item.get("added_at"),
        album_name=(inner.get("album") or {}).get("name") or "",
        album_id=(inner.get("album") or {}).get("id") or "",
    )


def _parse_playlist_info(data: dict[str, Any], owner_id_fallback: str = "") -> PlaylistInfo:
    owner = data.get("owner") or {}
    return PlaylistInfo(
        id=data["id"],
        name=data.get("name") or "",
        description=data.get("description") or "",
        snapshot_id=data.get("snapshot_id"),
        owner_id=owner.get("id") or owner_id_fallback,
        public=data.get("public"),
        track_count=(data.get("tracks") or {}).get("total"),
    )


# ---------------------------------------------------------------------------
# SpotifyHttpClient
# ---------------------------------------------------------------------------

class SpotifyHttpClient:
    """Implementa il Protocol `SpotifyClient` usando l'API HTTP di Spotify."""

    def __init__(
        self,
        settings: Settings,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._http = _Session(
            token_getter=lambda: ensure_token(settings),
            sleep=sleep,
        )

    # --- SpotifyClient ---

    def current_user(self) -> UserInfo:
        resp = self._http.get("/me")
        _check(resp)
        data = resp.json()
        return UserInfo(id=data["id"], display_name=data.get("display_name") or "")

    def list_my_playlists(self) -> list[PlaylistInfo]:
        resp = self._http.get("/me/playlists", params={"limit": 50})
        _check(resp)
        result = []
        for item in _paginate(self._http, resp.json()):
            if not isinstance(item, dict) or not item.get("id"):
                continue
            result.append(_parse_playlist_info(item))
        return result

    def get_playlist_items(self, playlist_id: str) -> PlaylistItems:
        resp = self._http.get(
            f"/playlists/{playlist_id}/items",
            params={"limit": 50, "fields": "next,items(added_at,item,track)"},
        )
        _check(resp)
        tracks: list[Track] = []
        skipped = 0
        for item in _paginate(self._http, resp.json()):
            if not isinstance(item, dict):
                skipped += 1
                continue
            track = _parse_track(item)
            if track is None:
                skipped += 1
            else:
                tracks.append(track)
        return PlaylistItems(tracks=tuple(tracks), skipped=skipped)

    def create_playlist(
        self, name: str, description: str = "", public: bool = False
    ) -> PlaylistInfo:
        user = self.current_user()
        resp = self._http.post(
            "/me/playlists",
            json={"name": name, "description": description, "public": public},
        )
        _check(resp)
        data = resp.json()
        return _parse_playlist_info(data, owner_id_fallback=user.id)

    def add_items(self, playlist_id: str, uris: Sequence[str]) -> None:
        batch_size = max(1, self._settings.add_batch_size)
        for i in range(0, len(uris), batch_size):
            batch = list(uris[i: i + batch_size])
            resp = self._http.post(
                f"/playlists/{playlist_id}/items",
                json={"uris": batch},
            )
            _check(resp)

    def remove_items(self, playlist_id: str, uris: Sequence[str]) -> None:
        # DELETE /playlists/{id}/items accetta max 100 per richiesta
        _REMOVE_BATCH = 100
        for i in range(0, len(uris), _REMOVE_BATCH):
            batch = list(uris[i: i + _REMOVE_BATCH])
            resp = self._http.delete(
                f"/playlists/{playlist_id}/items",
                json={"tracks": [{"uri": u} for u in batch]},
            )
            _check(resp)

    def update_details(
        self,
        playlist_id: str,
        name: str | None = None,
        description: str | None = None,
    ) -> None:
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if not body:
            return
        resp = self._http.put(f"/playlists/{playlist_id}", json=body)
        _check(resp)

    def get_artist(self, artist_id: str) -> ArtistInfo:
        resp = self._http.get(f"/artists/{artist_id}")
        if resp.status_code == 404:
            raise ApiError(404, f"Artista {artist_id} non trovato.")
        _check(resp)
        data = resp.json()
        return ArtistInfo(
            id=data["id"],
            name=data.get("name") or "",
            genres=tuple(data.get("genres") or []),
        )

    def get_album_year(self, album_id: str) -> int | None:
        """Restituisce l'anno di uscita dell'album, o None se non disponibile/non trovato."""
        if not album_id:
            return None
        resp = self._http.get(f"/albums/{album_id}")
        if resp.status_code == 404:
            return None
        _check(resp)
        data = resp.json()
        date = data.get("release_date") or ""
        # Spotify restituisce "YYYY", "YYYY-MM" o "YYYY-MM-DD" a seconda della precisione
        try:
            return int(date[:4])
        except (ValueError, IndexError):
            return None
