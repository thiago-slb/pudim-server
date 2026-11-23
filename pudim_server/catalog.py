"""Catalog (Deezer public API, no key) marked with what is already in the Jellyfin library.

Every catalog item carries ``jellyfin_id`` when the library already has it, so the app plays it from Jellyfin.
Playing a catalog item that is *not* in the library is optional: set ``catalog_stream_command`` to an external
resolver (see README, "Catalog playback"). Nothing that downloads audio is bundled.
"""
import glob
import json
import mimetypes
import os
import secrets
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Blueprint, abort, g, jsonify, redirect, request, send_file

from . import playlists
from .config import data_path
from .core import fail, player_auth, server
from .jellyfin import UA
from .textutil import base_title, index_items, match, norm, same_artist

DZ = "https://api.deezer.com"
CACHE_HOURS = 6

bp = Blueprint("catalog", __name__)
_dz_cache = {}
_pool = ThreadPoolExecutor(8)
_lock = threading.Lock()
_build_lock = threading.Lock()
_idx = {"t": 0, "tracks": None, "albums": None, "artists": None}
_streams, _resolved, _locks = {}, {}, {}


# ------------------------------------------------------------------ Deezer
def dz(path, ttl=600):
    url = path if path.startswith("http") else DZ + path
    now = time.time()
    hit = _dz_cache.get(url)
    if hit and hit[0] > now:
        return hit[1]
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=20) as r:
        data = json.loads(r.read())
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"Deezer: {data['error'].get('message') or data['error']}")
    if len(_dz_cache) > 2000:
        _dz_cache.clear()
    _dz_cache[url] = (now + ttl, data)
    return data


def q(params):
    return urllib.parse.urlencode(params)


def playlist_tracks(pid, limit=500):
    p = dz(f"/playlist/{int(pid)}")
    data, url = list((p.get("tracks") or {}).get("data", [])), (p.get("tracks") or {}).get("next")
    while url and len(data) < limit:
        page = dz(url)
        data += page.get("data", [])
        url = page.get("next")
    return p, data


# ------------------------------------------------------------------ library indexes
def build_indexes(srv):
    """Tracks, albums and artists of the library by normalised name. Built outside the lock, swapped at once."""
    jf = srv.jf()
    d = jf.call("GET", "/Items?IncludeItemTypes=Audio&Recursive=true&EnableImages=false&EnableUserData=false",
                timeout=120) or {}
    tracks = index_items(d.get("Items", []))
    al = jf.call("GET", "/Items?IncludeItemTypes=MusicAlbum&Recursive=true&EnableImages=false"
                        "&EnableUserData=false", timeout=120) or {}
    albums = {}
    for it in al.get("Items", []):
        arts = {norm(a) for a in (it.get("Artists") or []) + [it.get("AlbumArtist") or ""] if a}
        albums.setdefault(base_title(it.get("Name"))[0], []).append({"id": it["Id"], "arts": arts})
    ar = jf.call("GET", "/Artists/AlbumArtists?Recursive=true&EnableImages=false&EnableUserData=false",
                 timeout=120) or {}
    artists = {norm(it.get("Name")): it["Id"] for it in ar.get("Items", []) if it.get("Name")}
    with _lock:
        _idx.update(t=time.time(), tracks=tracks, albums=albums, artists=artists)
    return _idx


def indexes(srv):
    if _idx["albums"] is None:
        with _build_lock:
            if _idx["albums"] is None:
                build_indexes(srv)
    return _idx if _idx["albums"] is not None else None


def reset_indexes():
    with _lock:
        _idx.update(t=0, tracks=None, albums=None, artists=None)


def warm(srv, every=240):
    """Keeps the indexes fresh (building them from scratch can take several seconds on big libraries)."""
    while True:
        try:
            with _build_lock:
                build_indexes(srv)
        except Exception as e:
            print(f"library index: {e}", flush=True)
        time.sleep(every)


class Marker:
    def __init__(self, srv):
        try:
            self.idx = indexes(srv)
            self.ok = bool(self.idx)
        except Exception:
            self.ok = False

    def track(self, t):
        if not self.ok:
            return None
        return match({"title": t["title"], "artist": t["artist"], "album": t.get("album") or "",
                      "duration": t.get("duration") or 0}, self.idx["tracks"])

    def album(self, title, artist):
        if not self.ok:
            return None
        a = norm(artist)
        for c in self.idx["albums"].get(base_title(title)[0], []):
            if same_artist(a, c["arts"]):
                return c["id"]
        return None

    def artist(self, name):
        return self.idx["artists"].get(norm(name)) if self.ok else None


# ------------------------------------------------------------------ JSON shapes the app expects
def f_track(t, mk, album=None):
    al = t.get("album") or album or {}
    ar = t.get("artist") or {}
    out = {"kind": "track", "deezer_id": t["id"], "title": t.get("title") or "", "artist": ar.get("name") or "",
           "artist_id": ar.get("id"), "album": al.get("title") or "", "album_id": al.get("id"),
           "duration": t.get("duration") or 0, "explicit": bool(t.get("explicit_lyrics")),
           "cover": al.get("cover_xl") or al.get("cover_big") or al.get("cover_medium")}
    out["jellyfin_id"] = mk.track(out)
    return out


def f_album(a, mk, artist=None):
    ar = a.get("artist") or artist or {}
    out = {"kind": "album", "deezer_id": a["id"], "title": a.get("title") or "", "artist": ar.get("name") or "",
           "artist_id": ar.get("id"), "year": (a.get("release_date") or "")[:4] or None,
           "record_type": a.get("record_type"), "tracks": a.get("nb_tracks"),
           "cover": a.get("cover_xl") or a.get("cover_big") or a.get("cover_medium")}
    out["jellyfin_id"] = mk.album(out["title"], out["artist"])
    return out


def f_artist(a, mk):
    out = {"kind": "artist", "deezer_id": a["id"], "title": a.get("name") or "", "fans": a.get("nb_fan"),
           "cover": a.get("picture_xl") or a.get("picture_big") or a.get("picture_medium")}
    out["jellyfin_id"] = mk.artist(out["title"])
    return out


def f_playlist(p):
    owner = (p.get("user") or {}).get("name") or ""
    return {"kind": "playlist", "deezer_id": p["id"], "title": p.get("title") or "", "owner": owner,
            "tracks": p.get("nb_tracks"), "editorial": owner.lower().startswith("deezer"),
            "cover": p.get("picture_xl") or p.get("picture_big") or p.get("picture_medium"), "jellyfin_id": None}


# ------------------------------------------------------------------ routes
@bp.route("/api/player/search")
@player_auth
def player_search():
    text = (request.args.get("q") or "").strip()[:100]
    if not text:
        return jsonify(tracks=[], albums=[], artists=[], playlists=[])
    try:
        jobs = {k: _pool.submit(dz, f"/search/{k}?" + q({"q": text, "limit": n}), 300)
                for k, n in (("track", 25), ("album", 15), ("artist", 10), ("playlist", 10))}
        mk = Marker(server())
        res = {k: j.result().get("data", []) for k, j in jobs.items()}
    except Exception as e:
        return fail(e)
    tracks, seen = [], set()
    for t in res["track"]:  # the same song shows up on compilations; keep the first
        key = (norm(t.get("title")), norm((t.get("artist") or {}).get("name")))
        if key not in seen:
            seen.add(key)
            tracks.append(f_track(t, mk))
    return jsonify(tracks=tracks, albums=[f_album(a, mk) for a in res["album"]],
                   artists=[f_artist(a, mk) for a in res["artist"]],
                   playlists=[f_playlist(p) for p in res["playlist"]])


@bp.route("/api/player/artist")
@player_auth
def player_artist():
    """?id=<deezer id> or ?name=<name>: top 10, albums, related artists."""
    try:
        aid = request.args.get("id", "")
        if not aid.isdigit():
            name = (request.args.get("name") or "").strip()[:100]
            if not name:
                return fail("Missing artist.", 400)
            found = dz("/search/artist?" + q({"q": name, "limit": 10})).get("data", [])
            same = [a for a in found if norm(a.get("name")) == norm(name)] or found[:1]
            if not same:
                return jsonify(artist=None, top=[], albums=[], related=[])
            aid = str(max(same, key=lambda a: a.get("nb_fan") or 0)["id"])
        jobs = {"artist": _pool.submit(dz, f"/artist/{aid}"),
                "top": _pool.submit(dz, f"/artist/{aid}/top?limit=10"),
                "albums": _pool.submit(dz, f"/artist/{aid}/albums?limit=100"),
                "related": _pool.submit(dz, f"/artist/{aid}/related?limit=12")}
        mk = Marker(server())
        r = {k: j.result() for k, j in jobs.items()}
    except Exception as e:
        return fail(e)
    art = r["artist"]
    me = {"id": art.get("id"), "name": art.get("name")}
    albums = [f_album(a, mk, me) for a in r["albums"].get("data", [])]
    albums.sort(key=lambda a: a.get("year") or "", reverse=True)
    return jsonify(artist=f_artist(art, mk), top=[f_track(t, mk) for t in r["top"].get("data", [])],
                   albums=albums, related=[f_artist(a, mk) for a in r["related"].get("data", [])])


@bp.route("/api/player/album")
@player_auth
def player_album():
    aid = request.args.get("id", "")
    if not aid.isdigit():
        return fail("Invalid album.", 400)
    try:
        a = dz(f"/album/{aid}")
        mk = Marker(server())
    except Exception as e:
        return fail(e)
    al = {"id": a["id"], "title": a.get("title"), "cover_xl": a.get("cover_xl"), "cover_big": a.get("cover_big")}
    return jsonify(album=f_album(a, mk),
                   tracks=[f_track(t, mk, al) for t in (a.get("tracks") or {}).get("data", [])])


@bp.route("/api/player/playlist")
@player_auth
def player_playlist():
    pid = request.args.get("id", "")
    if not pid.isdigit():
        return fail("Invalid playlist.", 400)
    try:
        p, data = playlist_tracks(pid, 300)
        mk = Marker(server())
    except Exception as e:
        return fail(e)
    return jsonify(playlist=f_playlist(p), tracks=[f_track(t, mk) for t in data])


@bp.route("/api/player/featured")
@player_auth
def player_featured():
    try:
        res = dz("/chart/0/playlists?limit=30").get("data", [])
    except Exception as e:
        return fail(e)
    return jsonify(playlists=[{"kind": "playlist", "deezer_id": p["id"], "title": p.get("title") or "", "owner": "",
                               "tracks": p.get("nb_tracks"), "editorial": True,
                               "cover": p.get("picture_xl") or p.get("picture_big") or p.get("picture_medium"),
                               "jellyfin_id": None} for p in res][:20])


@bp.route("/api/player/playlist/save", methods=["POST"])
@player_auth
def player_playlist_save():
    """Recreates a catalog playlist in the caller's Jellyfin account with the songs the library already has."""
    srv = server()
    b = request.get_json(silent=True) or {}
    pid = str(b.get("deezer_id") or "")
    if not pid.isdigit():
        return fail("Invalid playlist.", 400)
    try:
        p, data = playlist_tracks(pid)
        idx = indexes(srv)
        if not idx:
            return fail("The library index is not ready yet.", 503)
        ids = []
        for t in data:
            i = match({"title": t.get("title") or "", "artist": (t.get("artist") or {}).get("name") or "",
                       "album": (t.get("album") or {}).get("title") or "", "duration": t.get("duration") or 0},
                      idx["tracks"])
            if i and i not in ids:
                ids.append(i)
        name = p.get("title") or "Playlist"
        if ids:
            jf, uid = srv.jf(), g.user["id"]
            title = srv.t("deezer_prefix") + name
            existed = playlists.find(jf, uid, name=title)
            new = playlists.write(jf, uid, title, ids)
            if not existed and p.get("picture_xl"):
                playlists.set_cover(jf, new, p["picture_xl"])
    except Exception as e:
        return fail(e)
    return jsonify(name=name, found=len(ids), total=len(data), downloading=0)


# ------------------------------------------------------------------ optional catalog playback
def _cache_dir(srv):
    d = data_path(srv.conf, "stream-cache")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cleanup(srv):
    cut = time.time() - CACHE_HOURS * 3600
    for p in glob.glob(str(_cache_dir(srv) / "*")):
        try:
            if os.path.getmtime(p) < cut:
                os.remove(p)
        except OSError:
            pass
    with _lock:
        for k in [k for k, v in _streams.items() if v[0] < time.time()]:
            _streams.pop(k, None)
        for k in [k for k, v in _resolved.items() if not os.path.exists(v)]:
            _resolved.pop(k, None)


def _resolve(srv, did, cmd):
    with _lock:
        lk = _locks.setdefault(did, threading.Lock())
    with lk:  # players often probe twice in a row (Range); resolve each track once
        path = _resolved.get(did)
        if path and os.path.exists(path):
            os.utime(path)
            return path, None
        t = dz(f"/track/{did}", 3600)
        cache = _cache_dir(srv)
        r = subprocess.run([*cmd, (t.get("artist") or {}).get("name") or "", t.get("title") or "",
                            str(t.get("duration") or 0), str(cache)], capture_output=True, text=True, timeout=150)
        line = (r.stdout or "").strip().splitlines()[-1:] or ["{}"]
        try:
            res = json.loads(line[0])
        except ValueError:
            res = {}
        if not res.get("ok"):
            return None, res.get("error") or "The stream resolver failed."
        path = Path(res["path"]).resolve()
        if cache.resolve() not in path.parents or not path.is_file():
            return None, "The stream resolver returned a file outside the cache directory."
        _resolved[did] = str(path)
        return str(path), None


@bp.route("/api/player/play/<int:did>")
@player_auth
def player_play(did):
    srv = server()
    cmd = srv.conf.get("catalog_stream_command") or []
    if not cmd:
        return fail("Playing songs that are not in the library is not enabled on this server.", 501)
    _cleanup(srv)
    try:
        path, err = _resolve(srv, did, cmd)
    except Exception as e:
        return fail(e)
    if not path:
        return fail(err, 404)
    tok = secrets.token_urlsafe(24)
    with _lock:
        _streams[tok] = (time.time() + CACHE_HOURS * 3600, path)
    return redirect(f"../stream/{tok}", code=302)  # relative, so it also works under a path prefix


@bp.route("/api/player/stream/<tok>")
def player_stream(tok):
    """No login here: the random, short-lived token is the authorisation (players can't always send headers)."""
    hit = _streams.get(tok)
    if not hit or hit[0] < time.time() or not os.path.exists(hit[1]):
        abort(404)
    mime = {".m4a": "audio/mp4", ".webm": "audio/webm", ".opus": "audio/ogg", ".mp3": "audio/mpeg"}.get(
        os.path.splitext(hit[1])[1], mimetypes.guess_type(hit[1])[0] or "application/octet-stream")
    return send_file(hit[1], mimetype=mime, conditional=True, max_age=3600)
