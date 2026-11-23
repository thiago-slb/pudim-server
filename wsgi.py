"""WSGI entry point, e.g. ``waitress-serve --port 8097 wsgi:app`` or ``gunicorn --threads 16 wsgi:app``.

Use a single process (threads, not workers): Jam sessions and some caches live in memory.
"""
from pudim_server import create_app

app = create_app()
