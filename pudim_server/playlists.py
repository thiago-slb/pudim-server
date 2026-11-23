"""Writing playlists into Jellyfin accounts (private, one per account)."""
import base64
import urllib.request


def find(jf, uid, pid=None, name=None, prefix=None):
    if pid:
        try:
            it = jf.call("GET", f"/Items/{pid}?userId={uid}")
            if it and it.get("Type") == "Playlist":
                return it
        except Exception:
            pass
    its = (jf.call("GET", f"/Items?IncludeItemTypes=Playlist&Recursive=true&userId={uid}") or {}).get("Items", [])
    for p in its:
        n = p.get("Name") or ""
        if (name and n == name) or (prefix and n.startswith(prefix)):
            return p
    return None


def write(jf, uid, name, ids, pid=None, prefix=None, overview=None):
    """Creates or rewrites the playlist (keeping its id, so shortcuts and offline copies keep working)."""
    old = find(jf, uid, pid=pid, name=name, prefix=prefix)
    if not old:
        return jf.call("POST", "/Playlists", {"Name": name, "Ids": ids, "UserId": uid, "MediaType": "Audio",
                                               "IsPublic": False})["Id"]
    pid = old["Id"]
    cur = (jf.call("GET", f"/Playlists/{pid}/Items?userId={uid}") or {}).get("Items", [])
    entry = [o.get("PlaylistItemId") for o in cur if o.get("PlaylistItemId")]
    for i in range(0, len(entry), 100):
        jf.call("DELETE", f"/Playlists/{pid}/Items?entryIds=" + ",".join(entry[i:i + 100]))
    for i in range(0, len(ids), 100):
        jf.call("POST", f"/Playlists/{pid}/Items?userId={uid}&ids=" + ",".join(ids[i:i + 100]))
    dto = jf.call("GET", f"/Items/{pid}?userId={uid}")
    if dto.get("Name") != name or (overview is not None and dto.get("Overview") != overview):
        dto["Name"] = name
        if overview is not None:
            dto["Overview"] = overview
        jf.call("POST", f"/Items/{pid}", dto)
    return pid


def valid_ids(jf, uid, ids):
    """Only the ids that still exist in the library (play history keeps tracks that were removed)."""
    ok = set()
    ids = list(dict.fromkeys(i for i in ids if i))
    for i in range(0, len(ids), 100):
        r = jf.call("GET", f"/Items?userId={uid}&Ids={','.join(ids[i:i + 100])}&EnableImages=false&Fields=")
        ok |= {x["Id"] for x in (r or {}).get("Items", [])}
    return [i for i in ids if i in ok]


def set_cover(jf, pid, url):
    """Uploads a JPEG cover from ``url`` (best effort)."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "pudim-server"}),
                                    timeout=20) as r:
            img = r.read(5 * 1024 * 1024)
        if img[:3] == b"\xff\xd8\xff":
            jf.call("POST", f"/Items/{pid}/Images/Primary", raw=base64.b64encode(img), ctype="image/jpeg")
    except Exception:
        pass
