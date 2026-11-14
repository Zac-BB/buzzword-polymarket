"""Minimal HTTP server + router on the standard library (no pip installs)."""
import hmac
import json
import mimetypes
import os
import re
import sys
import traceback
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import auth, db, market, views

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
SECURE_COOKIES = os.environ.get("BUZZ_SECURE_COOKIES") == "1"
MAX_BODY = 64 * 1024

ROUTES = []


def route(method, pattern, login=True, admin=False):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn, login, admin))
        return fn
    return deco


class Response:
    def __init__(self, body="", status=200, ctype="text/html; charset=utf-8", headers=None):
        self.body = body.encode() if isinstance(body, str) else body
        self.status = status
        self.headers = [("Content-Type", ctype)] + (headers or [])


def redirect(location, flash=None, kind="ok", cookie=None):
    headers = [("Location", location)]
    if flash:
        headers.append(("Set-Cookie", _cookie("flash", quote(json.dumps([kind, flash])), max_age=60)))
    if cookie:
        headers.append(("Set-Cookie", cookie))
    return Response("", 303, headers=headers)


def _cookie(name, value, max_age=None):
    c = f"{name}={value}; Path=/; HttpOnly; SameSite=Lax"
    if max_age is not None:
        c += f"; Max-Age={max_age}"
    if SECURE_COOKIES:
        c += "; Secure"
    return c


class Request:
    def __init__(self, handler, conn):
        self.conn = conn
        self.method = handler.command
        parts = urlsplit(handler.path)
        self.path = unquote(parts.path)
        self.query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        jar = cookies.SimpleCookie()
        try:
            jar.load(handler.headers.get("Cookie", ""))
        except cookies.CookieError:
            pass
        self.cookies = {k: m.value for k, m in jar.items()}
        self.form = {}
        if self.method == "POST":
            n = int(handler.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("Request too large")
            raw = handler.rfile.read(n).decode("utf-8", "replace")
            self.form = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}
        self.token = self.cookies.get("session")
        self.user, self.csrf = auth.load_session(conn, self.token)
        self.flash = None
        if "flash" in self.cookies:
            try:
                self.flash = tuple(json.loads(unquote(self.cookies["flash"])))[:2]
            except (ValueError, TypeError):
                pass

    def refresh_user(self):
        self.user, self.csrf = auth.load_session(self.conn, self.token)


class Handler(BaseHTTPRequestHandler):
    server_version = "BuzzMarket"
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def log_message(self, fmt, *args):
        if os.environ.get("BUZZ_QUIET") != "1":
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, resp, clear_flash=False):
        self.send_response(resp.status)
        for k, v in resp.headers:
            self.send_header(k, v)
        if clear_flash:
            self.send_header("Set-Cookie", _cookie("flash", "", max_age=0))
        self.send_header("Content-Length", str(len(resp.body)))
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(resp.body)

    def _dispatch(self):
        path = urlsplit(self.path).path
        if path.startswith("/static/"):
            return self._send(static(path[len("/static/"):]))
        conn = db.connect()
        try:
            try:
                req = Request(self, conn)
            except ValueError as ex:
                return self._send(Response(str(ex), 413, "text/plain"))
            resp = handle(req)
            self._send(resp, clear_flash=req.flash is not None and resp.status == 200)
        except Exception:
            traceback.print_exc()
            self._send(Response("Something went wrong.", 500, "text/plain"))
        finally:
            conn.close()


def static(name):
    full = os.path.realpath(os.path.join(STATIC_DIR, name))
    if not full.startswith(os.path.realpath(STATIC_DIR) + os.sep) or not os.path.isfile(full):
        return Response("Not found", 404, "text/plain")
    with open(full, "rb") as f:
        data = f.read()
    ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
    return Response(data, 200, ctype, [("Cache-Control", "public, max-age=3600")])


def handle(req):
    for method, pattern, fn, login, admin in ROUTES:
        if method != req.method:
            continue
        m = pattern.match(req.path)
        if not m:
            continue
        if login and not req.user:
            return redirect("/login") if req.method == "GET" else Response("Log in first", 403, "text/plain")
        if admin and not req.user["is_admin"]:
            return Response(views.error_page(req, 403, "Admins only."), 403)
        if req.method == "POST" and req.user:
            if not hmac.compare_digest(req.form.get("csrf", ""), req.csrf or ""):
                return Response(views.error_page(req, 403, "Form expired - go back and try again."), 403)
        try:
            return fn(req, *m.groups())
        except (market.MarketError, auth.AuthError) as ex:
            if req.method == "GET":
                return Response(views.error_page(req, 404, str(ex)), 404)
            return redirect(_back(req), str(ex), "error")
    return Response(views.error_page(req, 404, "Page not found."), 404)


def _back(req):
    """Where to send someone after a POST: the page they came from."""
    p = req.path
    if p.startswith("/admin"):
        return "/admin"
    m = re.match(r"^/m/(\d+)/", p)
    if m:
        return _safe_next(req.form.get("next"), f"/m/{m.group(1)}")
    return {"/me/password": "/me", "/suggest": "/suggest"}.get(p, "/")


def _safe_next(target, default):
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return default


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        raise market.MarketError("Bad number.")


# ---------------------------------------------------------------- auth routes

@route("GET", "/login", login=False)
def get_login(req):
    if req.user:
        return redirect("/")
    return Response(views.login_page(req))


@route("POST", "/login", login=False)
def post_login(req):
    try:
        user = auth.authenticate(req.conn, req.form.get("username"), req.form.get("password"))
    except auth.AuthError as ex:
        req.flash = ("error", str(ex))
        return Response(views.login_page(req, req.form.get("username", "")), 401)
    with db.transaction(req.conn):
        token = auth.start_session(req.conn, user["id"])
    return redirect("/", cookie=_cookie("session", token, max_age=auth.SESSION_DAYS * 86400))


@route("GET", "/register", login=False)
def get_register(req):
    if req.user:
        return redirect("/")
    return Response(views.register_page(req))


@route("POST", "/register", login=False)
def post_register(req):
    f = req.form
    code = db.get_setting(req.conn, "invite_code") or ""
    try:
        if not code:
            raise auth.AuthError("Self sign-up is turned off. Ask the admin for an account.")
        if not hmac.compare_digest(f.get("invite", "").strip(), code):
            raise auth.AuthError("Wrong invite code.")
        with db.transaction(req.conn):
            uid = auth.create_user(req.conn, f.get("username"), f.get("display_name"), f.get("password"))
            token = auth.start_session(req.conn, uid)
    except auth.AuthError as ex:
        req.flash = ("error", str(ex))
        return Response(views.register_page(req, f), 400)
    return redirect("/", "Welcome! You start with some points - go place a bet.",
                    cookie=_cookie("session", token, max_age=auth.SESSION_DAYS * 86400))


@route("POST", "/logout")
def post_logout(req):
    with db.transaction(req.conn):
        auth.end_session(req.conn, req.token)
    return redirect("/login", cookie=_cookie("session", "", max_age=0))


# -------------------------------------------------------------- user routes

@route("GET", "/")
def get_home(req):
    return Response(views.home_page(req))


@route("GET", r"/m/(\d+)")
def get_market(req, mid):
    m = market.get_market(req.conn, _int(mid))
    if m["status"] == "rejected" and not req.user["is_admin"]:
        return Response(views.error_page(req, 404, "Market not found."), 404)
    return Response(views.market_page(req, m))


@route("POST", r"/m/(\d+)/bet")
def post_bet(req, mid):
    with db.transaction(req.conn):
        old, new = market.place_bet(req.conn, _int(mid), req.user, req.form.get("side"), req.form.get("stake"))
    side = req.form.get("side", "").upper()
    return redirect(f"/m/{mid}", f"Bet placed: {side} {views.fmt_line(old)}. Line is now {views.fmt_line(new)}.")


@route("POST", r"/m/(\d+)/guess")
def post_guess(req, mid):
    with db.transaction(req.conn):
        market.submit_guess(req.conn, _int(mid), req.user, req.form.get("guess"))
    return redirect(_safe_next(req.form.get("next"), "/"), "Guess saved. It stays hidden from other players.")


@route("POST", r"/m/(\d+)/vote")
def post_vote(req, mid):
    with db.transaction(req.conn):
        market.toggle_vote(req.conn, _int(mid), req.user["id"])
    return redirect("/suggestions")


@route("GET", "/suggest")
def get_suggest(req):
    return Response(views.suggest_page(req))


@route("POST", "/suggest")
def post_suggest(req):
    f = req.form
    try:
        with db.transaction(req.conn):
            market.suggest(req.conn, req.user["id"], f.get("phrase"), f.get("description"), f.get("meeting"))
    except market.MarketError as ex:
        req.flash = ("error", str(ex))
        return Response(views.suggest_page(req, f), 400)
    return redirect("/suggestions", "Thanks! The admin will review your suggestion.")


@route("GET", "/suggestions")
def get_suggestions(req):
    return Response(views.suggestions_page(req))


@route("GET", "/leaderboard")
def get_leaderboard(req):
    return Response(views.leaderboard_page(req))


@route("GET", "/me")
def get_me(req):
    return Response(views.me_page(req))


@route("POST", "/me/password")
def post_password(req):
    if not auth.check_password(req.form.get("current", ""), req.user["pw_hash"]):
        raise auth.AuthError("Current password is wrong.")
    with db.transaction(req.conn):
        auth.set_password(req.conn, req.user["id"], req.form.get("new"))
        token = auth.start_session(req.conn, req.user["id"])
    return redirect("/me", "Password changed.", cookie=_cookie("session", token, max_age=auth.SESSION_DAYS * 86400))


# ------------------------------------------------------------- admin routes

@route("GET", "/admin", admin=True)
def get_admin(req):
    return Response(views.admin_page(req))


ADMIN_MARKET_ACTIONS = {
    "approve": (market.approve, "Approved - trusted users can now set the opening line."),
    "reject": (market.reject, "Rejected."),
    "lock": (market.lock, "Betting locked."),
    "reopen": (market.reopen, "Betting reopened."),
    "cancel": (market.cancel, "Cancelled; all bets refunded."),
}


@route("POST", r"/admin/m/(\d+)/(approve|reject|lock|reopen|cancel)", admin=True)
def post_admin_market(req, mid, action):
    fn, msg = ADMIN_MARKET_ACTIONS[action]
    with db.transaction(req.conn):
        fn(req.conn, _int(mid))
    return redirect(f"/admin#m{mid}", msg)


@route("POST", r"/admin/m/(\d+)/open", admin=True)
def post_admin_open(req, mid):
    with db.transaction(req.conn):
        market.open_market(req.conn, _int(mid), req.form.get("line"))
    return redirect(f"/m/{mid}", "Betting is open!")


@route("POST", r"/admin/m/(\d+)/resolve", admin=True)
def post_admin_resolve(req, mid):
    with db.transaction(req.conn):
        results = market.resolve(req.conn, _int(mid), req.form.get("actual"))
    winners = sum(1 for r, _ in results.values() if r == "win")
    return redirect(f"/m/{mid}", f"Resolved. {winners} winning bet(s) paid out.")


@route("POST", "/admin/markets", admin=True)
def post_admin_create_market(req):
    f = req.form
    with db.transaction(req.conn):
        mid = market.suggest(req.conn, req.user["id"], f.get("phrase"), f.get("description"), f.get("meeting"))
        market.approve(req.conn, mid)
        if f.get("line", "").strip():
            market.open_market(req.conn, mid, f.get("line"))
    return redirect("/admin", "Market created.")


@route("POST", "/admin/users", admin=True)
def post_admin_create_user(req):
    f = req.form
    with db.transaction(req.conn):
        auth.create_user(req.conn, f.get("username"), f.get("display_name"), f.get("password"),
                         is_trusted=f.get("trusted") == "1")
    return redirect("/admin", f"Added @{f.get('username', '').strip()}. Give them their password.")


@route("POST", r"/admin/u/(\d+)/(trusted|disable)", admin=True)
def post_admin_user_flag(req, uid, flag):
    uid = _int(uid)
    col = {"trusted": "is_trusted", "disable": "disabled"}[flag]
    if flag == "disable" and uid == req.user["id"]:
        raise auth.AuthError("You can't disable yourself.")
    with db.transaction(req.conn):
        req.conn.execute(f"UPDATE users SET {col} = 1 - {col} WHERE id = ?", (uid,))
        if flag == "disable":
            req.conn.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
    return redirect("/admin", "User updated.")


@route("POST", r"/admin/u/(\d+)/balance", admin=True)
def post_admin_balance(req, uid):
    with db.transaction(req.conn):
        market.adjust_balance(req.conn, _int(uid), req.form.get("amount"), "Admin adjustment")
    return redirect("/admin", "Balance adjusted.")


@route("POST", r"/admin/u/(\d+)/password", admin=True)
def post_admin_password(req, uid):
    uid = _int(uid)
    with db.transaction(req.conn):
        auth.set_password(req.conn, uid, req.form.get("password"))
        cookie = None
        if uid == req.user["id"]:
            cookie = _cookie("session", auth.start_session(req.conn, uid), max_age=auth.SESSION_DAYS * 86400)
    return redirect("/admin", "Password set.", cookie=cookie)


@route("POST", "/admin/settings", admin=True)
def post_admin_settings(req):
    f = req.form
    with db.transaction(req.conn):
        db.set_setting(req.conn, "invite_code", f.get("invite_code", "").strip())
        for k, lo in (("starting_balance", 0), ("max_bet", 0), ("liquidity", 1),
                      ("early_bets", 0), ("early_max_bet", 0)):
            v = _int(f.get(k))
            if v < lo:
                raise market.MarketError(f"{k} must be at least {lo}.")
            db.set_setting(req.conn, k, v)
    return redirect("/admin", "Settings saved.")


def serve(host="0.0.0.0", port=8000):
    conn = db.connect()
    db.init(conn)
    if not conn.execute("SELECT 1 FROM users WHERE is_admin = 1").fetchone():
        print("No admin account yet. Run:  python3 manage.py create-admin", file=sys.stderr)
    conn.close()
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    print(f"BuzzMarket running on http://{host}:{port}", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
