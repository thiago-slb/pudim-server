"""Concerts, podcasts and following catalog artists.

- Shows: MusicBrainz events (open data, no key) for the artists an account follows (Jellyfin favorites plus
  followed catalog artists). Fetched in the background at 1 request/second, as MusicBrainz asks, and cached in
  ``<data_dir>/shows.json``.
- Podcasts: Apple's public podcast directory (search and top charts, no account) and each show's RSS feed.
  Subscriptions and playback progress are stored per Jellyfin account.
- Follows: catalog artists that are not in the library yet. Once the library has them, they become Jellyfin
  favorites and leave this list.
"""
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date
from email.utils import parsedate_to_datetime

from flask import Blueprint, abort, g, jsonify, request

from . import __version__
from .config import data_path
from .core import fail, player_auth, server

bp = Blueprint("media", __name__)
MB = "https://musicbrainz.org/ws/2"
ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
_shows = None
_shows_lock = threading.Lock()


def user_agent(conf):
    contact = conf.get("contact") or "https://github.com/thiago-slb/pudim-server"
    return f"pudim-server/{__version__} ( {contact} )"


def _get(url, ua, timeout=20, raw=False, tries=3):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "application/json, */*"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read(8 * 1024 * 1024)
            return data if raw else json.loads(data)
        except urllib.error.HTTPError as e:
            if e.code != 503 or i == tries - 1:
                raise
        except (TimeoutError, urllib.error.URLError):
            if i == tries - 1:
                raise
        time.sleep(3 * (i + 1))  # MusicBrainz answers 503 above its rate limit: wait and retry


# ------------------------------------------------------------------ shows (MusicBrainz)
class Shows:
    def __init__(self, path, ua):
        self.path, self.ua = str(path), ua
        self.lock = threading.Lock()
        self.queue, self.worker = [], None
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}
        self.data.setdefault("artists", {})
        self.data.setdefault("places", {})

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def want(self, names):
        """Queues artists with no data or data older than a day; returns how many are pending."""
        now = time.time()
        with self.lock:
            for n in names:
                a = self.data["artists"].get(n.casefold())
                if (not a or now - a.get("t", 0) > 86400) and n not in self.queue:
                    self.queue.append(n)
            pending = len(self.queue)
            if self.queue and not (self.worker and self.worker.is_alive()):
                self.worker = threading.Thread(target=self._run, daemon=True)
                self.worker.start()
        return pending

    def _run(self):
        while True:
            with self.lock:
                if not self.queue:
                    return
                name = self.queue[0]
            try:
                self._fetch(name)
            except Exception as e:
                print(f"shows: {e}", flush=True)
                with self.lock:
                    self.data["artists"][name.casefold()] = {"t": time.time() - 86400 + 3600, "events": [],
                                                             "error": str(e)[:200]}
            with self.lock:
                if self.queue and self.queue[0] == name:
                    self.queue.pop(0)
                self._save()

    def _fetch(self, name):
        qs = urllib.parse.quote(f'artist:"{name}"')
        found = _get(f"{MB}/artist?query={qs}&limit=5&fmt=json", self.ua).get("artists") or []
        time.sleep(1.1)
        best = next((a for a in found if a.get("name", "").casefold() == name.casefold()), None) \
            or (found[0] if found and int(found[0].get("score") or 0) >= 95 else None)
        events = []
        if best:
            off = 0
            while True:
                d = _get(f"{MB}/event?artist={best['id']}&limit=100&offset={off}&inc=place-rels+area-rels&fmt=json",
                         self.ua)
                time.sleep(1.1)
                for e in d.get("events") or []:
                    day = (e.get("life-span") or {}).get("begin") or ""
                    if len(day) < 10:
                        continue
                    place = area = None
                    for r in e.get("relations") or []:
                        if r.get("place") and not place:
                            place = r["place"]
                        if r.get("area") and not area:
                            area = r["area"]
                    events.append({"id": e["id"], "date": day[:10], "time": e.get("time") or "",
                                   "name": e.get("name") or "", "type": e.get("type") or "",
                                   "cancelled": bool(e.get("cancelled")),
                                   "place": place.get("name") if place else "",
                                   "place_id": place.get("id") if place else "",
                                   "area": area.get("name") if area else ""})
                off += 100
                if off >= int(d.get("event-count") or 0) or off >= 600:
                    break
            for ev in events:  # city/country of each venue, cached forever
                pid = ev["place_id"]
                if pid and pid not in self.data["places"]:
                    try:
                        p = _get(f"{MB}/place/{pid}?inc=area-rels&fmt=json", self.ua)
                        self.data["places"][pid] = {"city": (p.get("area") or {}).get("name") or "",
                                                    "country": self._country(p)}
                    except Exception:
                        self.data["places"][pid] = {"city": "", "country": ""}
                    time.sleep(1.1)
        with self.lock:
            self.data["artists"][name.casefold()] = {"t": time.time(), "name": name,
                                                     "mbid": best["id"] if best else None, "events": events}

    @staticmethod
    def _country(p):
        codes = (p.get("area") or {}).get("iso-3166-1-codes") or []
        if codes:
            return codes[0]
        return (p.get("address") or "").rsplit(",", 1)[-1].strip()[:40]

    def events(self, names):
        out = []
        with self.lock:
            places = self.data["places"]
            for n in names:
                a = self.data["artists"].get(n.casefold())
                for e in (a or {}).get("events") or []:
                    pl = places.get(e.get("place_id") or "") or {}
                    out.append({**{k: v for k, v in e.items() if k != "place_id"}, "artist": n,
                                "city": pl.get("city") or e.get("area") or "", "country": pl.get("country") or "",
                                "url": f"https://musicbrainz.org/event/{e['id']}"})
        return out


def shows(srv):
    global _shows
    with _shows_lock:
        if _shows is None:
            _shows = Shows(data_path(srv.conf, "shows.json"), user_agent(srv.conf))
        return _shows


# ------------------------------------------------------------------ podcasts (Apple directory + RSS)
_pcache, _plock = {}, threading.Lock()


def _cached(key, ttl, fn):
    with _plock:
        hit = _pcache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _plock:
        if len(_pcache) > 400:
            _pcache.clear()
        _pcache[key] = (time.time(), val)
    return val


def _show(r):
    return {"id": str(r.get("collectionId") or r.get("id") or ""),
            "title": r.get("collectionName") or r.get("name") or "", "author": r.get("artistName") or "",
            "cover": (r.get("artworkUrl600") or r.get("artworkUrl100") or "").replace("100x100", "600x600"),
            "genre": r.get("primaryGenreName") or "", "feed": r.get("feedUrl") or ""}


def _country(conf):
    c = str(conf.get("podcast_country") or "US")
    return c if re.fullmatch(r"[A-Za-z]{2}", c) else "US"


def pod_search(conf, text):
    url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
        {"media": "podcast", "term": text, "limit": 40, "country": _country(conf)})
    return [_show(r) for r in _get(url, user_agent(conf)).get("results") or [] if r.get("feedUrl")]


def pod_lookup(conf, pid):
    def go():
        res = _get(f"https://itunes.apple.com/lookup?id={int(pid)}&entity=podcast&country={_country(conf)}",
                   user_agent(conf)).get("results") or []
        return _show(res[0]) if res else None
    return _cached(("lookup", pid), 86400, go)


def pod_top(conf):
    def go():
        cc = _country(conf).lower()
        d = _get(f"https://rss.applemarketingtools.com/api/v2/{cc}/podcasts/top/50/podcasts.json", user_agent(conf))
        return [{"id": str(r.get("id")), "title": r.get("name") or "", "author": r.get("artistName") or "",
                 "cover": (r.get("artworkUrl100") or "").replace("100x100", "600x600"),
                 "genre": ((r.get("genres") or [{}])[0] or {}).get("name") or "", "feed": ""}
                for r in (d.get("feed") or {}).get("results") or []]
    return _cached("top", 6 * 3600, go)


def _txt(el, *names):
    for n in names:
        x = el.find(n)
        if x is not None and (x.text or "").strip():
            return x.text.strip()
    return ""


def _secs(s):
    s = (s or "").strip()
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    try:
        parts = [int(float(p)) for p in s.split(":")]
    except ValueError:
        return 0
    n = 0
    for p in parts:
        n = n * 60 + p
    return n


def _clean(html, n=600):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()[:n]


def _episode(it):
    enc = it.find("enclosure")
    url = enc.get("url") if enc is not None else ""
    if not url:
        return None
    guid = _txt(it, "guid") or url
    when = _txt(it, "pubDate")
    try:
        ts = parsedate_to_datetime(when).timestamp() if when else 0
    except (TypeError, ValueError):
        ts = 0
    eimg = it.find(f"{ITUNES}image")
    return {"guid": hashlib.sha1(guid.encode()).hexdigest()[:20], "title": _txt(it, "title"),
            "date": ts, "duration": _secs(_txt(it, f"{ITUNES}duration")), "url": url,
            "image": (eimg.get("href") if eimg is not None else "") or "",
            "description": _clean(_txt(it, f"{ITUNES}summary", "description"), 400)}


def parse_feed(stream, limit=5000):
    """Streams the RSS (some feeds are tens of MB): each episode becomes a small dict and its XML is dropped."""
    ch = {"title": "", "description": "", "image": ""}
    eps, depth = [], 0
    for ev, el in ET.iterparse(stream, events=("start", "end")):
        tag = el.tag
        if ev == "start":
            if tag == "item":
                depth += 1
            continue
        if tag == "item":
            depth -= 1
            e = _episode(el)
            el.clear()
            if e:
                eps.append(e)
                if len(eps) >= limit:
                    break
            continue
        if depth == 0:
            if tag == "title" and not ch["title"]:
                ch["title"] = (el.text or "").strip()
            elif tag in ("description", f"{ITUNES}summary") and not ch["description"]:
                ch["description"] = (el.text or "").strip()
            elif tag == f"{ITUNES}image" and not ch["image"]:
                ch["image"] = el.get("href") or ""
    return ch, eps


def pod_feed(conf, pid):
    """All episodes, by Apple id (the feed URL comes from Apple, never from the client). Cached 30 min."""
    def go():
        show = pod_lookup(conf, pid)
        if not show or not show["feed"].startswith(("http://", "https://")):
            raise ValueError("Podcast not found.")
        req = urllib.request.Request(show["feed"], headers={"User-Agent": user_agent(conf)})
        with urllib.request.urlopen(req, timeout=30) as r:
            ch, eps = parse_feed(r)
        eps.sort(key=lambda e: e["date"], reverse=True)
        return {**show, "cover": ch["image"] or show["cover"], "description": _clean(ch["description"], 1200),
                "episodes": eps}
    return _cached(("feed", pid), 1800, go)


# ------------------------------------------------------------------ follows
def _favorites(srv):
    d = srv.jf().call("GET", f"/Users/{g.user['id']}/Items?IncludeItemTypes=MusicArtist&Recursive=true"
                             "&Filters=IsFavorite&SortBy=SortName&Limit=200&Fields=PrimaryImageAspectRatio")
    return [(x["Name"], x["Id"]) for x in (d or {}).get("Items") or []]


def catalog_follows(srv):
    rows = [dict(r) for r in srv.query("SELECT deezer_id, name, cover FROM followed_artists WHERE user_id=? "
                                       "ORDER BY added DESC", (g.user["id"],))]
    if not rows:
        return rows
    try:  # artists that reached the library become Jellyfin favorites
        jf, moved = srv.jf(), []
        for r in rows:
            qs = urllib.parse.urlencode({"searchTerm": r["name"], "Limit": 5})
            hit = [x for x in (jf.call("GET", f"/Artists?{qs}") or {}).get("Items") or []
                   if x.get("Name", "").casefold() == r["name"].casefold()]
            if hit:
                jf.call("POST", f"/Users/{g.user['id']}/FavoriteItems/{hit[0]['Id']}")
                moved.append(r["deezer_id"])
        for d in moved:
            srv.execute("DELETE FROM followed_artists WHERE user_id=? AND deezer_id=?", (g.user["id"], d))
        rows = [r for r in rows if r["deezer_id"] not in moved]
    except Exception as e:  # Jellyfin down: just don't promote now
        print(f"follows: {e}", flush=True)
    return rows


@bp.route("/api/player/follows")
@player_auth
def player_follows():
    return jsonify(artists=catalog_follows(server()))


@bp.route("/api/player/follows/<int:dzid>", methods=["POST", "DELETE"])
@player_auth
def player_follow(dzid):
    srv = server()
    if request.method == "DELETE":
        srv.execute("DELETE FROM followed_artists WHERE user_id=? AND deezer_id=?", (g.user["id"], dzid))
        return jsonify(ok=True)
    b = request.get_json(silent=True) or {}
    name = str(b.get("name") or "").strip()[:200]
    if not name:
        return fail("Missing artist name.", 400)
    cover = str(b.get("cover") or "")[:500]
    if cover and not cover.startswith("https://"):
        cover = ""
    srv.execute("INSERT OR REPLACE INTO followed_artists VALUES (?,?,?,?,?)",
                (g.user["id"], dzid, name, cover, time.time()))
    return jsonify(ok=True)


@bp.route("/api/player/shows")
@player_auth
def player_shows():
    srv = server()
    try:
        favs = _favorites(srv)
    except Exception as e:
        return fail(e)
    have = {n.casefold() for n, _ in favs}
    favs += [(r["name"], f"dzr{r['deezer_id']}") for r in catalog_follows(srv) if r["name"].casefold() not in have]
    names = [n for n, _ in favs]
    sh = shows(srv)
    pending = sh.want(names)
    ids = dict(favs)
    today = date.today().isoformat()
    evs = sh.events(names)
    for e in evs:
        e["artist_id"] = ids.get(e["artist"])
    up = sorted([e for e in evs if e["date"] >= today], key=lambda e: (e["date"], e["time"]))
    past = sorted([e for e in evs if e["date"] < today], key=lambda e: (e["date"], e["time"]), reverse=True)
    return jsonify(upcoming=up, past=past[:400], following=len(names), pending=pending, source="MusicBrainz")


# ------------------------------------------------------------------ podcast routes
def _progress(srv, guids):
    if not guids:
        return {}
    qs = ",".join("?" * len(guids))
    return {r["guid"]: {"pos": r["pos"], "dur": r["dur"], "done": bool(r["done"])} for r in srv.query(
        f"SELECT * FROM podcast_progress WHERE user_id=? AND guid IN ({qs})", (g.user["id"], *guids))}


@bp.route("/api/player/podcasts/search")
@player_auth
def pod_search_route():
    text = (request.args.get("q") or "").strip()[:100]
    if not text:
        return jsonify(results=[])
    conf = server().conf
    try:
        return jsonify(results=_cached(("search", text.casefold()), 3600, lambda: pod_search(conf, text)))
    except Exception as e:
        return fail(e)


@bp.route("/api/player/podcasts/top")
@player_auth
def pod_top_route():
    try:
        return jsonify(results=pod_top(server().conf))
    except Exception as e:
        return fail(e)


@bp.route("/api/player/podcasts/show/<pid>")
@player_auth
def pod_show_route(pid):
    if not pid.isdigit():
        abort(404)
    srv = server()
    try:
        off = max(0, int(request.args.get("offset", 0)))
        lim = min(50, max(1, int(request.args.get("limit", 20))))
    except ValueError:
        abort(400)
    try:
        d = dict(pod_feed(srv.conf, pid))
    except Exception as e:
        return fail(e)
    text = (request.args.get("q") or "").strip().lower()
    eps = [e for e in d["episodes"] if text in e["title"].lower()] if text else d["episodes"]
    page = eps[off:off + lim]
    sub = srv.query("SELECT 1 FROM podcast_subs WHERE user_id=? AND pid=?", (g.user["id"], pid))
    prog = _progress(srv, [e["guid"] for e in page])
    d["episodes"] = [{**e, "progress": prog.get(e["guid"])} for e in page]
    d["total"] = len(eps)
    d["next_offset"] = off + lim if off + lim < len(eps) else None
    d["subscribed"] = bool(sub)
    return jsonify(d)


@bp.route("/api/player/podcasts/subs")
@player_auth
def pod_subs():
    rows = server().query("SELECT pid, title, author, cover FROM podcast_subs WHERE user_id=? ORDER BY added DESC",
                          (g.user["id"],))
    return jsonify(subs=[{"id": r["pid"], "title": r["title"], "author": r["author"], "cover": r["cover"]}
                         for r in rows])


@bp.route("/api/player/podcasts/subs/<pid>", methods=["POST", "DELETE"])
@player_auth
def pod_sub(pid):
    if not pid.isdigit():
        abort(404)
    srv = server()
    if request.method == "DELETE":
        srv.execute("DELETE FROM podcast_subs WHERE user_id=? AND pid=?", (g.user["id"], pid))
        return jsonify(ok=True)
    try:
        s = pod_lookup(srv.conf, pid)
    except Exception as e:
        return fail(e)
    if not s:
        return fail("Podcast not found.", 404)
    srv.execute("INSERT OR REPLACE INTO podcast_subs VALUES (?,?,?,?,?,?)",
                (g.user["id"], pid, s["title"], s["author"], s["cover"], time.time()))
    return jsonify(ok=True)


@bp.route("/api/player/podcasts/latest")
@player_auth
def pod_latest():
    srv = server()
    subs = [r["pid"] for r in srv.query("SELECT pid FROM podcast_subs WHERE user_id=?", (g.user["id"],))]
    out = []
    for pid in subs[:40]:
        try:
            d = pod_feed(srv.conf, pid)
        except Exception:
            continue
        for e in d["episodes"][:5]:
            out.append({**e, "podcast": d["title"], "podcast_id": pid, "cover": e["image"] or d["cover"]})
    out.sort(key=lambda e: e["date"], reverse=True)
    out = out[:40]
    prog = _progress(srv, [e["guid"] for e in out])
    return jsonify(episodes=[{**e, "progress": prog.get(e["guid"])} for e in out])


@bp.route("/api/player/podcasts/progress", methods=["POST"])
@player_auth
def pod_progress():
    b = request.get_json(silent=True) or {}
    guid, pid = str(b.get("guid") or "")[:40], str(b.get("pid") or "")[:20]
    if not re.fullmatch(r"[0-9a-f]{20}", guid):
        return fail("Invalid episode.", 400)
    pos, dur = float(b.get("pos") or 0), float(b.get("dur") or 0)
    done = bool(b.get("done")) or (dur > 0 and pos >= dur - 30)
    server().execute("INSERT OR REPLACE INTO podcast_progress VALUES (?,?,?,?,?,?,?)",
                     (g.user["id"], guid, pid, pos, dur, int(done), time.time()))
    return jsonify(ok=True)
