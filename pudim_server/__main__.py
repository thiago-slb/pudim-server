"""``python -m pudim_server [--config config.json]``: runs the server with waitress."""
import argparse

from . import create_app


def main():
    ap = argparse.ArgumentParser(prog="pudim-server")
    ap.add_argument("--config", help="path to config.json (default: $PUDIM_CONFIG or ./config.json)")
    args = ap.parse_args()
    app = create_app(args.config)
    conf = app.extensions["pudim"].conf
    from waitress import serve
    print(f"pudim-server listening on {conf['host']}:{conf['port']}", flush=True)
    serve(app, host=conf["host"], port=int(conf["port"]), threads=int(conf["threads"]))


if __name__ == "__main__":
    main()
