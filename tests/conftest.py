"""A fake Jellyfin on a local port, and a configured app pointing at it."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest

TOKENS = {"tok-alice": {"Id": "a" * 32, "Name": "alice"}, "tok-bob": {"Id": "b" * 32, "Name": "bob"}}
SERVER_KEY = "test-server-key"
LIBRARY = [
    {"Id": "1" * 32, "Name": "Song One", "Artists": ["Test Artist"], "AlbumArtist": "Test Artist",
     "Album": "First Album", "RunTimeTicks": 200 * 10 ** 7},
    {"Id": "2" * 32, "Name": "Song Two (Live)", "Artists": ["Other Band"], "AlbumArtist": "Other Band",
     "Album": "Live Record", "RunTimeTicks": 180 * 10 ** 7},
]


class FakeJellyfin(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _token(self):
        auth = self.headers.get("Authorization") or ""
        return auth.split('Token="', 1)[1].rstrip('"') if 'Token="' in auth else None

    def _send(self, code, body=None):
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path, tok = urlparse(self.path).path, self._token()
        if path == "/Users/Me":
            return self._send(200, TOKENS[tok]) if tok in TOKENS else self._send(401)
        if tok != SERVER_KEY:
            return self._send(401)
        if path == "/Users":
            return self._send(200, [dict(u, Policy={"IsAdministrator": u["Name"] == "alice"})
                                    for u in TOKENS.values()])
        if path == "/Items" and "IncludeItemTypes=Audio" in self.path:
            return self._send(200, {"Items": LIBRARY})
        if path in ("/Items", "/Artists/AlbumArtists"):
            return self._send(200, {"Items": []})
        return self._send(404)

    def do_POST(self):
        return self._send(204)


@pytest.fixture(scope="session")
def jellyfin():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeJellyfin)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def app(jellyfin, tmp_path, monkeypatch):
    monkeypatch.delenv("PUDIM_CONFIG", raising=False)
    from pudim_server import catalog, create_app
    catalog.reset_indexes()
    a = create_app(config_path=tmp_path / "missing.json", overrides={
        "jellyfin_url": jellyfin, "jellyfin_api_key": SERVER_KEY, "data_dir": str(tmp_path / "data"),
        "background_jobs": False})
    a.testing = True
    return a


@pytest.fixture
def client(app):
    return app.test_client()


def auth(tok="tok-alice"):
    return {"Authorization": f'MediaBrowser Client="Pudim", Token="{tok}"'}
