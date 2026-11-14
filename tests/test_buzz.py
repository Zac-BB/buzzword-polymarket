import http.cookiejar
import os
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["BUZZ_QUIET"] = "1"

from buzz import auth, db, market, server  # noqa: E402


class PayoutTests(unittest.TestCase):
    def test_winners_split_pot(self):
        bets = [dict(id=1, side="over", stake=100, line=4.5),
                dict(id=2, side="over", stake=300, line=4.5),
                dict(id=3, side="under", stake=200, line=4.5)]
        r = market.compute_payouts(bets, 6)
        self.assertEqual(r[1], ("win", 150))
        self.assertEqual(r[2], ("win", 450))
        self.assertEqual(r[3], ("lose", 0))
        self.assertEqual(sum(p for _, p in r.values()), 600)

    def test_each_bet_judged_on_its_own_line(self):
        bets = [dict(id=1, side="over", stake=100, line=4.5),   # 5 > 4.5 win
                dict(id=2, side="over", stake=100, line=5.5),   # 5 < 5.5 lose
                dict(id=3, side="under", stake=100, line=5.5)]  # win
        r = market.compute_payouts(bets, 5)
        self.assertEqual(r[1], ("win", 150))
        self.assertEqual(r[2], ("lose", 0))
        self.assertEqual(r[3], ("win", 150))

    def test_push_and_no_winner_refund(self):
        bets = [dict(id=1, side="over", stake=50, line=5.0),
                dict(id=2, side="over", stake=70, line=6.5)]
        r = market.compute_payouts(bets, 5)
        self.assertEqual(r[1], ("push", 50))
        self.assertEqual(r[2], ("refund", 70))

    def test_no_points_lost_to_rounding(self):
        bets = [dict(id=i, side="over", stake=1, line=1.5) for i in range(1, 4)]
        bets.append(dict(id=9, side="under", stake=1, line=1.5))
        r = market.compute_payouts(bets, 3)
        self.assertEqual(sum(p for _, p in r.values()), 4)

    def test_line_moves(self):
        self.assertGreater(market.line_move(4.5, 100, "over", 2000), 0)
        self.assertLess(market.line_move(4.5, 100, "under", 2000), 0)


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "t.db"))
        db.init(self.conn)
        with db.transaction(self.conn):
            self.admin = auth.create_user(self.conn, "boss", "Boss", "pw1234", is_admin=True)
            self.t1 = auth.create_user(self.conn, "t1", "T1", "pw1234", is_trusted=True)
            self.t2 = auth.create_user(self.conn, "t2", "T2", "pw1234", is_trusted=True)
            self.u = auth.create_user(self.conn, "joe", "Joe", "pw1234")
            db.set_setting(self.conn, "early_bets", 0)  # tested separately below

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def user(self, uid):
        return self.conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()

    def total_points(self):
        bal = self.conn.execute("SELECT SUM(balance) FROM users").fetchone()[0]
        held = self.conn.execute("SELECT COALESCE(SUM(stake),0) FROM bets WHERE result IS NULL").fetchone()[0]
        return bal + held

    def test_full_market_lifecycle(self):
        c = self.conn
        start = self.total_points()
        with db.transaction(c):
            mid = market.suggest(c, self.u, "synergy", meeting="All hands")
            market.approve(c, mid)
            with self.assertRaises(market.MarketError):
                market.submit_guess(c, mid, self.user(self.u), 3)  # not trusted
            market.submit_guess(c, mid, self.user(self.t1), 3)
            market.submit_guess(c, mid, self.user(self.t2), 5)
            self.assertEqual(market.suggested_opening_line(c, mid), 4.5)
            market.open_market(c, mid)
            old, new = market.place_bet(c, mid, self.user(self.u), "over", 200)
            self.assertEqual(old, 4.5)
            self.assertGreater(new, 4.5)
            market.place_bet(c, mid, self.user(self.t1), "under", 100)
            self.assertEqual(self.total_points(), start)
            market.lock(c, mid)
            with self.assertRaises(market.MarketError):
                market.place_bet(c, mid, self.user(self.u), "over", 10)
            market.resolve(c, mid, 7)
        self.assertEqual(self.total_points(), start)
        self.assertEqual(self.user(self.u)["balance"], 1000 - 200 + 300)
        self.assertEqual(self.user(self.t1)["balance"], 900)
        self.assertEqual(len(market.history(c, mid)), 3)

    def test_lines_are_half_points_and_move_whole_steps(self):
        c = self.conn
        self.assertEqual([market.snap(x) for x in (0, 3, 3.5, 3.9, 4)], [0.5, 3.5, 3.5, 3.5, 4.5])
        with db.transaction(c):
            db.set_setting(c, "liquidity", 200)  # 200 net points = one step on a small line
            mid = market.suggest(c, self.u, "bandwidth")
            market.approve(c, mid)
            market.open_market(c, mid, 3.2)  # admin typed 3.2 -> snapped
            self.assertEqual(market.get_market(c, mid)["line"], 3.5)
            joe = self.user(self.u)
            self.assertEqual(market.place_bet(c, mid, joe, "over", 50), (3.5, 3.5))   # not enough yet
            self.assertEqual(market.points_to_move(market.get_market(c, mid), 200)["over"], (50, 4.5))
            self.assertEqual(market.place_bet(c, mid, joe, "over", 50), (3.5, 4.5))   # +100 total -> step up
            self.assertEqual(market.place_bet(c, mid, joe, "under", 1), (4.5, 3.5))   # just below 4 -> back down
            for _ in range(8):
                market.place_bet(c, mid, joe, "under", 100)
            m = market.get_market(c, mid)
            self.assertEqual(m["line"], 0.5)          # never below 0.5
            self.assertIsNone(market.points_to_move(m, 200)["under"])
            lines = [r["line"] for r in c.execute("SELECT line FROM line_history WHERE market_id = ?", (mid,))]
            self.assertTrue(all(x % 1 == 0.5 for x in lines), lines)

    def test_early_bet_cap(self):
        c = self.conn
        with db.transaction(c):
            db.set_setting(c, "early_bets", 3)
            db.set_setting(c, "early_max_bet", 100)
            mid = market.suggest(c, self.u, "align")
            market.approve(c, mid)
            market.open_market(c, mid, 3.5)
            joe = self.user(self.u)
            with self.assertRaisesRegex(market.MarketError, "just opened"):
                market.place_bet(c, mid, joe, "over", 101)
            for _ in range(3):
                self.assertTrue(market.bet_limit(c, mid)["early"])
                market.place_bet(c, mid, joe, "over", 100)
            limit = market.bet_limit(c, mid)
            self.assertEqual((limit["early"], limit["max"]), (False, 500))
            market.place_bet(c, mid, joe, "under", 400)
            db.set_setting(c, "early_max_bet", 0)  # 0 turns it off
            mid2 = market.suggest(c, self.u, "pivot")
            market.approve(c, mid2)
            market.open_market(c, mid2, 1.5)
            self.assertFalse(market.bet_limit(c, mid2)["early"])

    def test_cannot_overbet_and_cancel_refunds(self):
        c = self.conn
        with db.transaction(c):
            mid = market.suggest(c, self.u, "circle back")
            market.approve(c, mid)
            market.open_market(c, mid, 2.5)
            with self.assertRaises(market.MarketError):
                market.place_bet(c, mid, self.user(self.u), "over", 501)  # over max bet
            market.place_bet(c, mid, self.user(self.u), "over", 500)
            market.place_bet(c, mid, self.user(self.u), "over", 500)
            with self.assertRaises(market.MarketError):
                market.place_bet(c, mid, self.user(self.u), "over", 1)  # broke
            market.cancel(c, mid)
        self.assertEqual(self.user(self.u)["balance"], 1000)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(cls.tmp.name, "http.db")
        conn = db.connect()
        db.init(conn)
        with db.transaction(conn):
            auth.create_user(conn, "boss", "Boss", "adminpw", is_admin=True, is_trusted=True)
        conn.close()
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def client(self):
        jar = http.cookiejar.CookieJar()
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def get(self, op, path):
        with op.open(self.base + path) as r:
            return r.read().decode()

    def post(self, op, path, data, page=None):
        html = self.get(op, page or path)
        m = re.search(r'name="csrf" value="([^"]+)"', html)
        if m:
            data = dict(data, csrf=m.group(1))
        body = urllib.parse.urlencode(data).encode()
        with op.open(self.base + path, body) as r:
            return r.read().decode()

    def test_end_to_end(self):
        admin = self.client()
        self.post(admin, "/login", {"username": "boss", "password": "adminpw"})
        self.assertIn("Admin", self.get(admin, "/admin"))

        user = self.client()
        html = self.post(user, "/register", {"display_name": "Sam", "username": "sam",
                                             "password": "sampw", "invite": "buzzword"})
        self.assertIn("Welcome", html)
        self.post(user, "/suggest", {"phrase": "Low-hanging fruit", "meeting": "Standup"}, page="/suggest")

        # non-admin can't reach admin
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get(user, "/admin")
        self.assertEqual(cm.exception.code, 403)

        conn = db.connect()
        mid = conn.execute("SELECT id FROM markets WHERE phrase = 'Low-hanging fruit'").fetchone()[0]
        self.post(admin, f"/admin/m/{mid}/approve", {}, page="/admin")
        self.post(admin, f"/m/{mid}/guess", {"guess": "3"}, page=f"/m/{mid}")
        self.post(admin, f"/admin/m/{mid}/open", {"line": ""}, page="/admin")
        html = self.post(user, f"/m/{mid}/bet", {"side": "over", "stake": "100"}, page=f"/m/{mid}")
        self.assertIn("Bet placed: OVER 3.5", html)
        self.assertIn("<svg", html)
        self.post(admin, f"/admin/m/{mid}/resolve", {"actual": "5"}, page="/admin")
        bal = conn.execute("SELECT balance FROM users WHERE username = 'sam'").fetchone()[0]
        self.assertEqual(bal, 1000)  # only bettor -> wins own stake back
        self.assertIn("Sam", self.get(user, "/leaderboard"))
        conn.close()

    def test_captive_portal_redirect(self):
        import http.client
        port = self.httpd.server_address[1]
        server.CAPTIVE_HOST = "127.0.0.1"
        try:
            for host, path in [("captive.apple.com", "/hotspot-detect.html"),
                               ("connectivitycheck.gstatic.com", "/generate_204")]:
                c = http.client.HTTPConnection("127.0.0.1", port)
                c.request("GET", path, headers={"Host": host})
                r = c.getresponse()
                self.assertEqual((r.status, r.getheader("Location")), (302, "http://127.0.0.1/join"))
                c.close()
            html = self.get(self.client(), "/join")  # own address: served, no login needed
            self.assertIn("Welcome to BuzzMarket", html)
        finally:
            server.CAPTIVE_HOST = ""

    def test_csrf_required(self):
        admin = self.client()
        self.post(admin, "/login", {"username": "boss", "password": "adminpw"})
        req = urllib.request.Request(self.base + "/admin/settings",
                                     urllib.parse.urlencode({"invite_code": "x"}).encode())
        with self.assertRaises(urllib.error.HTTPError) as cm:
            admin.open(req)
        self.assertEqual(cm.exception.code, 403)

    def test_bad_login_and_escaping(self):
        op = self.client()
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.post(op, "/login", {"username": "<script>", "password": "nope"})
        self.assertEqual(cm.exception.code, 401)
        self.assertNotIn("<script>", cm.exception.read().decode())


if __name__ == "__main__":
    unittest.main()
