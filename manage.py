#!/usr/bin/env python3
"""BuzzMarket command line.

  python3 manage.py serve [--host 0.0.0.0] [--port 8000]
  python3 manage.py create-admin            (asks for name / username / password)
  python3 manage.py reset-password USERNAME
  python3 manage.py backup [FILE]           (safe copy of the database while running)
"""
import argparse
import getpass
import os
import sys
import time

from buzz import auth, db, server


def prompt_password():
    pw = os.environ.get("BUZZ_PASSWORD") or getpass.getpass("Password: ")
    if not os.environ.get("BUZZ_PASSWORD") and getpass.getpass("Again: ") != pw:
        sys.exit("Passwords don't match.")
    return pw


def main():
    ap = argparse.ArgumentParser(description="BuzzMarket")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default=os.environ.get("BUZZ_HOST", "0.0.0.0"))
    s.add_argument("--port", type=int, default=int(os.environ.get("BUZZ_PORT", "8000")))
    a = sub.add_parser("create-admin")
    a.add_argument("--username")
    a.add_argument("--name")
    r = sub.add_parser("reset-password")
    r.add_argument("username")
    b = sub.add_parser("backup")
    b.add_argument("file", nargs="?")
    args = ap.parse_args()

    if args.cmd == "serve":
        return server.serve(args.host, args.port)

    conn = db.connect()
    db.init(conn)
    if args.cmd == "create-admin":
        username = args.username or input("Admin username: ").strip()
        name = args.name or input("Your name: ").strip()
        pw = prompt_password()
        try:
            with db.transaction(conn):
                auth.create_user(conn, username, name, pw, is_admin=True, is_trusted=True)
        except auth.AuthError as ex:
            sys.exit(str(ex))
        print(f"Admin @{username} created.")
    elif args.cmd == "reset-password":
        row = conn.execute("SELECT id FROM users WHERE username = ?", (args.username,)).fetchone()
        if not row:
            sys.exit("No such user.")
        pw = prompt_password()
        try:
            with db.transaction(conn):
                auth.set_password(conn, row["id"], pw)
        except auth.AuthError as ex:
            sys.exit(str(ex))
        print("Password updated.")
    elif args.cmd == "backup":
        out = args.file or time.strftime("buzz-backup-%Y%m%d-%H%M%S.db")
        dest = db.connect(out)
        conn.backup(dest)
        dest.close()
        print(f"Backed up to {out}")


if __name__ == "__main__":
    main()
