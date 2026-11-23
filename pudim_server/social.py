"""Profile, contacts and chat. Contacts are the accounts of the Jellyfin server."""
import json
import re
import time

from flask import Blueprint, abort, g, jsonify, request

from . import mixes, recap
from .core import PLAYLIST_ID, fail, player_auth, server

bp = Blueprint("social", __name__)
NAME_RE = re.compile(r"^[\w][\w .@'-]{0,39}$")


def _msg(r):
    try:
        share = json.loads(r["share"]) if r["share"] else None
    except ValueError:
        share = None
    return {"id": r["id"], "ts": r["ts"], "from": r["from_id"], "to": r["to_id"], "text": r["text"] or "",
            "share": share, "read": bool(r["read"])}


def _find(uid):
    u = next((x for x in server().all_users() if x["id"] == uid), None)
    if not u:
        abort(404)
    return u


@bp.route("/api/player/me")
@player_auth
def player_me():
    srv = server()
    dn = srv.names()
    u = next((x for x in srv.all_users(fresh=True) if x["id"] == g.user["id"]), {})
    return jsonify(id=g.user["id"], name=g.user["name"], display_name=dn.get(g.user["id"]) or g.user["name"],
                   image=u.get("image"))


@bp.route("/api/player/me", methods=["POST"])
@player_auth
def player_me_save():
    dn = str((request.get_json(silent=True) or {}).get("display_name") or "").strip()[:40]
    server().execute("INSERT INTO profiles (user_id, display_name) VALUES (?, ?) ON CONFLICT(user_id) "
                     "DO UPDATE SET display_name=excluded.display_name", (g.user["id"], dn or None))
    return jsonify(ok=True, display_name=dn or g.user["name"])


@bp.route("/api/player/me/username", methods=["POST"])
@player_auth
def player_me_rename():
    """Renames the Jellyfin account and moves what this server keeps by user name."""
    srv = server()
    new = str((request.get_json(silent=True) or {}).get("name") or "").strip()
    old, uid = g.user["name"], g.user["id"]
    if not NAME_RE.fullmatch(new):
        return fail("Use 1 to 40 letters, digits, spaces, dots, @, hyphens or apostrophes.", 400)
    if new == old:
        return jsonify(ok=True, name=new)
    if any(u["name"].casefold() == new.casefold() and u["id"] != uid for u in srv.all_users(fresh=True)):
        return fail("An account with that name already exists.", 409)
    try:
        jf = srv.jf()
        dto = jf.call("GET", f"/Users/{uid}")
        dto["Name"] = new
        jf.call("POST", f"/Users/{uid}", dto)
    except Exception as e:
        return fail(f"Jellyfin refused it: {e}")
    moved = []
    for label, fn in (("mixes", lambda: mixes.rename_user(srv, old, new)),
                      ("recap", lambda: recap.rename_user(srv, old, new))):
        try:
            if fn():
                moved.append(label)
        except Exception as e:
            print(f"rename {label}: {e}", flush=True)
    srv.tokens.forget_user(uid)
    srv.forget_users()
    return jsonify(ok=True, name=new, moved=moved)


@bp.route("/api/player/contacts")
@player_auth
def player_contacts():
    srv = server()
    dn = srv.names()
    out = [srv.person(u, dn) for u in srv.all_users() if u["id"] != g.user["id"]]
    out.sort(key=lambda p: p["display_name"].casefold())
    return jsonify(contacts=out)


@bp.route("/api/player/chats")
@player_auth
def player_chats():
    srv, me = server(), g.user["id"]
    dn = srv.names()
    byid = {u["id"]: u for u in srv.all_users()}
    rows = srv.query("""SELECT m.* FROM messages m JOIN (
        SELECT CASE WHEN from_id=? THEN to_id ELSE from_id END other, MAX(id) mx FROM messages
        WHERE from_id=? OR to_id=? GROUP BY other) x ON m.id = x.mx ORDER BY m.id DESC""", (me, me, me))
    unread = {r["from_id"]: r["n"] for r in srv.query(
        "SELECT from_id, COUNT(*) n FROM messages WHERE to_id=? AND read=0 GROUP BY from_id", (me,))}
    out = []
    for r in rows:
        other = r["to_id"] if r["from_id"] == me else r["from_id"]
        if other in byid:
            out.append({"user": srv.person(byid[other], dn), "last": _msg(r), "unread": unread.get(other, 0)})
    return jsonify(chats=out, unread=sum(unread.values()), last_id=max((r["id"] for r in rows), default=0))


@bp.route("/api/player/chat/<uid>")
@player_auth
def player_chat(uid):
    srv, me, other = server(), g.user["id"], _find(uid)
    after = request.args.get("after", type=int) or 0
    rows = srv.query("""SELECT * FROM messages WHERE id > ? AND ((from_id=? AND to_id=?) OR (from_id=? AND to_id=?))
        ORDER BY id DESC LIMIT 300""", (after, me, uid, uid, me))
    srv.execute("UPDATE messages SET read=1 WHERE from_id=? AND to_id=? AND read=0", (uid, me))
    return jsonify(user=srv.person(other), messages=[_msg(r) for r in reversed(rows)])


@bp.route("/api/player/chat/<uid>", methods=["POST"])
@player_auth
def player_chat_send(uid):
    srv, me, other = server(), g.user["id"], _find(uid)
    if uid == me:
        return fail("Pick someone else.", 400)
    b = request.get_json(silent=True) or {}
    text = str(b.get("text") or "").strip()[:4000]
    share = b.get("share")
    if share is not None:
        if not isinstance(share, dict) or share.get("kind") not in ("track", "album", "artist", "playlist"):
            return fail("That can't be shared.", 400)
        share = {k: str(share.get(k) or "")[:500] for k in ("kind", "id", "title", "subtitle", "cover")}
        # A playlist belongs to one account: give the recipient read access (Jellyfin 10.9+).
        if share["kind"] == "playlist" and PLAYLIST_ID.fullmatch(share["id"]):
            try:
                srv.jf().call("POST", f"/Playlists/{share['id']}/Users/{uid}", {"CanEdit": False})
            except Exception as e:
                print(f"share playlist: {e}", flush=True)
    if not text and not share:
        return fail("Empty message.", 400)
    mid = srv.execute("INSERT INTO messages (ts, from_id, to_id, text, share) VALUES (?, ?, ?, ?, ?)",
                      (time.time(), me, uid, text, json.dumps(share, ensure_ascii=False) if share else None))
    row = srv.query("SELECT * FROM messages WHERE id=?", (mid,))[0]
    return jsonify(message=_msg(row), user=srv.person(other))


@bp.route("/api/player/notices")
@player_auth
def player_notices():
    try:
        since = float(request.args.get("since") or 0)
    except ValueError:
        since = 0
    rows = server().query("SELECT ts, title, body, tag FROM notices WHERE user_id=? AND ts>? ORDER BY ts",
                          (g.user["id"], since))
    return jsonify(notices=[{"ts": r["ts"], "title": r["title"] or "", "body": r["body"] or "",
                             "tag": r["tag"] or ""} for r in rows], now=time.time())
