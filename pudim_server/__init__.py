"""Pudim server: the optional companion service for the Pudim music player (Jellyfin)."""
import threading

__version__ = "0.1.0"


def create_app(config_path=None, overrides=None):
    """Builds the Flask app. ``overrides`` is merged over the config file and environment (tests use it)."""
    from flask import Flask, jsonify

    from . import catalog, config, extras, live, media, mixes, recap, social
    from .core import Server

    conf = config.load(config_path, overrides)
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 310 * 10 ** 6  # Spotify history uploads
    srv = Server(conf)
    app.extensions["pudim"] = srv
    for mod in (catalog, social, mixes, live, media, recap, extras):
        app.register_blueprint(mod.bp)

    @app.errorhandler(401)
    def unauthorized(e):
        return jsonify(error="Not signed in (send the Jellyfin token)."), 401

    @app.errorhandler(404)
    def not_found(e):
        return jsonify(error="Not found."), 404

    if conf.get("background_jobs") and conf.get("jellyfin_api_key"):
        for target in (catalog.warm, mixes.monitor, recap.monitor):
            threading.Thread(target=target, args=(srv,), daemon=True).start()
    return app
