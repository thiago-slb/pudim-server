"""Small Jellyfin HTTP client (server API key) and token check for app requests."""
import json
import time
import urllib.parse
import urllib.request

from . import __version__

UA = f"pudim-server/{__version__}"


class Jellyfin:
    def __init__(self, url, key, user=None):
        self.url, self.key, self.user = url.rstrip("/"), key, (user or "").casefold()

    @classmethod
    def from_conf(cls, conf, user=None):
        return cls(conf["jellyfin_url"], conf.get("jellyfin_api_key"), user)

    def call(self, method, path, body=None, raw=None, ctype="application/json", timeout=60):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.url + path, method=method, data=data, headers={
            "Authorization": f'MediaBrowser Token="{self.key}"', "Content-Type": ctype, "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read()
        return json.loads(txt) if txt[:1] in (b"{", b"[") else None

    def get_bytes(self, path, timeout=20):
        req = urllib.request.Request(self.url + path, headers={
            "Authorization": f'MediaBrowser Token="{self.key}"', "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

    def users(self, include_disabled=False):
        return [u for u in (self.call("GET", "/Users") or [])
                if include_disabled or not (u.get("Policy") or {}).get("IsDisabled")]

    def user_id(self):
        """Id of ``self.user`` (by name); without a name, the first administrator."""
        users = self.users(include_disabled=True)
        mine = [u for u in users if self.user and (u.get("Name") or "").casefold() == self.user]
        if mine:
            return mine[0]["Id"]
        admins = [u for u in users if (u.get("Policy") or {}).get("IsAdministrator")]
        return (admins or users)[0]["Id"]


class TokenCache:
    """Maps an app's Jellyfin access token to the account it belongs to (``/Users/Me``), cached 5 minutes."""

    def __init__(self, url, ttl=300):
        self.url, self.ttl, self.hits = url.rstrip("/"), ttl, {}

    def whoami(self, token):
        now = time.time()
        hit = self.hits.get(token)
        if hit and hit[0] > now:
            return hit[1]
        req = urllib.request.Request(self.url + "/Users/Me", headers={
            "Authorization": f'MediaBrowser Token="{token}"', "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=10) as r:
            me = json.loads(r.read())
        if len(self.hits) > 500:
            self.hits.clear()
        user = {"id": me["Id"], "name": me.get("Name") or ""}
        self.hits[token] = (now + self.ttl, user)
        return user

    def forget_user(self, uid):
        for tok in [t for t, v in self.hits.items() if v[1]["id"] == uid]:
            self.hits.pop(tok, None)


def quote(s):
    return urllib.parse.quote(s or "")
