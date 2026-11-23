"""Shared state for all route modules: config, Jellyfin client, SQLite database, users, notices, auth."""
import json
import re
import sqlite3
import threading
import time
from functools import wraps

from flask import abort, current_app, g, jsonify, request

from .config import data_path
from .i18n import t
from .jellyfin import Jellyfin, TokenCache

TOKEN_RE = re.compile(r'Token="([^"]+)"')
PLAYLIST_ID = re.compile(r"[0-9a-f]{32}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (user_id TEXT PRIMARY KEY, display_name TEXT);
CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
    from_id TEXT NOT NULL, to_id TEXT NOT NULL, text TEXT, share TEXT, read INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS messages_pair ON messages(from_id, to_id, id);
CREATE TABLE IF NOT EXISTS collab (pid TEXT, user_id TEXT, owner_id TEXT, ts REAL, PRIMARY KEY (pid, user_id));
CREATE TABLE IF NOT EXISTS playlist_adds (pid TEXT, item_id TEXT, user_id TEXT, ts REAL, PRIMARY KEY (pid, item_id));
CREATE TABLE IF NOT EXISTS lastfm (user_id TEXT PRIMARY KEY, name TEXT, session TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS podcast_subs (user_id TEXT, pid TEXT, title TEXT, author TEXT, cover TEXT, added REAL,
    PRIMARY KEY (user_id, pid));
CREATE TABLE IF NOT EXISTS podcast_progress (user_id TEXT, guid TEXT, pid TEXT, pos REAL, dur REAL,
    done INTEGER DEFAULT 0, ts REAL, PRIMARY KEY (user_id, guid));
CREATE TABLE IF NOT EXISTS followed_artists (user_id TEXT, deezer_id INTEGER, name TEXT, cover TEXT, added REAL,
    PRIMARY KEY (user_id, deezer_id));
CREATE TABLE IF NOT EXISTS notices (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, user_id TEXT NOT NULL,
    title TEXT, body TEXT, tag TEXT);
CREATE INDEX IF NOT EXISTS notices_user ON notices(user_id, ts);
"""


class Server:
    def __init__(self, conf):
        self.conf = conf
        self.db_path = str(data_path(conf, "pudim.db"))
        self.tokens = TokenCache(conf["jellyfin_url"])
        self._users = {"t": 0, "list": []}
        self.lock = threading.Lock()
        c = self.db()
        try:
            c.executescript(SCHEMA)
        finally:
            c.close()

    # ---------------------------------------------------------------- helpers
    def t(self, key, **kw):
        return t(self.conf, key, **kw)

    def jf(self, user=None):
        if not self.conf.get("jellyfin_api_key"):
            raise RuntimeError("The server has no Jellyfin API key (jellyfin_api_key).")
        return Jellyfin.from_conf(self.conf, user)

    def db(self):
        c = sqlite3.connect(self.db_path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    def execute(self, sql, args=()):
        c = self.db()
        try:
            with c:
                return c.execute(sql, args).lastrowid
        finally:
            c.close()

    def query(self, sql, args=()):
        c = self.db()
        try:
            return c.execute(sql, args).fetchall()
        finally:
            c.close()

    # ---------------------------------------------------------------- accounts
    def all_users(self, fresh=False):
        if fresh or time.time() - self._users["t"] > 60:
            us = self.jf().users()
            self._users.update(t=time.time(), list=[
                {"id": u["Id"], "name": u.get("Name") or "", "image": u.get("PrimaryImageTag")} for u in us])
        return self._users["list"]

    def forget_users(self):
        self._users["t"] = 0

    def names(self):
        return {r["user_id"]: r["display_name"] for r in self.query("SELECT * FROM profiles") if r["display_name"]}

    def display_name(self, uid, fallback):
        return self.names().get(uid) or fallback

    def person(self, u, dn=None):
        dn = self.names() if dn is None else dn
        return {"id": u["id"], "name": u["name"], "display_name": dn.get(u["id"]) or u["name"],
                "image": u.get("image")}

    def person_of(self, uid):
        u = next((x for x in self.all_users() if x["id"] == uid), None)
        return self.person(u) if u else {"id": uid, "name": "", "display_name": self.t("someone"), "image": None}

    # ---------------------------------------------------------------- chat and notices
    def chat(self, frm, to, text, share=None):
        return self.execute("INSERT INTO messages (ts, from_id, to_id, text, share) VALUES (?, ?, ?, ?, ?)",
                            (time.time(), frm, to, text, json.dumps(share, ensure_ascii=False) if share else None))

    def notify(self, user_id, title, body="", tag=None):
        """A notice the app shows as a notification (polled through /api/player/notices)."""
        self.execute("INSERT INTO notices (ts, user_id, title, body, tag) VALUES (?,?,?,?,?)",
                     (time.time(), user_id, title, body, tag))
        self.execute("DELETE FROM notices WHERE user_id=? AND id NOT IN "
                     "(SELECT id FROM notices WHERE user_id=? ORDER BY id DESC LIMIT 100)", (user_id, user_id))


def server():
    return current_app.extensions["pudim"]


def fail(e, code=502):
    return jsonify(error=str(e)), code


def player_auth(f):
    """The app sends its own Jellyfin token; the server asks Jellyfin whose it is and puts it in ``g.user``."""
    @wraps(f)
    def wrapper(*a, **kw):
        m = TOKEN_RE.search(request.headers.get("Authorization") or "")
        tok = m.group(1) if m else (request.headers.get("X-Emby-Token") or request.args.get("api_key"))
        if not tok:
            abort(401)
        try:
            g.user = server().tokens.whoami(tok)
        except Exception:
            abort(401)
        return f(*a, **kw)
    return wrapper
