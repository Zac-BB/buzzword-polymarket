"""Minimal HTTP server + router on the standard library (no pip installs)."""
import hmac
import json
import mimetypes
import os
import re
import socket
import sys
import traceback
from http import HTTPStatus, cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import auth, db, market, views

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
SECURE_COOKIES = os.environ.get("BUZZ_SECURE_COOKIES") == "1"
MAX_BODY = 64 * 1024
# When the Pi runs its own Wi-Fi hotspot, set this to the Pi's address there
# (e.g. 10.42.0.1). Any request for another host name -- like the "am I online?"
# checks phones make when joining Wi-Fi -- is redirected to the join page, so
# phones pop the app up automatically (captive portal).
CAPTIVE_HOST = os.environ.get("BUZZ_CAPTIVE_HOST", "").strip()


def _own_hosts():
    name = socket.gethostname().lower()
    return {CAPTIVE_HOST, "localhost", "127.0.0.1", "::1", name, name + ".local"}


def captive_redirect(host_header):
    """Return a redirect URL if this request is for some other site, else None."""
    if not CAPTIVE_HOST:
        return None
    host = (host_header or "").strip().lower()
    host = host[1:host.index("]")] if host.startswith("[") and "]" in host else host.rsplit(":", 1)[0]
    if host in _own_hosts():
        return None
    return f"http://{CAPTIVE_HOST}/join"

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
    """Built from plain pieces so both the built-in server and WSGI can use it.
    `header(name)` returns a header value or None; `read_body(n)` returns bytes."""

    def __init__(self, conn, method, raw_path, header, read_body, client_ip=""):
        self.conn = conn
        self.method = method
        self.client_ip = client_ip
        parts = urlsplit(raw_path)
        self.path = unquote(parts.path)
        self.query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        jar = cookies.SimpleCookie()
        try:
            jar.load(header("Cookie") or "")
        except cookies.CookieError:
            pass
        self.cookies = {k: m.value for k, m in jar.items()}
        self.form = {}
        if self.method == "POST":
            n = int(header("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("Request too large")
            raw = read_body(n).decode("utf-8", "replace")
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


SECURITY_HEADERS = [("X-Frame-Options", "DENY"), ("X-Content-Type-Options", "nosniff"),
                    ("Referrer-Policy", "same-origin")]


def process(method, raw_path, header, read_body, client_ip=""):
    """Handle one request from any server. Returns a Response with all headers set."""
    resp, clear_flash = _process(method, raw_path, header, read_body, client_ip)
    if clear_flash:
        resp.headers.append(("Set-Cookie", _cookie("flash", "", max_age=0)))
    resp.headers.extend(SECURITY_HEADERS)
    return resp


def _process(method, raw_path, header, read_body, client_ip):
    if method not in ("GET", "POST", "HEAD"):
        return Response("Method not allowed", 405, "text/plain"), False
    if method == "HEAD":
        method = "GET"
    target = captive_redirect(header("Host"))
    if target:
        return Response("", 302, headers=[("Location", target), ("Cache-Control", "no-store")]), False
    path = urlsplit(raw_path).path
    if path.startswith("/static/"):
        return static(path[len("/static/"):]), False
    conn = db.connect()
    try:
        try:
            req = Request(conn, method, raw_path, header, read_body, client_ip)
        except ValueError as ex:
            return Response(str(ex), 413, "text/plain"), False
        resp = handle(req)
        return resp, req.flash is not None and resp.status == 200
    except Exception:
        traceback.print_exc()
        return Response("Something went wrong.", 500, "text/plain"), False
    finally:
        conn.close()


class Handler(BaseHTTPRequestHandler):
    """The built-in server (used on the Pi)."""
    server_version = "BuzzMarket"
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_HEAD(self):
        self._dispatch(head=True)

    def log_message(self, fmt, *args):
        if os.environ.get("BUZZ_QUIET") != "1":
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _dispatch(self, head=False):
        resp = process(self.command, self.path, self.headers.get, self.rfile.read, self.client_address[0])
        self.send_response(resp.status)
        for k, v in resp.headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(resp.body)))
        self.end_headers()
        if not head:
            self.wfile.write(resp.body)


def wsgi_app(environ, start_response):
    """Standard Python web interface, for hosts like PythonAnywhere."""
    def header(name):
        key = name.upper().replace("-", "_")
        return environ.get(key if key in ("CONTENT_TYPE", "CONTENT_LENGTH") else "HTTP_" + key)

    raw_path = quote(environ.get("PATH_INFO", "/"), safe="/:@!$&'()*+,;=-._~")
    if environ.get("QUERY_STRING"):
        raw_path += "?" + environ["QUERY_STRING"]
    # Behind a host's proxy the real visitor address arrives in a header.
    ip = (header("X-Real-IP") or (header("X-Forwarded-For") or "").split(",")[0].strip()
          or environ.get("REMOTE_ADDR", ""))
    method = environ.get("REQUEST_METHOD", "GET")
    resp = process(method, raw_path, header, environ["wsgi.input"].read, ip)
    headers = resp.headers + [("Content-Length", str(len(resp.body)))]
    start_response(f"{resp.status} {HTTPStatus(resp.status).phrase}", headers)
    return [b"" if method == "HEAD" else resp.body]


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

@route("GET", "/join", login=False)
def get_join(req):
    return Response(views.join_page(req, CAPTIVE_HOST, os.path.isfile(os.path.join(STATIC_DIR, "qr-wifi.svg")),
                                    os.path.isfile(os.path.join(STATIC_DIR, "qr-site.svg"))))


@route("GET", "/login", login=False)
def get_login(req):
    if req.user:
        return redirect("/")
    return Response(views.login_page(req))


@route("POST", "/login", login=False)
def post_login(req):
    try:
        user = auth.authenticate(req.conn, req.form.get("username"), req.form.get("password"), req.client_ip)
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
        auth.IP_THROTTLE.check("ip:" + req.client_ip)
        if not hmac.compare_digest(f.get("invite", "").strip(), code):
            auth.IP_THROTTLE.fail("ip:" + req.client_ip)
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
