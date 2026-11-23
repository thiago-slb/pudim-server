"""Configuration: a JSON file plus environment variables (environment wins).

The file is read from ``$PUDIM_CONFIG`` (default ``./config.json``). Every key can also be set as an environment
variable named ``PUDIM_<KEY>`` in upper case, e.g. ``PUDIM_JELLYFIN_URL``. Lists (``catalog_stream_command``) are
given as JSON in the environment.
"""
import json
import os
from pathlib import Path

DEFAULTS = {
    "jellyfin_url": "",
    "jellyfin_api_key": "",
    "data_dir": "./data",
    "language": "en",
    "timezone": "",
    "host": "127.0.0.1",
    "port": 8097,
    "threads": 16,
    "background_jobs": True,
    "audiomuse_url": "",
    "lastfm_api_key": "",
    "lastfm_api_secret": "",
    "updates_dir": "",
    "catalog_stream_command": [],
    "podcast_country": "US",
    "contact": "",
}

_LISTS = {"catalog_stream_command"}
_INTS = {"port", "threads"}
_BOOLS = {"background_jobs"}


class ConfigError(RuntimeError):
    pass


def _coerce(key, raw):
    if key in _LISTS:
        try:
            val = json.loads(raw)
        except ValueError:
            val = raw.split()
        return val if isinstance(val, list) else [str(val)]
    if key in _INTS:
        return int(raw)
    if key in _BOOLS:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return raw


def load(path=None, overrides=None):
    """Returns the merged configuration dict. ``overrides`` (used by tests) wins over everything."""
    conf = dict(DEFAULTS)
    path = Path(path or os.environ.get("PUDIM_CONFIG") or "config.json")
    if path.exists():
        try:
            conf.update(json.loads(path.read_text(encoding="utf-8")))
        except ValueError as e:
            raise ConfigError(f"{path}: invalid JSON ({e})")
    for key in DEFAULTS:
        env = os.environ.get("PUDIM_" + key.upper())
        if env is not None and env != "":
            conf[key] = _coerce(key, env)
    conf.update(overrides or {})
    conf["jellyfin_url"] = str(conf.get("jellyfin_url") or "").rstrip("/")
    conf["data_dir"] = str(Path(conf["data_dir"]).expanduser().resolve())
    Path(conf["data_dir"]).mkdir(parents=True, exist_ok=True)
    if not conf["jellyfin_url"]:
        raise ConfigError("jellyfin_url is not set (config.json or PUDIM_JELLYFIN_URL).")
    return conf


def data_path(conf, *parts):
    return Path(conf["data_dir"]).joinpath(*parts)
