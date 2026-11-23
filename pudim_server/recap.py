"""Yearly recap ("Retrospectiva") per Jellyfin account, plus the play history used by daylist and Blend.

Where plays come from:
- Jellyfin's "Playback Reporting" plugin records every play. This server copies new rows every hour into its own
  ``plays.db`` with the track's artist/album/genres at that moment, so the recap stays right after tracks are
  replaced or the plugin prunes old data. Without the plugin, only Jellyfin's play counts are imported once
  (source "count", dated at the last play).
- The Spotify "Extended streaming history" export can be uploaded from the app (source "spotify").

A play counts when it lasted at least 30 seconds.
"""
import hashlib
import io
import json
import re
import sqlite3
import threading
import time
import urllib.parse
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, g, jsonify, request, send_file

from .config import data_path
from .core import fail, player_auth, server
from .textutil import norm

bp = Blueprint("recap", __name__)
MIN_S = 30
_lock = threading.Lock()

MONTHS = {
    "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December"],
    "pt": ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
           "novembro", "dezembro"],
}
WEEKDAYS = {
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    "pt": ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"],
}


def db_path(conf):
    return data_path(conf, "plays.db")


def connect(conf):
    c = sqlite3.connect(db_path(conf), timeout=30)
    c.execute("""CREATE TABLE IF NOT EXISTS plays (
        id INTEGER PRIMARY KEY, ts TEXT NOT NULL, user_id TEXT, user_name TEXT, item_id TEXT,
        title TEXT, artist TEXT, album_artist TEXT, album TEXT, year INTEGER, genres TEXT,
        runtime_s INTEGER, played_s INTEGER, client TEXT, device TEXT, method TEXT, source TEXT,
        src_row INTEGER, UNIQUE(source, src_row))""")
    c.execute("CREATE INDEX IF NOT EXISTS plays_user_ts ON plays(user_name, ts)")
    c.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    return c


def _meta(c, k, default=None):
    r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def _set_meta(c, k, v):
    c.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, str(v)))


def local_tz(conf):
    name = conf.get("timezone")
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo


# ------------------------------------------------------------------ collecting
def _items(jf, ids):
    out = {}
    ids = [i for i in ids if i]
    for n in range(0, len(ids), 80):
        qs = urllib.parse.urlencode({"Ids": ",".join(ids[n:n + 80]), "Fields": "Genres,ProductionYear"})
        for it in (jf.call("GET", "/Items?" + qs) or {}).get("Items", []):
            out[it["Id"].replace("-", "")] = it
    return out


def _info(it):
    if not it:
        return {}
    return {"title": it.get("Name"), "artist": (it.get("Artists") or [it.get("AlbumArtist")])[0],
            "album_artist": it.get("AlbumArtist"), "album": it.get("Album"), "year": it.get("ProductionYear"),
            "genres": "|".join(it.get("Genres") or []), "runtime_s": int((it.get("RunTimeTicks") or 0) / 1e7) or None}


def _guess(name):
    a, _, t = (name or "").partition(" - ")
    return {"title": t or name, "artist": a if t else None}


def _plugin(jf, sql):
    return (jf.call("POST", "/user_usage_stats/submit_custom_query",
                    {"CustomQueryString": sql, "ReplaceUserId": False}) or {}).get("results") or []


def collect(srv):
    """Copies new plays from the Playback Reporting plugin. Returns how many were added."""
    jf = srv.jf()
    with _lock:
        c = connect(srv.conf)
        try:
            users = {u["Id"].replace("-", ""): u["Name"] for u in jf.users(include_disabled=True)}
            try:
                plugin_ok = True
                counts = _plugin(jf, "SELECT UserId, ItemId, COUNT(*) FROM PlaybackActivity GROUP BY UserId, ItemId") \
                    if not _meta(c, "backfill_done") else None
            except Exception:
                plugin_ok, counts = False, []
            if not _meta(c, "backfill_done"):
                _backfill(jf, c, users, {(u.replace("-", ""), i.replace("-", "")): int(n) for u, i, n in counts or []})
            n = 0
            if plugin_ok:
                last = int(_meta(c, "last_row", 0))
                rows = _plugin(jf, "SELECT rowid, DateCreated, UserId, ItemId, ItemType, ItemName, PlaybackMethod, "
                                   f"ClientName, DeviceName, PlayDuration FROM PlaybackActivity WHERE rowid > {last} "
                                   "ORDER BY rowid LIMIT 5000")
                meta = _items(jf, list({r[3] for r in rows if r[4] == "Audio"}))
                for rowid, ts, uid, iid, typ, name, method, client, device, dur in rows:
                    last = max(last, int(rowid))
                    if typ != "Audio":
                        continue
                    info = _info(meta.get((iid or "").replace("-", ""))) or _guess(name)
                    c.execute("""INSERT OR IGNORE INTO plays (ts,user_id,user_name,item_id,title,artist,album_artist,
                                 album,year,genres,runtime_s,played_s,client,device,method,source,src_row)
                                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                              (str(ts)[:19].replace("T", " "), uid, users.get((uid or "").replace("-", ""), uid), iid,
                               info.get("title"), info.get("artist"), info.get("album_artist"), info.get("album"),
                               info.get("year"), info.get("genres"), info.get("runtime_s"), int(dur or 0), client,
                               device, method, "plugin", int(rowid)))
                    n += 1
                _set_meta(c, "last_row", last)
            _set_meta(c, "last_collect", time.time())
            c.commit()
            return n
        finally:
            c.close()


def _backfill(jf, c, users, plugin_counts):
    """Once: Jellyfin's own play counts from before the plugin, minus what the plugin already recorded."""
    seq = 0
    for uid, uname in users.items():
        start = 0
        while True:
            qs = urllib.parse.urlencode({"IncludeItemTypes": "Audio", "Recursive": "true", "Filters": "IsPlayed",
                                         "Fields": "Genres,ProductionYear", "StartIndex": start, "Limit": 500})
            r = jf.call("GET", f"/Users/{uid}/Items?{qs}") or {}
            for it in r.get("Items", []):
                ud = it.get("UserData") or {}
                when = (ud.get("LastPlayedDate") or "")[:19]
                if not when:
                    continue
                local = datetime.fromisoformat(when).replace(tzinfo=timezone.utc).astimezone() \
                    .strftime("%Y-%m-%d %H:%M:%S")
                info = _info(it)
                times = (ud.get("PlayCount") or 1) - plugin_counts.get((uid, it["Id"].replace("-", "")), 0)
                for _ in range(max(0, times)):
                    seq += 1
                    c.execute("""INSERT OR IGNORE INTO plays (ts,user_id,user_name,item_id,title,artist,album_artist,
                                 album,year,genres,runtime_s,played_s,client,device,method,source,src_row)
                                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                              (local, uid, uname, it["Id"], info["title"], info["artist"], info["album_artist"],
                               info["album"], info["year"], info["genres"], info["runtime_s"],
                               info["runtime_s"] or 0, None, None, None, "count", seq))
            start += 500
            if start >= r.get("TotalRecordCount", 0):
                break
    _set_meta(c, "backfill_done", time.time())


def monitor(srv):
    while True:
        try:
            collect(srv)
        except Exception as e:
            print(f"recap collect: {e}", flush=True)
        time.sleep(3600)


# ------------------------------------------------------------------ reading
def users(conf):
    c = connect(conf)
    try:
        return [r[0] for r in c.execute("SELECT user_name, COUNT(*) n FROM plays GROUP BY user_name ORDER BY n DESC")]
    finally:
        c.close()


def years(conf, user):
    c = connect(conf)
    try:
        return [int(r[0]) for r in c.execute(
            "SELECT DISTINCT substr(ts,1,4) FROM plays WHERE user_name=? ORDER BY 1 DESC", (user,))]
    finally:
        c.close()


def plays(conf, user, days=365):
    """[(datetime, item_id, artist, [genres])] of the user's plays of library tracks."""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    c = connect(conf)
    try:
        rows = c.execute("SELECT ts, item_id, COALESCE(NULLIF(album_artist,''), artist), genres FROM plays "
                         "WHERE user_name=? AND ts>=? AND item_id IS NOT NULL AND item_id<>''",
                         (user, since)).fetchall()
    finally:
        c.close()
    out = []
    for ts, iid, art, gen in rows:
        try:
            dt = datetime.fromisoformat(str(ts)[:19])
        except ValueError:
            continue
        out.append((dt, iid, art or "", [x.strip() for x in re.split(r"[,;|/]", gen or "") if x.strip()]))
    return out


def rename_user(srv, old, new):
    c = connect(srv.conf)
    try:
        with c:
            n = c.execute("UPDATE plays SET user_name=? WHERE user_name=?", (new, old)).rowcount
    finally:
        c.close()
    return bool(n)


def _streak(days):
    best = cur = 0
    prev = None
    for d in sorted(days):
        cur = cur + 1 if prev and d - prev == timedelta(days=1) else 1
        best, prev = max(best, cur), d
    return best


def recap(conf, user, year, month=None):
    lang = conf.get("language") if conf.get("language") in MONTHS else "en"
    months_l, wd_l = MONTHS[lang], WEEKDAYS[lang]
    prefix = f"{int(year)}-{int(month):02d}" if month else str(int(year))
    c = connect(conf)
    try:
        c.row_factory = sqlite3.Row
        rows = [dict(r) for r in c.execute("SELECT * FROM plays WHERE user_name=? AND ts LIKE ? ORDER BY ts",
                                           (user, prefix + "%"))]
        before = {r[0] for r in c.execute("SELECT DISTINCT lower(artist) FROM plays WHERE user_name=? AND ts < ?",
                                          (user, prefix + ("-01" if month else "-01-01")))}
        first_ts = c.execute("SELECT MIN(ts) FROM plays WHERE user_name=?", (user,)).fetchone()[0]
        plugin_since = c.execute("SELECT MIN(ts) FROM plays WHERE source='plugin'").fetchone()[0]
        months_avail = sorted({int(r[0]) for r in c.execute(
            "SELECT DISTINCT substr(ts,6,2) FROM plays WHERE user_name=? AND ts LIKE ?", (user, str(int(year)) + "%"))})
    finally:
        c.close()
    ps = [r for r in rows if (r["played_s"] or 0) >= MIN_S or r["source"] == "count"]
    for r in ps:
        r["sec"] = min(r["played_s"] or 0, r["runtime_s"] or 10 ** 9)
        r["dt"] = datetime.strptime(r["ts"][:19], "%Y-%m-%d %H:%M:%S")
    art_sec, art_n, song_n, alb_sec, gen_sec = Counter(), Counter(), Counter(), Counter(), Counter()
    song_meta, alb_meta = {}, {}
    by_month, by_hour, by_wd, by_day = Counter(), Counter(), Counter(), Counter()
    month_art = defaultdict(Counter)
    clients = Counter()
    for r in ps:
        a = r["artist"] or "?"
        art_sec[a] += r["sec"]
        art_n[a] += 1
        k = (r["title"] or "?", a)
        song_n[k] += 1
        if r["item_id"] or k not in song_meta:
            song_meta[k] = r
        if r["album"]:
            ak = (r["album"], r["album_artist"] or a)
            alb_sec[ak] += r["sec"]
            if r["item_id"] or ak not in alb_meta:
                alb_meta[ak] = r
        for gn in filter(None, (r["genres"] or "").split("|")):
            gen_sec[gn] += r["sec"]
        m = r["dt"].month - 1
        by_month[m] += r["sec"]
        month_art[m][a] += r["sec"]
        if r["source"] in ("plugin", "spotify"):  # real timestamps
            by_hour[r["dt"].hour] += 1
            by_wd[r["dt"].weekday()] += 1
            if r["client"]:
                clients[r["client"]] += 1
        by_day[r["dt"].date()] += r["sec"]
    total = sum(r["sec"] for r in ps)
    new_artists = [a for a in art_sec if a.lower() not in before]
    top_day = max(by_day.items(), key=lambda x: x[1]) if by_day else None
    art_song = {}
    for (t, a), _ in song_n.most_common():
        art_song.setdefault(a, song_meta[(t, a)]["item_id"])
    return {
        "user": user, "user_key": user, "family": False, "month": int(month) if month else None,
        "label": f"{months_l[int(month) - 1]} {int(year)}" if month else str(int(year)),
        "months_avail": months_avail, "video": None, "people": [],
        "year": int(year), "since": first_ts, "plugin_since": plugin_since,
        "plays": len(ps), "minutes": round(total / 60), "songs": len(song_n), "artists": len(art_sec),
        "albums": len(alb_sec), "days": len(by_day), "streak": _streak(by_day),
        "top_artists": [{"name": a, "minutes": round(s / 60), "plays": art_n[a], "item_id": art_song[a]}
                        for a, s in art_sec.most_common(10)],
        "top_songs": [{"title": t, "artist": a, "plays": n, "item_id": song_meta[(t, a)]["item_id"],
                       "album": song_meta[(t, a)]["album"]} for (t, a), n in song_n.most_common(10)],
        "top_albums": [{"album": al, "artist": ar, "minutes": round(s / 60), "year": alb_meta[(al, ar)]["year"],
                        "item_id": alb_meta[(al, ar)]["item_id"]} for (al, ar), s in alb_sec.most_common(5)],
        "mosaic": [{"item_id": alb_meta[k]["item_id"], "artist": k[1], "album": k[0]}
                   for k, _ in alb_sec.most_common(12)],
        "top_genres": [{"name": gn, "minutes": round(s / 60)} for gn, s in gen_sec.most_common(5)],
        "months": [{"month": months_l[m], "minutes": round(by_month.get(m, 0) / 60),
                    "artist": (month_art[m].most_common(1) or [[None]])[0][0]} for m in range(12)],
        "hours": [by_hour.get(h, 0) for h in range(24)],
        "top_hour": max(by_hour, key=by_hour.get) if by_hour else None,
        "top_weekday": wd_l[max(by_wd, key=by_wd.get)] if by_wd else None,
        "top_day": {"date": top_day[0].isoformat(), "minutes": round(top_day[1] / 60)} if top_day else None,
        "new_artists": len(new_artists), "new_artists_top": sorted(new_artists, key=lambda a: -art_sec[a])[:5],
        "first_song": {"title": ps[0]["title"], "artist": ps[0]["artist"], "ts": ps[0]["ts"]} if ps else None,
        "clients": [{"name": k, "plays": v} for k, v in clients.most_common(4)],
    }


# ------------------------------------------------------------------ Spotify history
def _spotify_rows(data):
    """(start in UTC, title, artist, album, ms played, platform, uri). Extended and 1-year exports."""
    out = []
    if not isinstance(data, list):
        return out
    for e in data:
        if not isinstance(e, dict):
            continue
        ms = int(e.get("ms_played", e.get("msPlayed")) or 0)
        if "ts" in e:
            title, artist = e.get("master_metadata_track_name"), e.get("master_metadata_album_artist_name")
            if not title or not artist:  # podcast episode, audiobook or track without metadata
                continue
            try:
                end = datetime.strptime(e["ts"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
            out.append((end - timedelta(milliseconds=ms), title, artist, e.get("master_metadata_album_album_name"),
                        ms, e.get("platform") or "", e.get("spotify_track_uri") or ""))
        elif "endTime" in e:
            title, artist = e.get("trackName"), e.get("artistName")
            if not title or not artist or artist == "Unknown Artist":
                continue
            try:
                end = datetime.strptime(e["endTime"][:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
            out.append((end - timedelta(milliseconds=ms), title, artist, None, ms, "", ""))
    return out


def _spotify_files(blob, filename):
    music = re.compile(r"(Streaming_History_Audio_|endsong_|StreamingHistory_music_|StreamingHistory\d)[^/]*\.json$",
                       re.I)
    if blob[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            for n in z.namelist():
                if music.search(n) and z.getinfo(n).file_size < 200 * 10 ** 6:
                    yield json.loads(z.read(n).decode("utf-8-sig"))
    else:
        yield json.loads(blob.decode("utf-8-sig"))


def import_spotify(conf, user, blob, filename="history.json"):
    """Adds a Spotify history export to ``user``'s plays. Uploading the same file again adds nothing."""
    tz = local_tz(conf)
    rows, files = [], 0
    try:
        for data in _spotify_files(blob, filename):
            files += 1
            rows += _spotify_rows(data)
    except Exception:  # broken zip, invalid JSON, another kind of file
        files = 0
    if not files:
        raise ValueError("No music history found in that file. Send the zip Spotify emailed you "
                         "(my_spotify_data.zip) or a Streaming_History_Audio_*.json file.")
    stats = {"files": files, "found": len(rows), "added": 0, "duplicated": 0, "short": 0, "already": 0}
    with _lock:
        c = connect(conf)
        try:
            known, near = {}, defaultdict(list)
            for item_id, title, artist, aa, album, year, genres, runtime, ts, src in c.execute(
                    "SELECT item_id, title, artist, album_artist, album, year, genres, runtime_s, ts, source "
                    "FROM plays WHERE user_name=?", (user,)):
                k = (norm(title), norm(artist))
                if item_id and src != "spotify":
                    known[k] = (item_id, aa, album, year, genres, runtime)
                if src == "plugin":
                    near[k].append(ts)
            for start, title, artist, album, ms, platform, uri in rows:
                if ms < MIN_S * 1000:
                    stats["short"] += 1
                    continue
                local = start.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")
                k = (norm(title), norm(artist))
                lt = datetime.strptime(local, "%Y-%m-%d %H:%M:%S")
                if any(abs((datetime.strptime(t[:19], "%Y-%m-%d %H:%M:%S") - lt).total_seconds()) < 600
                       for t in near.get(k, ())):
                    stats["duplicated"] += 1
                    continue
                src_row = int.from_bytes(hashlib.sha1(f"{user}|{start.isoformat()}|{uri or k}".encode())
                                         .digest()[:7], "big")
                item_id, aa, alb, year, genres, runtime = known.get(k, (None,) * 6)
                cur = c.execute("""INSERT OR IGNORE INTO plays (ts,user_id,user_name,item_id,title,artist,album_artist,
                                   album,year,genres,runtime_s,played_s,client,device,method,source,src_row)
                                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (local, None, user, item_id, title, artist, aa or artist, album or alb, year, genres,
                                 runtime, ms // 1000, "Spotify", platform[:60], None, "spotify", src_row))
                stats["added" if cur.rowcount else "already"] += 1
            c.commit()
            stats["total"] = c.execute("SELECT COUNT(*) FROM plays WHERE user_name=? AND source='spotify'",
                                       (user,)).fetchone()[0]
        finally:
            c.close()
    stats["years"] = years(conf, user)
    return stats


def spotify_summary(conf, user):
    c = connect(conf)
    try:
        n, a, b = c.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM plays WHERE user_name=? AND source='spotify'",
                            (user,)).fetchone()
        return {"plays": n, "from": a, "to": b}
    finally:
        c.close()


def remove_spotify(conf, user):
    with _lock:
        c = connect(conf)
        try:
            n = c.execute("DELETE FROM plays WHERE user_name=? AND source='spotify'", (user,)).rowcount
            c.commit()
            return n
        finally:
            c.close()


def image(srv, kind, item=None, artist=None):
    """Cached JPEG path: kind 'artista' (artist photo) or 'capa' (cover of the track's album). None if not found."""
    key = hashlib.sha1(json.dumps([kind, item, norm(artist)]).encode()).hexdigest()
    cache = data_path(srv.conf, "images")
    cache.mkdir(parents=True, exist_ok=True)
    f = cache / f"{key}.jpg"
    if f.exists():
        return f
    jf, data = srv.jf(), None
    try:
        if kind == "artista" and artist:
            data = jf.get_bytes(f"/Artists/{urllib.parse.quote(artist)}/Images/Primary?fillWidth=600&quality=88")
        elif item and re.fullmatch(r"[0-9a-fA-F-]{32,36}", item):
            data = jf.get_bytes(f"/Items/{item}/Images/Primary?fillWidth=600&quality=88")
    except Exception:
        data = None
    if not data:
        return None
    f.write_bytes(data)
    return f


# ------------------------------------------------------------------ routes
@bp.route("/api/player/recap")
@player_auth
def player_recap():
    conf, user = server().conf, g.user["name"]
    ys = years(conf, user)
    if not ys:
        return jsonify(years=[], recap=None)
    try:
        year = int(request.args.get("year") or ys[0])
    except ValueError:
        return fail("Invalid year.", 400)
    try:
        return jsonify(years=ys, recap=recap(conf, user, year))
    except Exception as e:
        return fail(e)


@bp.route("/api/player/recap/spotify", methods=["GET", "POST", "DELETE"])
@player_auth
def player_recap_spotify():
    conf, user = server().conf, g.user["name"]
    try:
        if request.method == "GET":
            return jsonify(spotify_summary(conf, user))
        if request.method == "DELETE":
            return jsonify(removed=remove_spotify(conf, user))
        f = request.files.get("file")
        if not f:
            return fail("Send the Spotify file.", 400)
        blob = f.read(300 * 10 ** 6 + 1)
        if len(blob) > 300 * 10 ** 6:
            return fail("File too large (300 MB max).", 413)
        return jsonify(import_spotify(conf, user, blob, f.filename or "history.json"))
    except ValueError as e:
        return fail(str(e), 400)
    except Exception as e:
        return fail(e)


@bp.route("/api/player/recap/img")
@player_auth
def player_recap_img():
    kind = request.args.get("tipo", "capa")
    if kind not in ("capa", "artista"):
        abort(400)
    f = image(server(), kind, request.args.get("item"), request.args.get("artista"))
    if not f:
        abort(404)
    return send_file(f, mimetype="image/jpeg", max_age=7 * 86400)
