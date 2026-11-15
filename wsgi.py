"""Entry point for WSGI hosts such as PythonAnywhere.

On PythonAnywhere, replace the contents of your web app's WSGI configuration
file (Web tab -> "WSGI configuration file") with:

    import sys
    sys.path.insert(0, "/home/YOUR_USERNAME/buzzword-polymarket")
    from wsgi import application

Not needed on the Raspberry Pi; there you use `python3 manage.py serve`.
"""
import os

# Hosted online behind HTTPS: only send login cookies over HTTPS.
os.environ.setdefault("BUZZ_SECURE_COOKIES", "1")
os.environ.setdefault("BUZZ_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "buzz.db"))

from buzz import db, server  # noqa: E402

_conn = db.connect()
db.init(_conn)
_conn.close()

application = server.wsgi_app
