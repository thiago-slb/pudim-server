import io
import json
import zipfile

from conftest import auth

from pudim_server import catalog
from pudim_server.mixes import affinity
from pudim_server.textutil import base_title, norm


def test_healthz(client):
    assert client.get("/healthz").get_json() == {"ok": True}


def test_requires_token(client):
    r = client.get("/api/player/me")
    assert r.status_code == 401
    assert "error" in r.get_json()
    assert client.get("/api/player/me", headers=auth("wrong")).status_code == 401


def test_me_and_display_name(client):
    r = client.get("/api/player/me", headers=auth())
    assert r.status_code == 200
    assert r.get_json()["name"] == "alice"
    r = client.post("/api/player/me", json={"display_name": "Alice A."}, headers=auth())
    assert r.get_json() == {"ok": True, "display_name": "Alice A."}
    assert client.get("/api/player/me", headers=auth()).get_json()["display_name"] == "Alice A."


def test_contacts_and_chat(client):
    contacts = client.get("/api/player/contacts", headers=auth()).get_json()["contacts"]
    assert [c["name"] for c in contacts] == ["bob"]
    bob = contacts[0]["id"]
    r = client.post(f"/api/player/chat/{bob}", json={"text": "hi"}, headers=auth())
    assert r.status_code == 200 and r.get_json()["message"]["text"] == "hi"
    chats = client.get("/api/player/chats", headers=auth("tok-bob")).get_json()
    assert chats["unread"] == 1
    msgs = client.get(f"/api/player/chat/{'a' * 32}", headers=auth("tok-bob")).get_json()["messages"]
    assert [m["text"] for m in msgs] == ["hi"]
    assert client.get("/api/player/chats", headers=auth("tok-bob")).get_json()["unread"] == 0
    assert client.post(f"/api/player/chat/{bob}", json={}, headers=auth()).status_code == 400


def test_search_marks_library(client, monkeypatch):
    def fake_dz(path, ttl=600):
        if path.startswith("/search/track"):
            return {"data": [{"id": 10, "title": "Song One", "duration": 200,
                              "artist": {"id": 5, "name": "Test Artist"}, "album": {"id": 7, "title": "First Album"}},
                             {"id": 11, "title": "Unknown", "duration": 100,
                              "artist": {"id": 6, "name": "Nobody"}, "album": {"id": 8, "title": "X"}}]}
        return {"data": []}
    monkeypatch.setattr(catalog, "dz", fake_dz)
    d = client.get("/api/player/search?q=song", headers=auth()).get_json()
    assert [t["jellyfin_id"] for t in d["tracks"]] == ["1" * 32, None]
    assert d["albums"] == [] and d["playlists"] == []


def test_jam_flow(client):
    r = client.post("/api/player/jam", json={"queue": [{"id": "x"}], "playing": True}, headers=auth())
    jam = r.get_json()["jam"]
    assert jam["is_host"] and jam["queue"] == [{"id": "x"}]
    r = client.post(f"/api/player/jam/{jam['id']}/join", headers=auth("tok-bob"))
    assert len(r.get_json()["jam"]["members"]) == 2
    client.post(f"/api/player/jam/{jam['id']}/add", json={"items": [{"id": "y"}]}, headers=auth("tok-bob"))
    r = client.post(f"/api/player/jam/{jam['id']}/state", json={"index": 0}, headers=auth())
    assert r.get_json()["jam"]["pending"][0]["id"] == "y"
    assert client.post(f"/api/player/jam/{jam['id']}/state", json={}, headers=auth("tok-bob")).status_code == 403


def test_podcast_progress_and_notices(client, app):
    assert client.post("/api/player/podcasts/progress", json={"guid": "bad"}, headers=auth()).status_code == 400
    r = client.post("/api/player/podcasts/progress", json={"guid": "0" * 20, "pid": "1", "pos": 5}, headers=auth())
    assert r.get_json() == {"ok": True}
    app.extensions["pudim"].notify("a" * 32, "Hello", "body", tag="radar-alice")
    d = client.get("/api/player/notices?since=0", headers=auth()).get_json()
    assert [n["title"] for n in d["notices"]] == ["Hello"]
    assert client.get("/api/player/notices?since=0", headers=auth("tok-bob")).get_json()["notices"] == []


def test_downloads_not_available(client):
    assert client.get("/api/player/jobs", headers=auth()).get_json()["jobs"] == []
    r = client.post("/api/player/download", json={"kind": "track", "deezer_id": 1}, headers=auth())
    assert r.status_code == 501 and "error" in r.get_json()
    assert client.get("/api/player/play/1", headers=auth()).status_code == 501
    feats = client.get("/api/player/server", headers=auth()).get_json()["features"]
    assert feats["downloads"] is False and feats["weekly_mix"] is False


def test_recap_spotify_import(client):
    hist = [{"ts": "2025-03-01T12:00:00Z", "ms_played": 120000, "master_metadata_track_name": "Song One",
             "master_metadata_album_artist_name": "Test Artist", "master_metadata_album_album_name": "First Album"},
            {"ts": "2025-03-01T12:05:00Z", "ms_played": 5000, "master_metadata_track_name": "Skipped",
             "master_metadata_album_artist_name": "Test Artist"}]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Spotify Extended Streaming History/Streaming_History_Audio_2025.json", json.dumps(hist))
    r = client.post("/api/player/recap/spotify", headers=auth(),
                    data={"file": (io.BytesIO(buf.getvalue()), "my_spotify_data.zip")})
    s = r.get_json()
    assert r.status_code == 200 and s["added"] == 1 and s["short"] == 1
    d = client.get("/api/player/recap?year=2025", headers=auth()).get_json()
    assert d["years"] == [2025] and d["recap"]["plays"] == 1
    assert d["recap"]["top_artists"][0]["name"] == "Test Artist"


def test_update_without_dir(client):
    assert client.get("/api/player/update", headers=auth()).get_json()["build"] == 0
    assert client.get("/api/player/update/apk", headers=auth()).status_code == 404


def test_text_helpers():
    assert norm("Ação!") == "acao"
    assert base_title("Song (Live) - Remastered") == ("song", True)
    assert affinity({"a": 4}, {"a": 4}) == 99
    assert affinity({"a": 1}, {"b": 1}) == 1
