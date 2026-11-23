"""App updates, server info, and the download routes this server does not implement.

App updates (optional): put ``pudim-version.json`` plus the files it names in ``updates_dir``::

    {"build": 7, "notes": "...", "apk": {"sha256": "...", "size": 123}, "windows": {"sha256": "...", "size": 456}}

with ``pudimmusic.apk`` and ``pudimmusic-setup.exe`` next to it. The app compares ``build`` with its own and
downloads from ``/api/player/update/<apk|windows>``.

Downloads: the Pudim app can ask an extras server to download missing music (catalog track/album/artist,
higher quality versions). That depends on a separate download pipeline, so it is not part of this project; those
routes answer 501 with a JSON error the app shows, and ``/api/player/jobs`` is always empty.
"""
import json
import time
from pathlib import Path

from flask import Blueprint, abort, jsonify, send_file

from . import __version__
from .core import fail, player_auth, server

bp = Blueprint("extras", __name__)
UPDATE_FILES = {"apk": "pudimmusic.apk", "windows": "pudimmusic-setup.exe"}
NO_DOWNLOADS = "Downloads are not available on this server."


def _updates_dir():
    d = server().conf.get("updates_dir")
    return Path(d) if d else None


@bp.route("/api/player/update")
@player_auth
def player_update():
    d, base = {}, _updates_dir()
    if base:
        try:
            d = json.loads((base / "pudim-version.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
    return jsonify(build=int(d.get("build") or 0), notes=d.get("notes") or "", apk=d.get("apk"),
                   windows=d.get("windows"))


@bp.route("/api/player/update/<kind>")
@player_auth
def player_update_file(kind):
    base, name = _updates_dir(), UPDATE_FILES.get(kind)
    if not base or not name or not (base / name).is_file():
        abort(404)
    return send_file(base / name, mimetype="application/octet-stream", as_attachment=True, download_name=name,
                     max_age=0, conditional=True)


@bp.route("/api/player/server")
@player_auth
def player_server():
    """What this server offers, so a client can hide what is not available."""
    conf = server().conf
    return jsonify(name="pudim-server", version=__version__, features={
        "catalog": True, "social": True, "jam": True, "collab": True, "recap": True, "shows": True,
        "podcasts": True, "radar": True, "daylist": True, "blend": True,
        "weekly_mix": bool(conf.get("audiomuse_url")),
        "lastfm": bool(conf.get("lastfm_api_key") and conf.get("lastfm_api_secret")),
        "catalog_playback": bool(conf.get("catalog_stream_command")),
        "updates": bool(conf.get("updates_dir")), "downloads": False, "discovery": False})


@bp.route("/api/player/jobs")
@player_auth
def player_jobs():
    return jsonify(jobs=[], now=time.time())


@bp.route("/api/player/jobs/<jid>")
@player_auth
def player_job(jid):
    abort(404)


@bp.route("/api/player/download", methods=["POST"])
@bp.route("/api/player/flac", methods=["POST"])
@bp.route("/api/player/improve", methods=["POST"])
@bp.route("/api/player/albums/plan", methods=["POST"])
@bp.route("/api/player/albums/get", methods=["POST"])
@bp.route("/api/player/jobs/<jid>/cancel", methods=["POST"])
@bp.route("/api/player/jobs/<jid>/retry", methods=["POST"])
@bp.route("/api/player/jobs/<jid>", methods=["DELETE"])
@player_auth
def no_downloads(jid=None):
    return fail(NO_DOWNLOADS, 501)


@bp.route("/api/player/albums/plan/<token>")
@player_auth
def no_plan(token):
    return fail(NO_DOWNLOADS, 501)


@bp.route("/api/player/quality")
@player_auth
def player_quality():
    return jsonify(albums=[], poor=0)


@bp.route("/healthz")
def healthz():
    return jsonify(ok=True)
