"""Title/artist normalisation and matching catalog tracks against the Jellyfin library."""
import re
import unicodedata

LIVE = re.compile(r"\b(ao vivo|live|en vivo|unplugged|ac[uú]stico|acoustic)\b", re.I)
NOISE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]|\s+-\s+.*$|\s+feat\.?.*$|\s+ft\.?\s.*$", re.I)


def norm(s):
    """Lower case, no accents, letters and digits only (keeps CJK and Hangul)."""
    s = unicodedata.normalize("NFKD", (s or "").casefold())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^0-9a-z぀-ヿ㐀-鿿가-힯]+", "", s)


def base_title(t):
    """'Song (Live) - Remastered' -> ('song', True)"""
    live = bool(LIVE.search(t or ""))
    b = NOISE.sub("", t or "")
    b = LIVE.sub("", b)
    return norm(b) or norm(t), live


def same_artist(a, arts):
    return a in arts or any(a and (a in x or x in a) for x in arts if len(x) > 3)


def _album_like(a, b):
    return a == b or (min(len(a), len(b)) >= 5 and (a in b or b in a))


def _is_latin(s):
    letters = [c for c in s or "" if c.isalpha()]
    return sum(c.isascii() or unicodedata.name(c, "").startswith("LATIN") for c in letters) >= len(letters) / 2


def index_items(items):
    """Library tracks (Jellyfin ``Audio`` items) grouped by normalised title and by artist."""
    by, by_artist = {}, {}
    for it in items:
        b, live = base_title(it.get("Name"))
        arts = {norm(a) for a in (it.get("Artists") or []) + [it.get("AlbumArtist") or ""] if a}
        e = {"id": it["Id"], "name": it.get("Name") or "", "arts": arts, "live": live,
             "album": norm(it.get("Album")), "full": norm(it.get("Name")),
             "secs": (it.get("RunTimeTicks") or 0) / 1e7}
        by.setdefault(b, []).append(e)
        for x in arts:
            by_artist.setdefault(x, []).append(e)
    return {"title": by, "artist": by_artist}


def match(track, idx):
    """Jellyfin id of ``track`` ({title, artist, album, duration}) or None."""
    b, live = base_title(track["title"])
    a = norm(track["artist"])
    cands = [c for c in idx["title"].get(b, []) if same_artist(a, c["arts"])]
    full, alb = norm(track["title"]), norm(track.get("album"))
    if cands:
        cands.sort(key=lambda c: (c["live"] != live, c["full"] != full, c["album"] != alb))
        return cands[0]["id"]
    # Title in another script (e.g. Hangul vs. English): same artist, same album, same length.
    dur = track.get("duration") or 0
    if not dur:
        return None
    pool = [c for x, lst in idx["artist"].items() if a and (x == a or (len(x) > 3 and (a in x or x in a)))
            for c in lst if c["secs"]]
    latin = _is_latin(track["title"])
    near = [c for c in pool if abs(c["secs"] - dur) <= 2 and _album_like(alb, c["album"])
            and _is_latin(c["name"]) != latin]
    return near[0]["id"] if len({c["id"] for c in near}) == 1 else None
