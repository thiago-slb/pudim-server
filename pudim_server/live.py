"""Jam (listen together), collaborative playlists and Last.fm scrobbling.

- Jam: the host's app pushes its state (queue, track, position, playing) and guests mirror it. Guests can add
  songs and send commands (next, previous, pause, jump) that the host's app applies. Kept in memory only; it ends
  when the host leaves or is gone for 90 s.
- Collaborative playlist: the owner's app grants edit rights in Jellyfin itself (only the owner's token can) and
  tells this server, which records members, who added each song, and sends the invite in the chat.
- Last.fm: ``lastfm_api_key`` / ``lastfm_api_secret`` in the config; each account's session key is stored in the
  database. The app reports "now playing" and scrobbles; the server signs and forwards them.
"""
import hashlib
import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from flask import Blueprint, abort, g, jsonify, request

from .core import PLAYLIST_ID, fail, player_auth, server
from .jellyfin import UA

bp = Blueprint("live", __name__)
LASTFM = "https://ws.audioscrobbler.com/2.0/"
_jams = {}
_jlock = threading.Lock()
MAX_ITEM = 4000


def _clean_items(items, n=300):
    out = []
    for it in (items or [])[:n]:
        if isinstance(it, dict) and isinstance(it.get("id"), str):
            if len(json.dumps(it, ensure_ascii=False)) <= MAX_ITEM:
                out.append(it)
    return out


# ================================================================== Jam
def _gc():
    now = time.time()
    for jid, j in list(_jams.items()):
        if j["ended"] and now - j["ended"] > 600:
            _jams.pop(jid, None)
            continue
        if not j["ended"] and now - j["members"].get(j["host"], {}).get("seen", 0) > 90:
            j["ended"] = now
            j["rev"] += 1
        for uid, m in list(j["members"].items()):
            if uid != j["host"] and now - m["seen"] > 60:
                j["members"].pop(uid, None)
                j["rev"] += 1


def _mine(uid):
    return next((j for j in _jams.values() if not j["ended"] and uid in j["members"]), None)


def _view(j, me, rev=None):
    srv = server()
    now = time.time()
    pos = j["pos_ms"] + (int((now - j["at"]) * 1000) if j["playing"] else 0)
    out = {"id": j["id"], "host": j["host"], "is_host": me == j["host"], "rev": j["rev"], "index": j["index"],
           "pos_ms": pos, "playing": j["playing"], "context": j["context"], "ended": bool(j["ended"]),
           "members": [dict(srv.person_of(uid), host=uid == j["host"]) for uid in j["members"]]}
    if rev is None or str(rev) != str(j["rev"]):
        out["queue"] = j["queue"]
    if me == j["host"]:
        out["pending"], j["pending"] = j["pending"], []
        out["cmds"], j["cmds"] = j["cmds"], []
    return out


def _get(jid):
    j = _jams.get(jid)
    if not j:
        abort(404)
    return j


@bp.route("/api/player/jam")
@player_auth
def jam_mine():
    with _jlock:
        _gc()
        j = _mine(g.user["id"])
        if j:
            j["members"][g.user["id"]]["seen"] = time.time()
        return jsonify(jam=_view(j, g.user["id"], request.args.get("rev")) if j else None)


@bp.route("/api/player/jam", methods=["POST"])
@player_auth
def jam_create():
    me = g.user["id"]
    b = request.get_json(silent=True) or {}
    with _jlock:
        _gc()
        old = _mine(me)
        if old:
            if old["host"] == me:
                old["ended"] = time.time()
                old["rev"] += 1
            else:
                old["members"].pop(me, None)
        jid = secrets.token_urlsafe(8)
        j = {"id": jid, "host": me, "members": {me: {"seen": time.time()}}, "queue": _clean_items(b.get("queue")),
             "index": int(b.get("index") or 0), "pos_ms": int(b.get("pos_ms") or 0), "at": time.time(),
             "playing": bool(b.get("playing")), "context": str(b.get("context") or "")[:200], "rev": 1,
             "pending": [], "cmds": [], "ended": 0, "created": time.time()}
        _jams[jid] = j
        return jsonify(jam=_view(j, me))


@bp.route("/api/player/jam/<jid>/join", methods=["POST"])
@player_auth
def jam_join(jid):
    me = g.user["id"]
    with _jlock:
        _gc()
        j = _get(jid)
        if j["ended"]:
            return fail("That Jam has ended.", 410)
        if len(j["members"]) >= 10 and me not in j["members"]:
            return fail("The Jam is full.", 409)
        old = _mine(me)
        if old and old is not j:
            if old["host"] == me:
                old["ended"] = time.time()
            old["members"].pop(me, None)
            old["rev"] += 1
        j["members"][me] = {"seen": time.time()}
        j["rev"] += 1
        return jsonify(jam=_view(j, me))


@bp.route("/api/player/jam/<jid>/leave", methods=["POST"])
@player_auth
def jam_leave(jid):
    me = g.user["id"]
    with _jlock:
        j = _get(jid)
        if j["host"] == me:
            j["ended"] = time.time()
        j["members"].pop(me, None)
        j["rev"] += 1
    return jsonify(ok=True)


@bp.route("/api/player/jam/<jid>/state", methods=["POST"])
@player_auth
def jam_state(jid):
    me = g.user["id"]
    b = request.get_json(silent=True) or {}
    with _jlock:
        _gc()
        j = _get(jid)
        if j["host"] != me:
            return fail("Only the host controls the queue.", 403)
        if j["ended"]:
            return fail("That Jam has ended.", 410)
        changed = False
        if "queue" in b:
            j["queue"] = _clean_items(b.get("queue"))
            changed = True
        for k, conv in (("index", int), ("playing", bool)):
            if k in b and conv(b[k]) != j[k]:
                j[k] = conv(b[k])
                changed = True
        if "context" in b:
            j["context"] = str(b.get("context") or "")[:200]
        if "pos_ms" in b:
            newpos = int(b.get("pos_ms") or 0)
            expect = j["pos_ms"] + (int((time.time() - j["at"]) * 1000) if j["playing"] else 0)
            if abs(newpos - expect) > 2500:  # a seek: guests need to know
                changed = True
            j["pos_ms"], j["at"] = newpos, time.time()
        if changed:
            j["rev"] += 1
        j["members"][me]["seen"] = time.time()
        return jsonify(jam=_view(j, me, j["rev"]))


@bp.route("/api/player/jam/<jid>/add", methods=["POST"])
@player_auth
def jam_add(jid):
    items = _clean_items((request.get_json(silent=True) or {}).get("items"), 50)
    if not items:
        return fail("Nothing to add.", 400)
    with _jlock:
        j = _get(jid)
        if g.user["id"] not in j["members"] or j["ended"]:
            return fail("You are not in that Jam.", 403)
        for it in items:
            it["jam_by"] = g.user["id"]
        j["pending"] = (j["pending"] + items)[-200:]
    return jsonify(ok=True)


@bp.route("/api/player/jam/<jid>/cmd", methods=["POST"])
@player_auth
def jam_cmd(jid):
    b = request.get_json(silent=True) or {}
    act = b.get("action")
    if act not in ("next", "previous", "toggle", "jump"):
        return fail("Unknown command.", 400)
    with _jlock:
        j = _get(jid)
        if g.user["id"] not in j["members"] or j["ended"]:
            return fail("You are not in that Jam.", 403)
        j["cmds"] = (j["cmds"] + [{"action": act, "index": b.get("index"), "by": g.user["id"]}])[-20:]
    return jsonify(ok=True)


@bp.route("/api/player/jam/<jid>/invite/<uid>", methods=["POST"])
@player_auth
def jam_invite(jid, uid):
    srv = server()
    with _jlock:
        j = _get(jid)
        if g.user["id"] not in j["members"] or j["ended"]:
            return fail("You are not in that Jam.", 403)
    me = srv.person_of(g.user["id"])
    srv.chat(g.user["id"], uid, srv.t("jam_invite"),
             {"kind": "jam", "id": jid, "title": srv.t("jam_title", name=me["display_name"]),
              "subtitle": srv.t("jam_tap"), "cover": ""})
    return jsonify(ok=True)


# ================================================================== collaborative playlists
@bp.route("/api/player/collab/<pid>")
@player_auth
def collab_get(pid):
    if not PLAYLIST_ID.fullmatch(pid):
        abort(404)
    srv = server()
    rows = srv.query("SELECT user_id, owner_id FROM collab WHERE pid=?", (pid,))
    adds = {r["item_id"]: r["user_id"] for r in srv.query(
        "SELECT item_id, user_id FROM playlist_adds WHERE pid=?", (pid,))}
    owner = rows[0]["owner_id"] if rows else None
    members = ([owner] if owner else []) + [r["user_id"] for r in rows]
    return jsonify(owner=owner, members=[srv.person_of(u) for u in dict.fromkeys(members)],
                   added=adds if rows else {})


@bp.route("/api/player/collab/<pid>/invite/<uid>", methods=["POST"])
@player_auth
def collab_invite(pid, uid):
    if not PLAYLIST_ID.fullmatch(pid) or uid == g.user["id"]:
        return fail("Invalid invite.", 400)
    srv = server()
    title = str((request.get_json(silent=True) or {}).get("title") or "Playlist")[:200]
    srv.execute("INSERT OR REPLACE INTO collab VALUES (?,?,?,?)", (pid, uid, g.user["id"], time.time()))
    srv.chat(g.user["id"], uid, srv.t("collab_invite", title=title),
             {"kind": "playlist", "id": pid, "title": title, "subtitle": srv.t("collab_subtitle"), "cover": ""})
    return jsonify(ok=True)


@bp.route("/api/player/collab/<pid>/member/<uid>", methods=["DELETE"])
@player_auth
def collab_remove(pid, uid):
    server().execute("DELETE FROM collab WHERE pid=? AND user_id=? AND (owner_id=? OR user_id=?)",
                     (pid, uid, g.user["id"], g.user["id"]))
    return jsonify(ok=True)


@bp.route("/api/player/collab/<pid>/added", methods=["POST"])
@player_auth
def collab_added(pid):
    if not PLAYLIST_ID.fullmatch(pid):
        abort(404)
    ids = [i for i in (request.get_json(silent=True) or {}).get("ids") or [] if isinstance(i, str)][:500]
    c = server().db()
    try:
        with c:
            c.executemany("INSERT OR REPLACE INTO playlist_adds VALUES (?,?,?,?)",
                          [(pid, i, g.user["id"], time.time()) for i in ids])
    finally:
        c.close()
    return jsonify(ok=True)


# ================================================================== Last.fm
def _sig(params, secret):
    raw = "".join(f"{k}{params[k]}" for k in sorted(params) if k not in ("format", "callback")) + secret
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def lastfm_call(conf, method, params, post=False):
    key, secret = conf.get("lastfm_api_key"), conf.get("lastfm_api_secret")
    if not key or not secret:
        raise RuntimeError("Last.fm is not configured on this server.")
    p = {k: str(v) for k, v in params.items() if v not in (None, "")}
    p.update(method=method, api_key=key)
    p["api_sig"] = _sig(p, secret)
    p["format"] = "json"
    data = urllib.parse.urlencode(p).encode()
    req = urllib.request.Request(LASTFM if post else f"{LASTFM}?{data.decode()}", data=data if post else None,
                                 headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read())
        except ValueError:
            raise RuntimeError(f"Last.fm: HTTP {e.code}")
    if isinstance(d, dict) and d.get("error"):
        raise RuntimeError(f"Last.fm: {d.get('message') or d['error']}")
    return d


def _lf_row(uid):
    rows = server().query("SELECT * FROM lastfm WHERE user_id=?", (uid,))
    return rows[0] if rows else None


@bp.route("/api/player/lastfm")
@player_auth
def lastfm_status():
    conf = server().conf
    r = _lf_row(g.user["id"])
    return jsonify(configured=bool(conf.get("lastfm_api_key") and conf.get("lastfm_api_secret")),
                   connected=bool(r and r["session"]), name=r["name"] if r else None)


@bp.route("/api/player/lastfm/connect", methods=["POST"])
@player_auth
def lastfm_connect():
    conf = server().conf
    try:
        tok = lastfm_call(conf, "auth.getToken", {})["token"]
    except Exception as e:
        return fail(e, 400)
    url = "https://www.last.fm/api/auth/?" + urllib.parse.urlencode({"api_key": conf["lastfm_api_key"], "token": tok})
    return jsonify(token=tok, url=url)


@bp.route("/api/player/lastfm/finish", methods=["POST"])
@player_auth
def lastfm_finish():
    tok = str((request.get_json(silent=True) or {}).get("token") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", tok):
        return fail("Invalid token.", 400)
    try:
        s = lastfm_call(server().conf, "auth.getSession", {"token": tok})["session"]
    except Exception as e:
        return fail(f"{e}. Did you authorise it on the Last.fm page?", 400)
    server().execute("INSERT OR REPLACE INTO lastfm VALUES (?,?,?,?)",
                     (g.user["id"], s.get("name"), s["key"], time.time()))
    return jsonify(connected=True, name=s.get("name"))


@bp.route("/api/player/lastfm", methods=["DELETE"])
@player_auth
def lastfm_disconnect():
    server().execute("DELETE FROM lastfm WHERE user_id=?", (g.user["id"],))
    return jsonify(ok=True)


@bp.route("/api/player/scrobble", methods=["POST"])
@player_auth
def scrobble():
    r = _lf_row(g.user["id"])
    if not r or not r["session"]:
        return jsonify(ok=False, connected=False)
    b = request.get_json(silent=True) or {}
    artist, track = str(b.get("artist") or "")[:300], str(b.get("track") or "")[:300]
    if not artist or not track:
        return fail("Missing artist or track.", 400)
    p = {"artist": artist, "track": track, "album": str(b.get("album") or "")[:300], "sk": r["session"]}
    dur = int(b.get("duration") or 0)
    if dur > 0:
        p["duration"] = dur
    try:
        if b.get("now_playing"):
            lastfm_call(server().conf, "track.updateNowPlaying", p, post=True)
        else:
            p["timestamp"] = int(b.get("timestamp") or time.time())
            lastfm_call(server().conf, "track.scrobble", p, post=True)
    except Exception as e:
        if "Invalid session" in str(e):
            server().execute("DELETE FROM lastfm WHERE user_id=?", (g.user["id"],))
        return fail(e)
    return jsonify(ok=True)
