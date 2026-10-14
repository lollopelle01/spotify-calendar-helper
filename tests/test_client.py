"""Test di client.py (task A2, A3, A4). Nessuna rete: HTTP mockato con unittest.mock.

Copre:
- _Session: retry su 5xx, retry + Retry-After su 429, RateLimitError dopo max tentativi,
  AuthError su 401, ApiError su 4xx non gestiti, errore di rete
- _paginate: una sola pagina, più pagine concatenate
- _parse_track: chiave item, chiave track (D3), null/local/episodio → None
- SpotifyHttpClient: tutti i metodi del Protocol SpotifyClient
- A4: conformità al Protocol SpotifyClient (stesse firme del fake)
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests

from spotify_calendar_helper.auth import AuthError
from spotify_calendar_helper.client import (
    ApiError,
    RateLimitError,
    SpotifyHttpClient,
    _Session,
    _parse_track,
    _paginate,
)
from spotify_calendar_helper.config import default_settings
from spotify_calendar_helper.protocols import SpotifyClient


# ---------------------------------------------------------------------------
# Fixture / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path):
    return default_settings(data_dir=tmp_path / "data")


def _resp(status: int, body: Any = None, headers: dict | None = None) -> MagicMock:
    r = MagicMock(spec=requests.Response)
    r.status_code = status
    r.ok = status < 400
    r.json.return_value = body or {}
    r.text = json.dumps(body or {})
    r.headers = headers or {}
    return r


def _session(responses: list, sleep_fn: Callable | None = None) -> _Session:
    """Crea una _Session con requests.Session.request mockato."""
    mock_req = MagicMock(side_effect=responses)
    session = _Session(token_getter=lambda: "tok", sleep=sleep_fn or (lambda _: None))
    session._session.request = mock_req
    return session


def _client(settings, responses: list, sleep_fn: Callable | None = None) -> SpotifyHttpClient:
    """Crea un SpotifyHttpClient con la sessione HTTP mockata e token finto (nessun file su disco)."""
    client = SpotifyHttpClient(settings, sleep=sleep_fn or (lambda _: None))
    # Sostituisce sia il token_getter (nessuna lettura del disco) che request (nessuna rete)
    client._http._token_getter = lambda: "fake-token"
    client._http._session.request = MagicMock(side_effect=responses)
    return client


# ---------------------------------------------------------------------------
# A4: conformità al Protocol SpotifyClient
# ---------------------------------------------------------------------------


def test_client_rispetta_il_protocol(settings):
    """SpotifyHttpClient deve soddisfare il Protocol SpotifyClient a runtime."""
    assert isinstance(SpotifyHttpClient(settings), SpotifyClient)


def test_client_ha_stesse_firme_del_fake(settings):
    """Verifica che i nomi dei metodi corrispondano al Protocol."""
    from tests.fakes import FakeSpotifyClient
    protocol_methods = {
        "current_user", "list_my_playlists", "get_playlist_items",
        "create_playlist", "add_items", "remove_items", "update_details", "get_artist",
    }
    real_methods = {m for m in dir(SpotifyHttpClient) if not m.startswith("_")}
    fake_methods = {m for m in dir(FakeSpotifyClient) if not m.startswith("_")}
    # tutti i metodi del Protocol devono esistere sia nel client reale che nel fake
    assert protocol_methods <= real_methods
    assert protocol_methods <= fake_methods


# ---------------------------------------------------------------------------
# _Session: retry
# ---------------------------------------------------------------------------


def test_session_successo_al_primo_tentativo():
    s = _session([_resp(200, {"ok": True})])
    resp = s.get("/me")
    assert resp.status_code == 200


def test_session_retry_su_500():
    s = _session([_resp(500), _resp(500), _resp(200, {"x": 1})])
    resp = s.get("/anything")
    assert resp.status_code == 200


def test_session_rate_limit_con_retry_after():
    slept: list[float] = []
    s = _session(
        [_resp(429, headers={"Retry-After": "2"}), _resp(200, {"ok": True})],
        sleep_fn=slept.append,
    )
    resp = s.get("/anything")
    assert resp.status_code == 200
    assert slept[0] == 2.0


def test_session_rate_limit_dopo_max_tentativi():
    s = _session([_resp(429)] * 10)
    with pytest.raises(RateLimitError):
        s.get("/anything")


def test_session_401_solleva_auth_error():
    s = _session([_resp(401)])
    with pytest.raises(AuthError):
        s.get("/me")


def test_session_404_restituisce_risposta_non_solleva():
    """La _Session restituisce le risposte 4xx senza eccezioni: è _check() a sollevare ApiError."""
    s = _session([_resp(404, {"error": {"message": "non trovato"}})])
    resp = s.get("/playlists/xxx")
    assert resp.status_code == 404
    # _check solleva ApiError
    from spotify_calendar_helper.client import _check
    with pytest.raises(ApiError) as exc_info:
        _check(resp)
    assert exc_info.value.status == 404


def test_session_errore_di_rete_con_retry():
    s = _session([
        requests.ConnectionError("rete giù"),
        requests.ConnectionError("rete giù"),
        _resp(200, {"ok": True}),
    ])
    resp = s.get("/me")
    assert resp.status_code == 200


def test_session_errore_di_rete_persistente_solleva_api_error():
    s = _session([requests.ConnectionError("sempre giù")] * 10)
    with pytest.raises(ApiError):
        s.get("/me")


# ---------------------------------------------------------------------------
# _paginate
# ---------------------------------------------------------------------------


def test_paginate_pagina_singola():
    first_page = {"items": [{"id": "a"}, {"id": "b"}], "next": None}
    items = list(_paginate(_session([]), first_page))
    assert [i["id"] for i in items] == ["a", "b"]


def test_paginate_piu_pagine():
    page2 = {"items": [{"id": "c"}], "next": None}
    session = _session([_resp(200, page2)])
    first_page = {"items": [{"id": "a"}, {"id": "b"}], "next": "https://api.spotify.com/v1/p2"}
    items = list(_paginate(session, first_page))
    assert [i["id"] for i in items] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# _parse_track (D3: chiavi item e track, tipi speciali)
# ---------------------------------------------------------------------------


def _item(inner: dict | None, *, key: str = "item", added_at: str = "2025-01-01T00:00:00Z") -> dict:
    return {key: inner, "added_at": added_at}


def _track_inner(track_id: str = "t1", uri: str | None = None, track_type: str = "track") -> dict:
    return {
        "id": track_id,
        "uri": uri or f"spotify:track:{track_id}",
        "name": f"Brano {track_id}",
        "type": track_type,
        "artists": [{"id": "a1", "name": "Artista"}],
        "album": {"name": "Album"},
    }


def test_parse_track_con_chiave_item():
    inner = _track_inner("t1")
    result = _parse_track(_item(inner, key="item"))
    assert result is not None
    assert result.id == "t1"


def test_parse_track_con_chiave_track_legacy():
    """D3: supporta anche la chiave legacy 'track'."""
    inner = _track_inner("t2")
    result = _parse_track(_item(inner, key="track"))
    assert result is not None
    assert result.id == "t2"


def test_parse_track_null_inner():
    assert _parse_track({"item": None, "added_at": "x"}) is None


def test_parse_track_episodio():
    inner = _track_inner("ep1", track_type="episode")
    assert _parse_track(_item(inner)) is None


def test_parse_track_local_file():
    inner = _track_inner("local1", uri="spotify:local:Artist:Album:Track:123")
    assert _parse_track(_item(inner)) is None


def test_parse_track_senza_id():
    inner = {**_track_inner("t1"), "id": None}
    assert _parse_track(_item(inner)) is None


def test_parse_track_artisti_multipli():
    inner = {
        "id": "t1",
        "uri": "spotify:track:t1",
        "name": "Multi",
        "type": "track",
        "artists": [{"id": "a1", "name": "Uno"}, {"id": "a2", "name": "Due"}],
        "album": {"name": "A"},
    }
    result = _parse_track(_item(inner))
    assert result is not None
    assert len(result.artists) == 2
    assert result.artists[0].name == "Uno"


# ---------------------------------------------------------------------------
# SpotifyHttpClient: metodi del Protocol
# ---------------------------------------------------------------------------


def test_current_user(settings):
    c = _client(settings, [_resp(200, {"id": "lorenzo", "display_name": "Lorenzo"})])
    user = c.current_user()
    assert user.id == "lorenzo"
    assert user.display_name == "Lorenzo"


def test_list_my_playlists(settings):
    body = {
        "items": [
            {"id": "p1", "name": "Marzo 2025", "description": "", "snapshot_id": "s1",
             "owner": {"id": "me"}, "public": False, "tracks": {"total": 10}},
        ],
        "next": None,
    }
    c = _client(settings, [_resp(200, body)])
    playlists = c.list_my_playlists()
    assert len(playlists) == 1
    assert playlists[0].id == "p1"
    assert playlists[0].snapshot_id == "s1"


def test_get_playlist_items_con_chiave_item(settings):
    inner = _track_inner("t1")
    body = {"items": [{"item": inner, "added_at": "2025-01-01T00:00:00Z"}], "next": None}
    c = _client(settings, [_resp(200, body)])
    result = c.get_playlist_items("p1")
    assert len(result.tracks) == 1
    assert result.tracks[0].id == "t1"
    assert result.skipped == 0


def test_get_playlist_items_salta_episodi(settings):
    ep = _track_inner("ep1", track_type="episode")
    track = _track_inner("t1")
    body = {
        "items": [
            {"item": ep, "added_at": "2025-01-01T00:00:00Z"},
            {"item": track, "added_at": "2025-01-01T00:00:00Z"},
        ],
        "next": None,
    }
    c = _client(settings, [_resp(200, body)])
    result = c.get_playlist_items("p1")
    assert len(result.tracks) == 1
    assert result.skipped == 1


def test_create_playlist(settings):
    # create_playlist chiama prima current_user, poi POST /users/{id}/playlists
    user_resp = _resp(200, {"id": "me", "display_name": "Lorenzo"})
    pl_resp = _resp(200, {
        "id": "new-pl", "name": "Artista - A", "description": "[sch:artist:a1]",
        "snapshot_id": "s1", "owner": {"id": "me"}, "public": False, "tracks": {"total": 0}
    })
    c = _client(settings, [user_resp, pl_resp])
    pl = c.create_playlist("Artista - A", "[sch:artist:a1]", public=False)
    assert pl.id == "new-pl"
    assert pl.owner_id == "me"
    assert pl.public is False


def test_add_items_in_batch(settings):
    """add_items deve spezzare in batch da add_batch_size e fare più chiamate POST."""
    # 25 brani con batch_size=10 → 3 chiamate (10, 10, 5)
    responses = [_resp(200, {"snapshot_id": f"s{i}"}) for i in range(3)]
    c = _client(settings, responses)
    uris = [f"spotify:track:{i}" for i in range(25)]
    c.add_items("p1", uris)
    # verifica numero di chiamate POST
    assert c._http._session.request.call_count == 3


def test_add_items_batch_size_da_settings(settings):
    """Il batch_size si legge da settings.add_batch_size."""
    settings.config["sync"]["add_batch_size"] = 5
    # 12 brani con batch=5 → 3 chiamate (5, 5, 2)
    responses = [_resp(200, {"snapshot_id": f"s{i}"}) for i in range(3)]
    c = _client(settings, responses)
    c.add_items("p1", [f"spotify:track:{i}" for i in range(12)])
    assert c._http._session.request.call_count == 3


def test_add_items_lista_vuota(settings):
    c = _client(settings, [])
    c.add_items("p1", [])
    assert c._http._session.request.call_count == 0


def test_remove_items(settings):
    """remove_items fa una chiamata DELETE con body {tracks: [{uri: ...}]}."""
    c = _client(settings, [_resp(200, {"snapshot_id": "s1"})])
    c.remove_items("p1", ["spotify:track:1", "spotify:track:2"])
    assert c._http._session.request.call_count == 1


def test_remove_items_usa_chiave_tracks(settings):
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return _resp(200, {"snapshot_id": "s1"})

    c = SpotifyHttpClient(settings, sleep=lambda _: None)
    c._http._token_getter = lambda: "fake-token"
    c._http._session.request = fake_request
    c.remove_items("p1", ["spotify:track:1"])
    assert "tracks" in captured
    assert captured["tracks"][0] == {"uri": "spotify:track:1"}


def test_update_details(settings):
    c = _client(settings, [_resp(200)])
    c.update_details("p1", description="nuova [sch:artist:a1]")
    assert c._http._session.request.call_count == 1


def test_update_details_senza_parametri_non_fa_chiamate(settings):
    c = _client(settings, [])
    c.update_details("p1")  # nessun parametro → nessuna chiamata
    assert c._http._session.request.call_count == 0


def test_get_artist_con_generi(settings):
    body = {"id": "a1", "name": "Radiohead", "genres": ["art rock", "alternative rock"]}
    c = _client(settings, [_resp(200, body)])
    artist = c.get_artist("a1")
    assert artist.id == "a1"
    assert "art rock" in artist.genres


def test_get_artist_generi_vuoti(settings):
    """D1: genres può essere vuoto o assente."""
    body = {"id": "a1", "name": "Artista Ignoto", "genres": []}
    c = _client(settings, [_resp(200, body)])
    artist = c.get_artist("a1")
    assert artist.genres == ()


def test_get_artist_non_trovato(settings):
    body = {"error": {"status": 404, "message": "non trovato"}}
    c = _client(settings, [_resp(404, body)])
    with pytest.raises(ApiError) as exc_info:
        c.get_artist("inesistente")
    assert exc_info.value.status == 404
