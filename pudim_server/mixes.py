"""Automatic playlists, written as real private playlists in each Jellyfin account.

- Weekly Mix: favorites + most played + similar-sounding songs from AudioMuse (needs ``audiomuse_url``). Mondays.
- Release Radar: recent albums by the artists the account follows (Jellyfin favorites). Fridays.
- daylist: changes with the time of day, from the account's play history (see recap.py) plus AudioMuse.
- Blend: two accounts' taste in one playlist that both get, with a taste match percentage.

State lives in ``<data_dir>/mixes.json``, keyed by Jellyfin user name (Blend by the pair of user ids).
"""
import json
import math
import random
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta

from flask import Blueprint, abort, g, jsonify

from . import playlists, recap
from .config import data_path
from .core import fail, player_auth, server
from .textutil import norm

bp = Blueprint("mixes", __name__)
_lock = threading.Lock()
running = set()  # (kind, key)
PARTS = [(0, 6), (6, 12), (12, 18), (18, 24)]
MIX_SIZE, MIX_SEEDS, PER_SEED, PER_ARTIST, FROM_FAVS, FROM_PLAYED = 48, 14, 14, 3, 9, 9


# ------------------------------------------------------------------ state
def _path(srv):
    return data_path(srv.conf, "mixes.json")


def load(srv):
    try:
        return json.loads(_path(srv).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_all(srv, d):
    tmp = _path(srv).with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(_path(srv))


def put(srv, section, key, value):
    with _lock:
        d = load(srv)
        d.setdefault(section, {})[key] = value
        _save_all(srv, d)


def drop(srv, section, key):
    with _lock:
        d = load(srv)
        if key in d.get(section, {}):
            d[section].pop(key)
            _save_all(srv, d)


def rename_user(srv, old, new):
    moved = False
    with _lock:
        d = load(srv)
        for section in ("radar", "daylist", "mix"):
            sec = d.get(section) or {}
            if old in sec and new not in sec:
                sec[new] = sec.pop(old)
                moved = True
        for st in (d.get("blend") or {}).values():
            for uid, name in list((st.get("users") or {}).items()):
                if name == old:
                    st["users"][uid] = new
                    moved = True
        if moved:
            _save_all(srv, d)
    return moved


def week_id(dt=None):
    y, w, _ = (dt or datetime.now()).isocalendar()
    return f"{y}-W{w:02d}"


# ------------------------------------------------------------------ Jellyfin and AudioMuse helpers
def similar(srv, item_id, n=PER_SEED):
    base = (srv.conf.get("audiomuse_url") or "").rstrip("/")
    if not base:
        return []
    qs = urllib.parse.urlencode({"item_id": item_id, "n": n, "eliminate_duplicates": "true"})
    with urllib.request.urlopen(f"{base}/api/similar_tracks?{qs}", timeout=90) as r:
        d = json.loads(r.read())
    return d if isinstance(d, list) else []


def similar_to(srv, seeds, known, n):
    out, per, lists = [], Counter(), []
    for s in seeds:
        try:
            lists.append([x for x in similar(srv, s, 12) if x.get("item_id") and x["item_id"] not in known])
        except Exception:
            pass
    while any(lists) and len(out) < n:
        for lst in lists:
            while lst:
                x = lst.pop(0)
                a = norm(x.get("author") or "")
                if x["item_id"] in out or per[a] >= 2:
                    continue
                per[a] += 1
                out.append(x["item_id"])
                break
    return out[:n]


def favorites(jf, uid):
    """Tracks of the account's favorites (tracks, albums, artists): [{id, artist, album}] and a summary."""
    def items(qs):
        return (jf.call("GET", f"/Items?userId={uid}&Recursive=true&{qs}", timeout=120) or {}).get("Items", [])
    albums = items("IncludeItemTypes=MusicAlbum&Filters=IsFavorite")
    artists = items("IncludeItemTypes=MusicArtist&Filters=IsFavorite")
    if not artists:
        try:
            artists = (jf.call("GET", f"/Artists?userId={uid}&IsFavorite=true", timeout=60) or {}).get("Items", [])
        except Exception:
            artists = []
    tracks, seen = [], set()

    def add(its):
        for t in its:
            if t["Id"] not in seen:
                seen.add(t["Id"])
                tracks.append({"id": t["Id"], "artist": t.get("AlbumArtist") or (t.get("Artists") or [""])[0],
                               "album": t.get("Album") or ""})
    add(items("IncludeItemTypes=Audio&Filters=IsFavorite"))
    for a in albums:
        add((jf.call("GET", f"/Items?userId={uid}&ParentId={a['Id']}&IncludeItemTypes=Audio&Recursive=true")
             or {}).get("Items", []))
    for a in artists:
        add(items(f"IncludeItemTypes=Audio&ArtistIds={a['Id']}&Limit=200"))
    return tracks, {"albums": len(albums), "artists": len(artists),
                    "names": [a["Name"] for a in (albums + artists)][:12]}


def played(jf, uid):
    its = (jf.call("GET", f"/Items?userId={uid}&Recursive=true&IncludeItemTypes=Audio&Filters=IsPlayed"
                          "&SortBy=PlayCount&SortOrder=Descending&Limit=600&EnableImages=false", timeout=120)
           or {}).get("Items", [])
    return [{"id": t["Id"], "artist": t.get("AlbumArtist") or (t.get("Artists") or [""])[0],
             "album": t.get("Album") or "", "plays": (t.get("UserData") or {}).get("PlayCount") or 1} for t in its]


def _user_jf(srv, user):
    jf = srv.jf(user)
    return jf, jf.user_id()


# ------------------------------------------------------------------ Weekly Mix
def _round_robin(groups, n):
    groups, out = [list(x) for x in groups if x], []
    while groups and len(out) < n:
        for grp in list(groups):
            out.append(grp.pop(random.randrange(len(grp))))
            if not grp:
                groups.remove(grp)
            if len(out) >= n:
                break
    return out


def weekly_mix(srv, user, user_id=None):
    jf, uid = _user_jf(srv, user)
    favs, info = favorites(jf, uid)
    heard = played(jf, uid)
    info["played_tracks"] = len(heard)
    info["played_artists"] = len({norm(t["artist"]) for t in heard})
    if not favs and not heard:
        st = {"week": week_id(), "generated": time.time(), "tracks": 0, "favorites": info,
              "note": srv.t("mix_empty")}
        put(srv, "mix", user, st)
        return st
    known = {t["id"] for t in favs} | {t["id"] for t in heard}
    by_album, by_artist, art_plays = {}, {}, Counter()
    for t in favs:
        by_album.setdefault((t["artist"], t["album"]), []).append(t)
    for t in heard:
        by_artist.setdefault(norm(t["artist"]), []).append(t)
        art_plays[norm(t["artist"])] += t["plays"]
    fav_groups = list(by_album.values())
    random.shuffle(fav_groups)
    top_artists = [a for a, _ in art_plays.most_common(20)]
    random.shuffle(top_artists)
    seeds_f = _round_robin(fav_groups, (MIX_SEEDS // 2 if heard else MIX_SEEDS) if favs else 0)
    seeds_p = _round_robin([by_artist[a][:8] for a in top_artists], MIX_SEEDS - len(seeds_f))
    if len(seeds_f) + len(seeds_p) < MIX_SEEDS:
        seeds_f += _round_robin(fav_groups, MIX_SEEDS - len(seeds_f) - len(seeds_p))
    lists, failed = [], 0
    for s in seeds_f + seeds_p:
        try:
            lists.append([x for x in similar(srv, s["id"]) if x.get("item_id") and x["item_id"] not in known])
        except Exception:
            failed += 1
    if not any(lists):
        raise RuntimeError("AudioMuse returned no similar songs (is audiomuse_url set and the analysis done?).")
    random.shuffle(lists)
    n_fav = min(FROM_FAVS, len(favs))
    pool, from_played = list(range(len(heard))), []
    while pool and len(from_played) < FROM_PLAYED:
        i = random.choices(pool, weights=[heard[j]["plays"] for j in pool])[0]
        pool.remove(i)
        from_played.append(heard[i]["id"])
    picked, per, seen = [], Counter(), set()
    room = MIX_SIZE - n_fav - len(from_played)
    while any(lists) and len(picked) < room:
        for lst in lists:
            while lst:
                x = lst.pop(0)
                a = norm(x.get("author") or "")
                if x["item_id"] in seen or per[a] >= PER_ARTIST:
                    continue
                seen.add(x["item_id"])
                per[a] += 1
                picked.append(x["item_id"])
                break
    anchors = [t["id"] for t in random.sample(favs, n_fav)]
    ids = list(dict.fromkeys(picked[:room] + anchors + from_played))
    random.shuffle(ids)
    old = (load(srv).get("mix", {}).get(user) or {}).get("playlist")
    pid = playlists.write(jf, uid, srv.t("mix"), ids, pid=old)
    st = {"week": week_id(), "generated": time.time(), "tracks": len(ids), "similar": len(picked[:room]),
          "from_favorites": len(anchors), "from_played": len(from_played), "favorites": info,
          "seeds_failed": failed, "playlist": pid, "note": None}
    put(srv, "mix", user, st)
    srv.notify(user_id or uid, srv.t("mix_notice_title", name=srv.t("mix"), user=user),
               srv.t("mix_notice", n=len(ids)), tag="mistura")
    return st


# ------------------------------------------------------------------ Release Radar
def _date(s):
    try:
        return datetime.fromisoformat(str(s)[:19])
    except ValueError:
        return None


def radar(srv, user, user_id=None):
    jf, uid = _user_jf(srv, user)
    arts = (jf.call("GET", f"/Artists?userId={uid}&IsFavorite=true&Fields=", timeout=60) or {}).get("Items", [])
    st = {"generated": time.time(), "tracks": 0, "releases": [], "note": None}
    if not arts:
        st["note"] = srv.t("radar_no_follows")
        put(srv, "radar", user, st)
        return st
    now = datetime.utcnow()
    albums, ids = [], [a["Id"] for a in arts]
    for i in range(0, len(ids), 40):
        albums += (jf.call("GET", f"/Items?userId={uid}&Recursive=true&IncludeItemTypes=MusicAlbum"
                                  f"&AlbumArtistIds={','.join(ids[i:i + 40])}&Years={now.year},{now.year - 1}"
                                  "&Fields=PremiereDate,DateCreated,ChildCount&EnableImages=false", timeout=90)
                   or {}).get("Items", [])

    def fresh(a, days):
        pd = _date(a.get("PremiereDate"))
        if pd and not (pd.month == 1 and pd.day == 1):  # full date in the tags
            return pd >= now - timedelta(days=days)
        dc = _date(a.get("DateCreated"))  # year only: count when it reached the server
        return bool(dc and dc >= now - timedelta(days=min(days, 21))
                    and (a.get("ProductionYear") or 0) >= now.year - (now.month == 1))

    for days in (35, 90):
        picked = [a for a in albums if fresh(a, days)]
        if len(picked) >= 3:
            break
    picked.sort(key=lambda a: str(a.get("PremiereDate") or a.get("DateCreated") or ""), reverse=True)
    tracks, per = [], Counter()
    for a in picked[:25]:
        its = (jf.call("GET", f"/Items?userId={uid}&ParentId={a['Id']}&IncludeItemTypes=Audio&Recursive=true"
                              "&SortBy=ParentIndexNumber,IndexNumber&EnableImages=false") or {}).get("Items", [])
        if len(its) > 6:  # an album: first two and two random ones
            its = its[:2] + random.sample(its[2:], min(2, len(its) - 2))
        art = norm(a.get("AlbumArtist") or "")
        for t in its:
            if per[art] < 6 and len(tracks) < 40:
                per[art] += 1
                tracks.append(t["Id"])
    st["releases"] = [f"{a.get('AlbumArtist') or ''} - {a.get('Name') or ''}" for a in picked[:25]]
    if not tracks:
        st["note"] = srv.t("radar_nothing")
        put(srv, "radar", user, {**(load(srv).get("radar", {}).get(user) or {}), **st})
        return st
    old = (load(srv).get("radar", {}).get(user) or {}).get("playlist")
    st["playlist"] = playlists.write(jf, uid, srv.t("radar"), tracks, pid=old, overview=srv.t("radar_overview"))
    st["tracks"] = len(tracks)
    put(srv, "radar", user, st)
    srv.notify(user_id or uid, f"📡 {srv.t('radar')}", srv.t("radar_notice", n=len(picked)), tag=f"radar-{user}")
    return st


# ------------------------------------------------------------------ daylist
def part_of(dt):
    for i, (a, b) in enumerate(PARTS):
        if a <= dt.hour < b:
            return i
    return 3


def part_key(dt=None):
    dt = dt or datetime.now()
    return f"{dt:%Y-%m-%d}-{part_of(dt)}"


def daylist(srv, user, user_id=None):
    jf, uid = _user_jf(srv, user)
    now = datetime.now()
    pi = part_of(now)
    a, b = PARTS[pi]
    weekend = now.weekday() >= 5
    score, genres, artists = Counter(), Counter(), Counter()
    for dt, iid, art, gen in recap.plays(srv.conf, user):
        w = 1 / (1 + (now - dt).days / 90)
        if a <= dt.hour < b:
            w *= 3
        elif abs(dt.hour - (a + b) / 2) <= 5:
            w *= 1.5
        if (dt.weekday() >= 5) == weekend:
            w *= 1.5
        if dt.weekday() == now.weekday():
            w *= 1.3
        score[iid] += w
        artists[art] += w
        for gn in gen:
            genres[gn.casefold()] += w
    try:  # little history: add what Jellyfin knows the account played
        for t in played(jf, uid)[:200]:
            score[t["id"]] += 0.3 * math.log1p(t["plays"])
    except Exception:
        pass
    if not score:
        st = {"generated": time.time(), "key": part_key(now), "tracks": 0, "note": srv.t("daylist_empty")}
        put(srv, "daylist", user, st)
        return st
    top = [x for x, _ in genres.most_common(2)]
    mood = srv.t("and").join(top) if top else (artists.most_common(1)[0][0] if artists else "")
    name = srv.t("daylist_name", daylist=srv.t("daylist"), mood=mood + " " if mood else "",
                 weekday=srv.t("weekdays")[now.weekday()], part=srv.t("parts")[pi]).strip()
    pool, from_hist = list(score.keys()), []
    while pool and len(from_hist) < 28:  # weighted draw without repeats
        i = random.choices(range(len(pool)), weights=[score[x] for x in pool])[0]
        from_hist.append(pool.pop(i))
    from_hist = playlists.valid_ids(jf, uid, from_hist)
    seeds = [x for x, _ in score.most_common(12)]
    random.shuffle(seeds)
    sim = similar_to(srv, playlists.valid_ids(jf, uid, seeds)[:5], set(score), 14)
    ids = from_hist[:]
    for j, s in enumerate(sim):
        ids.insert(min(len(ids), 2 + j * 3), s)
    ids = list(dict.fromkeys(ids))[:40]
    old = (load(srv).get("daylist", {}).get(user) or {}).get("playlist")
    pid = playlists.write(jf, uid, name, ids, pid=old, prefix=f"{srv.t('daylist')} · ",
                          overview=srv.t("daylist_overview"))
    st = {"generated": time.time(), "key": part_key(now), "tracks": len(ids), "name": name, "playlist": pid,
          "note": None}
    put(srv, "daylist", user, st)
    return st


# ------------------------------------------------------------------ Blend
def _profile(srv, user):
    jf, uid = _user_jf(srv, user)
    items, arts, names = Counter(), Counter(), {}

    def add_artist(name, w):
        k = norm(name)
        if k:
            arts[k] += w
            names.setdefault(k, name)
    for _, iid, art, _ in recap.plays(srv.conf, user):
        items[iid] += 1
        add_artist(art, 1)
    try:
        for t in played(jf, uid):
            items[t["id"]] += t["plays"] * 0.5
            add_artist(t["artist"], t["plays"] * 0.5)
    except Exception:
        pass
    try:
        for t in favorites(jf, uid)[0][:400]:
            items[t["id"]] += 2
            add_artist(t["artist"], 1)
    except Exception:
        pass
    return jf, uid, items, arts, names


def affinity(a, b):
    keys = set(a) | set(b)
    if not keys:
        return 0
    va = {k: math.sqrt(a.get(k, 0)) for k in keys}
    vb = {k: math.sqrt(b.get(k, 0)) for k in keys}
    dot = sum(va[k] * vb[k] for k in keys)
    na = math.sqrt(sum(v * v for v in va.values()))
    nb = math.sqrt(sum(v * v for v in vb.values()))
    if not na or not nb:
        return 0
    return max(1, min(99, round(100 * math.sqrt(dot / (na * nb)))))


def pair_key(a, b):
    return "|".join(sorted([a, b]))


def blend(srv, user_a, user_b, label_a, label_b):
    jfa, uida, ia, aa, na = _profile(srv, user_a)
    jfb, uidb, ib, ab, nb = _profile(srv, user_b)
    pct = affinity(aa, ab)
    common_art = sorted(set(aa) & set(ab), key=lambda k: -min(aa[k], ab[k]))
    ids, seen = [], set()

    def add(x):
        if x and x not in seen:
            seen.add(x)
            ids.append(x)

    for x in sorted(set(ia) & set(ib), key=lambda k: -min(ia[k], ib[k]))[:14]:
        add(x)
    top_a = [x for x, _ in ia.most_common(300)]
    top_b = [x for x, _ in ib.most_common(300)]
    art_of = {}
    for pjf, uid, its in ((jfa, uida, top_a), (jfb, uidb, top_b)):
        for i in range(0, min(len(its), 300), 100):
            r = pjf.call("GET", f"/Items?userId={uid}&Ids={','.join(its[i:i + 100])}&EnableImages=false&Fields=")
            for t in (r or {}).get("Items", []):
                art_of[t["Id"]] = norm(t.get("AlbumArtist") or (t.get("Artists") or [""])[0])
    top_a = [x for x in top_a if x in art_of]
    top_b = [x for x in top_b if x in art_of]
    cset = set(common_art[:20])
    ca = [x for x in top_a if art_of[x] in cset]
    cb = [x for x in top_b if art_of[x] in cset]
    for j in range(16):  # from shared artists, alternating who brought it
        for lst in (ca, cb):
            if j < len(lst) and len(ids) < 30:
                add(lst[j])
    ra, rb = [x for x in top_a if x not in seen], [x for x in top_b if x not in seen]
    j = 0
    while len(ids) < 44 and (j < len(ra) or j < len(rb)):
        for lst in (ra, rb):
            if j < len(lst):
                add(lst[j])
        j += 1
    seeds = ids[:14]
    random.shuffle(seeds)
    for x in similar_to(srv, seeds[:4], set(ia) | set(ib), 6):
        add(x)
    ids = playlists.valid_ids(jfa, uida, ids)[:50]
    if not ids:
        raise RuntimeError(srv.t("blend_not_enough"))
    head, tail = ids[:10], ids[10:]
    random.shuffle(tail)
    ids = head + tail
    key = pair_key(uida, uidb)
    st = load(srv).get("blend", {}).get(key) or {}
    pids = dict(st.get("pids") or {})
    over = srv.t("blend_overview", pct=pct)
    pre = srv.t("blend")
    pids[uida] = playlists.write(jfa, uida, f"{pre}{label_a} + {label_b}", ids, pid=pids.get(uida), overview=over)
    pids[uidb] = playlists.write(jfb, uidb, f"{pre}{label_b} + {label_a}", ids, pid=pids.get(uidb), overview=over)
    st = {"users": {uida: user_a, uidb: user_b}, "labels": {uida: label_a, uidb: label_b}, "pids": pids,
          "affinity": pct, "common": [na.get(k) or nb.get(k) or k for k in common_art[:6]], "tracks": len(ids),
          "generated": time.time(), "error": None}
    put(srv, "blend", key, st)
    return st


# ------------------------------------------------------------------ background runs
def run_bg(srv, kind, key, fn, *args):
    if (kind, key) in running:
        return False

    def go():
        try:
            fn(srv, *args)
        except Exception as e:
            print(f"mixes {kind}: {e}", flush=True)
            st = load(srv).get(kind, {}).get(key) or {}
            put(srv, kind, key, {**st, "error": str(e)[:200], "tried": time.time()})
        finally:
            running.discard((kind, key))
    running.add((kind, key))
    threading.Thread(target=go, daemon=True).start()
    return True


def monitor(srv):
    """Every 10 minutes: daylist for a new part of the day, Radar on Fridays, Weekly Mix on Mondays,
    Blends older than a day."""
    time.sleep(240)
    while True:
        try:
            users = {u["name"]: u["id"] for u in srv.all_users(fresh=True)}
            now = datetime.now()
            d = load(srv)
            for name, uid in users.items():
                st = (d.get("daylist") or {}).get(name) or {}
                if st.get("key") != part_key(now) and time.time() - (st.get("tried") or 0) > 1800:
                    _safe(srv, "daylist", name, daylist, uid)
                st = (d.get("radar") or {}).get(name) or {}
                gen = datetime.fromtimestamp(st["generated"]) if st.get("generated") else None
                done = gen and week_id(gen) == week_id(now) and gen.weekday() >= 4
                if now.weekday() == 4 and now.hour >= 5 and not done and time.time() - (st.get("tried") or 0) > 3 * 3600:
                    _safe(srv, "radar", name, radar, uid)
                st = (d.get("mix") or {}).get(name) or {}
                if srv.conf.get("audiomuse_url") and now.weekday() == 0 and now.hour >= 4 \
                        and st.get("week") != week_id(now) and time.time() - (st.get("tried") or 0) > 3 * 3600:
                    _safe(srv, "mix", name, weekly_mix, uid)
            ids = {v: k for k, v in users.items()}
            for key, st in list((d.get("blend") or {}).items()):
                if time.time() - (st.get("generated") or 0) < 86400 or time.time() - (st.get("tried") or 0) < 6 * 3600:
                    continue
                a, b = key.split("|")
                if a in ids and b in ids:
                    try:
                        blend(srv, ids[a], ids[b], srv.display_name(a, ids[a]), srv.display_name(b, ids[b]))
                    except Exception as e:
                        put(srv, "blend", key, {**st, "error": str(e)[:200], "tried": time.time()})
        except Exception as e:
            print(f"mixes monitor: {e}", flush=True)
        time.sleep(600)


def _safe(srv, kind, name, fn, uid):
    if (kind, name) in running:
        return
    running.add((kind, name))
    try:
        fn(srv, name, uid)
    except Exception as e:
        st = load(srv).get(kind, {}).get(name) or {}
        put(srv, kind, name, {**st, "error": str(e)[:200], "tried": time.time()})
    finally:
        running.discard((kind, name))


# ------------------------------------------------------------------ routes
@bp.route("/api/player/mixes")
@player_auth
def player_mixes():
    srv, name = server(), g.user["name"]
    mx = load(srv).get("mix", {}).get(name) or {}
    return jsonify(
        descobertas={"name": srv.t("discovery"), "week": None, "generated": None, "this_week": False, "tracks": 0,
                     "running": False, "error": None, "available": False},
        mistura=dict(mx, name=srv.t("mix"), available=bool(srv.conf.get("audiomuse_url")),
                     running=("mix", name) in running))


@bp.route("/api/player/mixes/<kind>", methods=["POST"])
@player_auth
def player_mix_run(kind):
    srv, name = server(), g.user["name"]
    if kind == "descobertas":
        return fail("Weekly Discovery needs a download backend, which this server does not include.", 501)
    if kind != "mistura":
        abort(404)
    if not srv.conf.get("audiomuse_url"):
        return fail("The Weekly Mix needs AudioMuse (audiomuse_url).", 400)
    if not run_bg(srv, "mix", name, weekly_mix, name, g.user["id"]):
        return fail("Already building your mix.", 409)
    return jsonify(ok=True)


def _blend_out(key, st, me):
    other = next((u for u in (st.get("users") or {}) if u != me), None)
    return {"user_id": other, "name": (st.get("labels") or {}).get(other) or "", "affinity": st.get("affinity"),
            "common": st.get("common") or [], "playlist": (st.get("pids") or {}).get(me),
            "tracks": st.get("tracks") or 0, "generated": st.get("generated"), "error": st.get("error"),
            "running": ("blend", key) in running}


@bp.route("/api/player/made")
@player_auth
def player_made():
    srv = server()
    d, me, name = load(srv), g.user["id"], g.user["name"]
    blends = [_blend_out(k, st, me) for k, st in (d.get("blend") or {}).items() if me in (st.get("users") or {})]
    blends += [{"user_id": k.replace(me, "").strip("|"), "running": True, "affinity": None, "common": [],
                "name": "", "playlist": None, "tracks": 0}
               for kind, k in list(running) if kind == "blend" and me in k and k not in (d.get("blend") or {})]
    return jsonify(radar={**((d.get("radar") or {}).get(name) or {}), "running": ("radar", name) in running},
                   daylist={**((d.get("daylist") or {}).get(name) or {}), "running": ("daylist", name) in running},
                   blends=blends)


@bp.route("/api/player/made/<kind>", methods=["POST"])
@player_auth
def player_made_run(kind):
    if kind not in ("radar", "daylist"):
        abort(404)
    srv, name = server(), g.user["name"]
    if not run_bg(srv, kind, name, radar if kind == "radar" else daylist, name, g.user["id"]):
        return fail("Already building it.", 409)
    return jsonify(ok=True)


@bp.route("/api/player/blend/<uid>", methods=["POST"])
@player_auth
def player_blend(uid):
    srv, me = server(), dict(g.user)
    if uid == me["id"]:
        return fail("Pick someone else.", 400)
    users = {u["id"]: u["name"] for u in srv.all_users()}
    if uid not in users:
        abort(404)
    key = pair_key(me["id"], uid)
    first = key not in (load(srv).get("blend") or {})

    def make(srv_):
        st = blend(srv_, me["name"], users[uid], srv_.display_name(me["id"], me["name"]),
                   srv_.display_name(uid, users[uid]))
        if first:  # tell the other person in the chat, with their copy of the playlist
            srv_.chat(me["id"], uid, srv_.t("blend_chat", pct=st["affinity"]),
                      {"kind": "playlist", "id": st["pids"][uid], "subtitle": "Blend", "cover": "",
                       "title": f"{srv_.t('blend')}{st['labels'][uid]} + {st['labels'][me['id']]}"})
    if not run_bg(srv, "blend", key, make):
        return fail("Already building that Blend.", 409)
    return jsonify(ok=True)


@bp.route("/api/player/blend/<uid>", methods=["DELETE"])
@player_auth
def player_blend_delete(uid):
    srv = server()
    key = pair_key(g.user["id"], uid)
    st = (load(srv).get("blend") or {}).get(key)
    if not st:
        abort(404)
    jf = srv.jf()
    for pid in (st.get("pids") or {}).values():
        try:
            jf.call("DELETE", f"/Items/{pid}")
        except Exception as e:
            print(f"delete blend playlist: {e}", flush=True)
    drop(srv, "blend", key)
    return jsonify(ok=True)
