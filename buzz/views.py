"""HTML pages. Plain f-strings; every user-supplied value goes through e()."""
import time
from html import escape

from . import charts, db, market

STATUS_LABEL = {
    "suggested": "Suggested", "opening": "Setting line", "open": "Open",
    "locked": "Locked", "resolved": "Resolved", "cancelled": "Cancelled", "rejected": "Rejected",
}


def e(v):
    return escape("" if v is None else str(v), quote=True)


def fmt_line(v):
    return "—" if v is None else f"{v:.2f}".rstrip("0").rstrip(".") if v != int(v) else f"{int(v)}"


def fmt_ts(ts):
    return time.strftime("%b %d, %H:%M", time.localtime(ts)) if ts else ""


def pts(n):
    return f"{n:,}"


def csrf_field(req):
    return f'<input type="hidden" name="csrf" value="{e(req.csrf)}">'


def post_button(req, action, label, cls="", confirm=None, extra=""):
    onsubmit = f' onsubmit="return confirm(\'{e(confirm)}\')"' if confirm else ""
    return (f'<form method="post" action="{e(action)}" class="inline"{onsubmit}>{csrf_field(req)}{extra}'
            f'<button class="{cls}">{e(label)}</button></form>')


def change_badge(opening, line):
    if opening is None or line is None:
        return ""
    d = line - opening
    if abs(d) < 0.005:
        return '<span class="chg flat">±0</span>'
    cls, arrow = ("up", "▲") if d > 0 else ("down", "▼")
    return f'<span class="chg {cls}">{arrow} {abs(d):.2f}</span>'


def status_pill(status):
    return f'<span class="pill s-{e(status)}">{e(STATUS_LABEL.get(status, status))}</span>'


# --------------------------------------------------------------------- layout

def layout(req, title, body, ticker=True):
    u = req.user
    nav = ""
    if u:
        admin = '<a href="/admin">Admin</a>' if u["is_admin"] else ""
        nav = (f'<nav><a href="/">Markets</a><a href="/suggestions">Suggestions</a>'
               f'<a href="/leaderboard">Leaderboard</a>{admin}'
               f'<a href="/me" class="me">{e(u["display_name"])} · <b>{pts(u["balance"])}</b> pts</a>'
               f'{post_button(req, "/logout", "Log out", "link")}</nav>')
    flash = ""
    if req.flash:
        kind, msg = req.flash
        flash = f'<div class="flash {e(kind)}">{e(msg)}</div>'
    tick = ticker_tape(req) if (ticker and u) else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)} · BuzzMarket</title>
<link rel="stylesheet" href="/static/style.css">
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>🐝</text></svg>">
</head><body>
<header><a href="/" class="brand">🐝 Buzz<span>Market</span></a>{nav}</header>
{tick}
<main>{flash}{body}</main>
<footer>Play money only · bets judged on the line you took · winners split the pot</footer>
</body></html>"""


def ticker_tape(req):
    rows = req.conn.execute(
        "SELECT id, phrase, opening_line, line FROM markets WHERE status IN ('open','locked') ORDER BY id").fetchall()
    if not rows:
        return ""
    items = "".join(
        f'<a href="/m/{r["id"]}" class="tick-item"><b>{e(r["phrase"]).upper()}</b> '
        f'{fmt_line(r["line"])} {change_badge(r["opening_line"], r["line"])}</a>' for r in rows)
    # duplicated so the scroll loops seamlessly
    return f'<div class="ticker"><div class="tick-track">{items}{items}</div></div>'


# ---------------------------------------------------------------------- auth

def login_page(req, username=""):
    body = f"""
<section class="auth card">
  <h1>Log in</h1>
  <form method="post" action="/login">
    <label>Username <input name="username" value="{e(username)}" autocomplete="username" required autofocus></label>
    <label>Password <input name="password" type="password" autocomplete="current-password" required></label>
    <button class="primary">Log in</button>
  </form>
  <p class="muted">New here? <a href="/register">Create an account</a> with the invite code.</p>
</section>"""
    return layout(req, "Log in", body)


def register_page(req, form=None):
    f = form or {}
    body = f"""
<section class="auth card">
  <h1>Join BuzzMarket</h1>
  <form method="post" action="/register">
    <label>Your name <input name="display_name" value="{e(f.get('display_name', ''))}" maxlength="40" required autofocus></label>
    <label>Username <input name="username" value="{e(f.get('username', ''))}" maxlength="24" autocomplete="username" required></label>
    <label>Password <input name="password" type="password" autocomplete="new-password" required></label>
    <label>Invite code <input name="invite" value="{e(f.get('invite', ''))}" required></label>
    <button class="primary">Create account</button>
  </form>
  <p class="muted">Already have one? <a href="/login">Log in</a></p>
</section>"""
    return layout(req, "Register", body)


# ------------------------------------------------------------------- markets

def market_card(conn, m):
    pool = market.pool(conn, m["id"])
    spark = charts.sparkline(market.history(conn, m["id"]))
    return f"""<a class="mcard" href="/m/{m['id']}">
  <div class="mhead"><h3>“{e(m['phrase'])}”</h3>{status_pill(m['status'])}</div>
  <div class="mmeta">{e(m['meeting'])}</div>
  <div class="mline"><span class="big">{fmt_line(m['line'])}</span>{change_badge(m['opening_line'], m['line'])}{spark}</div>
  <div class="mpool"><span class="over">Over {pts(pool['over'])}</span><span class="under">Under {pts(pool['under'])}</span>
  <span class="muted">{pool['n']} bets</span></div>
</a>"""


def home_page(req):
    c = req.conn
    open_ = c.execute("SELECT * FROM markets WHERE status = 'open' ORDER BY opened_at DESC").fetchall()
    locked = c.execute("SELECT * FROM markets WHERE status = 'locked' ORDER BY locked_at DESC").fetchall()
    opening = c.execute("SELECT * FROM markets WHERE status = 'opening' ORDER BY created_at").fetchall()
    done = c.execute("SELECT * FROM markets WHERE status IN ('resolved','cancelled') "
                     "ORDER BY resolved_at DESC LIMIT 12").fetchall()

    parts = ['<div class="pagehead"><h1>Markets</h1><a class="button primary" href="/suggest">+ Suggest a buzzword</a></div>']
    parts.append('<h2>Open for betting</h2>')
    parts.append('<div class="grid">' + "".join(market_card(c, m) for m in open_) + '</div>' if open_
                 else '<p class="muted">Nothing open right now. Suggest a buzzword!</p>')
    if locked:
        parts.append('<h2>Locked · meeting in progress</h2><div class="grid">'
                     + "".join(market_card(c, m) for m in locked) + '</div>')
    if opening:
        u = req.user
        can = u["is_trusted"] or u["is_admin"]
        items = []
        for m in opening:
            n = c.execute("SELECT COUNT(*) FROM guesses WHERE market_id = ?", (m["id"],)).fetchone()[0]
            mine = c.execute("SELECT guess FROM guesses WHERE market_id = ? AND user_id = ?",
                             (m["id"], u["id"])).fetchone()
            action = ""
            if can:
                action = (f'<form method="post" action="/m/{m["id"]}/guess" class="inline">{csrf_field(req)}'
                          f'<input name="guess" type="number" step="0.5" min="0" placeholder="your guess" '
                          f'value="{e(fmt_line(mine["guess"]) if mine else "")}" required>'
                          f'<button>{"Update" if mine else "Submit"}</button></form>')
            items.append(f'<li><a href="/m/{m["id"]}">“{e(m["phrase"])}”</a> '
                         f'<span class="muted">{e(m["meeting"])} · {n} guess{"es" if n != 1 else ""}</span> {action}</li>')
        hint = ("You're trusted: give a blind guess of how many times each will be said. "
                "The median becomes the opening line." if can else
                "Trusted users are setting the opening line for these.")
        parts.append(f'<h2>Setting the opening line</h2><p class="muted">{hint}</p><ul class="list">{"".join(items)}</ul>')
    if done:
        rows = "".join(
            f'<tr><td><a href="/m/{m["id"]}">“{e(m["phrase"])}”</a></td><td>{e(m["meeting"])}</td>'
            f'<td>{fmt_line(m["opening_line"])}</td><td>{fmt_line(m["line"])}</td>'
            f'<td><b>{"—" if m["actual"] is None else m["actual"]}</b></td><td>{status_pill(m["status"])}</td></tr>'
            for m in done)
        parts.append('<h2>Recently settled</h2><table><thead><tr><th>Phrase</th><th>Meeting</th><th>Opened</th>'
                     f'<th>Closed</th><th>Said</th><th></th></tr></thead><tbody>{rows}</tbody></table>')
    return layout(req, "Markets", "".join(parts))


def market_page(req, m):
    c = req.conn
    u = req.user
    pool = market.pool(c, m["id"])
    hist = market.history(c, m["id"])
    parts = [f'<p><a href="/">← Markets</a></p>',
             f'<div class="pagehead"><h1>“{e(m["phrase"])}”</h1>{status_pill(m["status"])}</div>']
    sugg = c.execute("SELECT display_name FROM users WHERE id = ?", (m["suggested_by"],)).fetchone()
    meta = " · ".join(x for x in [e(m["meeting"]), f"suggested by {e(sugg['display_name'])}" if sugg else ""] if x)
    if meta:
        parts.append(f'<p class="muted">{meta}</p>')
    if m["description"]:
        parts.append(f'<p>{e(m["description"])}</p>')

    if m["status"] in ("open", "locked", "resolved", "cancelled") and m["line"] is not None:
        actual_txt = f'<div class="stat"><span>Actually said</span><b>{m["actual"]}×</b></div>' if m["actual"] is not None else ""
        parts.append(f"""<div class="stats">
  <div class="stat"><span>Current line</span><b>{fmt_line(m['line'])}</b>{change_badge(m['opening_line'], m['line'])}</div>
  <div class="stat"><span>Opening line</span><b>{fmt_line(m['opening_line'])}</b></div>
  <div class="stat"><span>Pot</span><b>{pts(pool['total'])}</b></div>
  <div class="stat over"><span>On over</span><b>{pts(pool['over'])}</b></div>
  <div class="stat under"><span>On under</span><b>{pts(pool['under'])}</b></div>{actual_txt}
</div>""")
        parts.append(f'<div class="card chartbox">{charts.line_chart(hist, m["opening_line"], m["actual"])}</div>')

    if m["status"] == "open":
        mult_o = market.estimated_multiplier(pool, "over", 100)
        mult_u = market.estimated_multiplier(pool, "under", 100)
        parts.append(f"""<div class="card betbox">
  <h2>Place a bet</h2>
  <p>Will <b>“{e(m['phrase'])}”</b> be said more or less than <b class="big">{fmt_line(m['line'])}</b> times?</p>
  <form method="post" action="/m/{m['id']}/bet">{csrf_field(req)}
    <label>Stake <input name="stake" type="number" min="1" max="{u['balance']}" value="{min(100, max(u['balance'], 1))}" required></label>
    <div class="betbtns">
      <button name="side" value="over" class="over">▲ Over {fmt_line(m['line'])}</button>
      <button name="side" value="under" class="under">▼ Under {fmt_line(m['line'])}</button>
    </div>
  </form>
  <p class="muted small">You have {pts(u['balance'])} pts. Your bet locks in the current line, then nudges the line
  up (over) or down (under) for the next person. Rough payout if you win with 100 pts now:
  over ≈ {mult_o:.2f}×, under ≈ {mult_u:.2f}×.</p>
</div>""")
    elif m["status"] == "opening":
        n = c.execute("SELECT COUNT(*) FROM guesses WHERE market_id = ?", (m["id"],)).fetchone()[0]
        mine = c.execute("SELECT guess FROM guesses WHERE market_id = ? AND user_id = ?", (m["id"], u["id"])).fetchone()
        if u["is_trusted"] or u["is_admin"]:
            parts.append(f"""<div class="card"><h2>Set the opening line</h2>
<p>Blind guess: how many times will this be said? ({n} guesses so far.)</p>
<form method="post" action="/m/{m['id']}/guess" class="row">{csrf_field(req)}<input type="hidden" name="next" value="/m/{m['id']}">
<input name="guess" type="number" step="0.5" min="0" value="{e(fmt_line(mine['guess']) if mine else '')}" required>
<button class="primary">{'Update guess' if mine else 'Submit guess'}</button></form></div>""")
        else:
            parts.append(f'<div class="card"><p>Trusted users are setting the opening line ({n} guesses so far). '
                         'Betting opens soon.</p></div>')
    elif m["status"] == "suggested":
        votes = c.execute("SELECT COUNT(*) FROM votes WHERE market_id = ?", (m["id"],)).fetchone()[0]
        parts.append(f'<div class="card"><p>This is a suggestion with {votes} upvote(s). '
                     'The admin will approve it before it becomes a market.</p></div>')

    bets = c.execute("SELECT b.*, u.display_name FROM bets b JOIN users u ON u.id = b.user_id "
                     "WHERE b.market_id = ? ORDER BY b.created_at DESC", (m["id"],)).fetchall()
    if bets:
        rows = "".join(
            f'<tr class="{"mine" if b["user_id"] == u["id"] else ""}"><td>{fmt_ts(b["created_at"])}</td>'
            f'<td>{e(b["display_name"])}</td><td class="{b["side"]}">{b["side"].upper()} {fmt_line(b["line"])}</td>'
            f'<td>{pts(b["stake"])}</td><td>{result_cell(b)}</td></tr>' for b in bets)
        parts.append('<h2>Bets</h2><table><thead><tr><th>When</th><th>Who</th><th>Bet</th><th>Stake</th>'
                     f'<th>Result</th></tr></thead><tbody>{rows}</tbody></table>')
    if u["is_admin"]:
        parts.append(f'<p><a href="/admin#m{m["id"]}">Manage in admin →</a></p>')
    return layout(req, m["phrase"], "".join(parts))


def result_cell(b):
    if not b["result"]:
        return '<span class="muted">pending</span>'
    if b["result"] == "win":
        return f'<span class="win">won {pts(b["payout"])}</span>'
    if b["result"] == "lose":
        return '<span class="lose">lost</span>'
    return f'<span class="muted">{e(b["result"])} {pts(b["payout"] or 0)}</span>'


def suggest_page(req, form=None):
    f = form or {}
    body = f"""<p><a href="/">← Markets</a></p>
<section class="card narrow"><h1>Suggest a buzzword</h1>
<form method="post" action="/suggest">{csrf_field(req)}
<label>Buzzword / phrase <input name="phrase" maxlength="80" value="{e(f.get('phrase', ''))}" placeholder="e.g. synergy" required autofocus></label>
<label>Which meeting? <input name="meeting" maxlength="80" value="{e(f.get('meeting', ''))}" placeholder="e.g. Monday all-hands"></label>
<label>Notes / rules <textarea name="description" maxlength="300" placeholder="e.g. 'synergies' counts too">{e(f.get('description', ''))}</textarea></label>
<button class="primary">Suggest it</button>
</form></section>"""
    return layout(req, "Suggest", body)


def suggestions_page(req):
    c = req.conn
    rows = c.execute(
        "SELECT m.*, u.display_name, (SELECT COUNT(*) FROM votes v WHERE v.market_id = m.id) AS votes, "
        "EXISTS(SELECT 1 FROM votes v WHERE v.market_id = m.id AND v.user_id = ?) AS voted "
        "FROM markets m LEFT JOIN users u ON u.id = m.suggested_by WHERE m.status = 'suggested' "
        "ORDER BY votes DESC, m.created_at", (req.user["id"],)).fetchall()
    items = "".join(
        f'<li>{post_button(req, f"/m/{m["id"]}/vote", f"▲ {m["votes"]}", "vote on" if m["voted"] else "vote")}'
        f' <b>“{e(m["phrase"])}”</b> <span class="muted">{e(m["meeting"])} · by {e(m["display_name"] or "?")}</span>'
        f'{"<div class=small>" + e(m["description"]) + "</div>" if m["description"] else ""}</li>'
        for m in rows)
    body = ('<div class="pagehead"><h1>Suggestions</h1><a class="button primary" href="/suggest">+ Suggest</a></div>'
            '<p class="muted">Upvote the ones you want to bet on. The admin approves the best ones.</p>'
            + (f'<ul class="list votes">{items}</ul>' if rows else '<p class="muted">No pending suggestions.</p>'))
    return layout(req, "Suggestions", body)


def leaderboard_page(req):
    rows = req.conn.execute(
        "SELECT u.id, u.display_name, u.username, u.balance, "
        "COALESCE((SELECT SUM(stake) FROM bets b WHERE b.user_id = u.id AND b.result IS NULL), 0) AS at_risk, "
        "(SELECT COUNT(*) FROM bets b WHERE b.user_id = u.id AND b.result = 'win') AS wins, "
        "(SELECT COUNT(*) FROM bets b WHERE b.user_id = u.id AND b.result = 'lose') AS losses "
        "FROM users u WHERE u.disabled = 0 ORDER BY u.balance + at_risk DESC, u.display_name").fetchall()
    trs = "".join(
        f'<tr class="{"mine" if r["id"] == req.user["id"] else ""}"><td>{i}</td><td>{e(r["display_name"])} '
        f'<span class="muted">@{e(r["username"])}</span></td><td><b>{pts(r["balance"] + r["at_risk"])}</b></td>'
        f'<td>{pts(r["balance"])}</td><td>{pts(r["at_risk"])}</td><td>{r["wins"]}–{r["losses"]}</td></tr>'
        for i, r in enumerate(rows, 1))
    body = ('<h1>Leaderboard</h1><table><thead><tr><th>#</th><th>Player</th><th>Net worth</th><th>Cash</th>'
            f'<th>In bets</th><th>W–L</th></tr></thead><tbody>{trs}</tbody></table>')
    return layout(req, "Leaderboard", body)


def me_page(req):
    c = req.conn
    u = req.user
    bets = c.execute("SELECT b.*, m.phrase, m.status, m.line AS cur_line FROM bets b JOIN markets m ON m.id = b.market_id "
                     "WHERE b.user_id = ? ORDER BY b.created_at DESC LIMIT 100", (u["id"],)).fetchall()
    led = c.execute("SELECT * FROM ledger WHERE user_id = ? ORDER BY ts DESC, id DESC LIMIT 50", (u["id"],)).fetchall()
    bet_rows = "".join(
        f'<tr><td><a href="/m/{b["market_id"]}">“{e(b["phrase"])}”</a></td>'
        f'<td class="{b["side"]}">{b["side"].upper()} {fmt_line(b["line"])}</td><td>{pts(b["stake"])}</td>'
        f'<td>{fmt_line(b["cur_line"])}</td><td>{result_cell(b)}</td></tr>' for b in bets)
    led_rows = "".join(
        f'<tr><td>{fmt_ts(l["ts"])}</td><td>{e(l["reason"])}</td>'
        f'<td class="{"win" if l["amount"] > 0 else "lose"}">{l["amount"]:+,}</td></tr>' for l in led)
    trusted = ' <span class="pill s-open">trusted</span>' if u["is_trusted"] else ""
    body = f"""<h1>{e(u['display_name'])} <span class="muted">@{e(u['username'])}</span>{trusted}</h1>
<div class="stats"><div class="stat"><span>Balance</span><b>{pts(u['balance'])}</b></div></div>
<h2>My bets</h2>
{'<table><thead><tr><th>Market</th><th>Bet</th><th>Stake</th><th>Line now</th><th>Result</th></tr></thead><tbody>' + bet_rows + '</tbody></table>' if bets else '<p class="muted">No bets yet.</p>'}
<h2>Points history</h2>
<table><thead><tr><th>When</th><th>What</th><th>Points</th></tr></thead><tbody>{led_rows}</tbody></table>
<section class="card narrow"><h2>Change password</h2>
<form method="post" action="/me/password">{csrf_field(req)}
<label>Current password <input name="current" type="password" autocomplete="current-password" required></label>
<label>New password <input name="new" type="password" autocomplete="new-password" required></label>
<button>Change password</button></form></section>"""
    return layout(req, "My account", body)


# --------------------------------------------------------------------- admin

def admin_page(req):
    c = req.conn
    parts = ['<h1>Admin</h1>']

    def market_rows(status, order):
        return c.execute(f"SELECT m.*, u.display_name AS by_name, "
                         f"(SELECT COUNT(*) FROM votes v WHERE v.market_id = m.id) AS votes "
                         f"FROM markets m LEFT JOIN users u ON u.id = m.suggested_by "
                         f"WHERE m.status = ? ORDER BY {order}", (status,)).fetchall()

    # Suggestions
    sugg = market_rows("suggested", "votes DESC, m.created_at")
    items = "".join(
        f'<li id="m{m["id"]}"><b>“{e(m["phrase"])}”</b> <span class="muted">{e(m["meeting"])} · by '
        f'{e(m["by_name"] or "?")} · ▲{m["votes"]}</span>'
        f'{"<div class=small>" + e(m["description"]) + "</div>" if m["description"] else ""}'
        f'<div class="actions">{post_button(req, f"/admin/m/{m["id"]}/approve", "Approve", "primary")}'
        f'{post_button(req, f"/admin/m/{m["id"]}/reject", "Reject", "danger")}</div></li>' for m in sugg)
    parts.append(f'<h2>Pending suggestions</h2>' + (f'<ul class="list">{items}</ul>' if sugg else '<p class="muted">None.</p>'))

    # Opening
    opening = market_rows("opening", "m.created_at")
    items = []
    for m in opening:
        gs = c.execute("SELECT g.guess, u.display_name FROM guesses g JOIN users u ON u.id = g.user_id "
                       "WHERE g.market_id = ? ORDER BY g.guess", (m["id"],)).fetchall()
        sug = market.suggested_opening_line(c, m["id"])
        glist = ", ".join(f'{e(g["display_name"])}: {fmt_line(g["guess"])}' for g in gs) or "no guesses yet"
        items.append(
            f'<li id="m{m["id"]}"><b>“{e(m["phrase"])}”</b> <span class="muted">{e(m["meeting"])}</span>'
            f'<div class="small">Guesses: {glist}</div>'
            f'<div class="actions"><form method="post" action="/admin/m/{m["id"]}/open" class="inline">{csrf_field(req)}'
            f'<input name="line" type="number" step="0.01" min="0" placeholder="line" value="{e(fmt_line(sug) if sug is not None else "")}">'
            f'<button class="primary">Open betting</button></form>'
            f'{post_button(req, f"/admin/m/{m["id"]}/cancel", "Cancel", "danger", "Cancel this market?")}</div></li>')
    parts.append('<h2>Setting the opening line</h2><p class="muted small">Line defaults to the median guess '
                 '(whole numbers get +0.5 so the opening line can’t tie). Edit it before opening if you like.</p>'
                 + (f'<ul class="list">{"".join(items)}</ul>' if opening else '<p class="muted">None.</p>'))

    # Live markets
    live = c.execute("SELECT * FROM markets WHERE status IN ('open','locked') ORDER BY status, opened_at").fetchall()
    items = []
    for m in live:
        p = market.pool(c, m["id"])
        lock_btn = (post_button(req, f"/admin/m/{m['id']}/lock", "Lock betting") if m["status"] == "open"
                    else post_button(req, f"/admin/m/{m['id']}/reopen", "Reopen betting"))
        items.append(
            f'<li id="m{m["id"]}"><a href="/m/{m["id"]}"><b>“{e(m["phrase"])}”</b></a> {status_pill(m["status"])} '
            f'<span class="muted">line {fmt_line(m["line"])} (opened {fmt_line(m["opening_line"])}) · pot {pts(p["total"])} · {p["n"]} bets</span>'
            f'<div class="actions">{lock_btn}'
            f'<form method="post" action="/admin/m/{m["id"]}/resolve" class="inline" '
            f'onsubmit="return confirm(\'Resolve and pay out? This can’t be undone.\')">{csrf_field(req)}'
            f'<input name="actual" type="number" min="0" step="1" placeholder="times said" required>'
            f'<button class="primary">Resolve</button></form>'
            f'{post_button(req, f"/admin/m/{m["id"]}/cancel", "Cancel & refund", "danger", "Cancel and refund all bets?")}</div></li>')
    parts.append('<h2>Live markets</h2>' + (f'<ul class="list">{"".join(items)}</ul>' if live else '<p class="muted">None.</p>'))

    # New market directly
    parts.append(f"""<details class="card"><summary>Create a market directly</summary>
<form method="post" action="/admin/markets">{csrf_field(req)}
<label>Phrase <input name="phrase" maxlength="80" required></label>
<label>Meeting <input name="meeting" maxlength="80"></label>
<label>Notes <textarea name="description" maxlength="300"></textarea></label>
<label>Opening line (leave blank to let trusted users guess) <input name="line" type="number" step="0.01" min="0"></label>
<button class="primary">Create</button></form></details>""")

    # Users
    users = c.execute("SELECT * FROM users ORDER BY disabled, display_name").fetchall()
    trs = []
    for x in users:
        flags = " ".join(f for f in ["admin" if x["is_admin"] else "", "trusted" if x["is_trusted"] else "",
                                       "disabled" if x["disabled"] else ""] if f)
        is_me = x["id"] == req.user["id"]
        trs.append(
            f'<tr class="{"muted" if x["disabled"] else ""}"><td>{e(x["display_name"])}<br><span class="muted">@{e(x["username"])}</span></td>'
            f'<td>{pts(x["balance"])}</td><td>{e(flags)}</td><td class="actions">'
            f'{post_button(req, f"/admin/u/{x["id"]}/trusted", "Untrust" if x["is_trusted"] else "Make trusted")}'
            + ("" if is_me else
               post_button(req, f"/admin/u/{x["id"]}/disable", "Enable" if x["disabled"] else "Disable", "danger"))
            + f'<form method="post" action="/admin/u/{x["id"]}/balance" class="inline">{csrf_field(req)}'
            f'<input name="amount" type="number" step="1" placeholder="±pts" required class="short">'
            f'<button>Adjust</button></form>'
            f'<form method="post" action="/admin/u/{x["id"]}/password" class="inline">{csrf_field(req)}'
            f'<input name="password" type="text" placeholder="new password" required class="short">'
            f'<button>Set password</button></form></td></tr>')
    parts.append('<h2>Users</h2><table class="users"><thead><tr><th>User</th><th>Balance</th><th>Flags</th>'
                 f'<th>Actions</th></tr></thead><tbody>{"".join(trs)}</tbody></table>')
    parts.append(f"""<details class="card"><summary>Add a user</summary>
<form method="post" action="/admin/users">{csrf_field(req)}
<label>Name <input name="display_name" maxlength="40" required></label>
<label>Username <input name="username" maxlength="24" required></label>
<label>Password (give this to them) <input name="password" type="text" required></label>
<label class="check"><input type="checkbox" name="trusted" value="1"> Trusted (sets opening lines)</label>
<button class="primary">Add user</button></form></details>""")

    # Settings
    s = {k: db.get_setting(c, k) for k in db.DEFAULT_SETTINGS}
    parts.append(f"""<h2>Settings</h2><form method="post" action="/admin/settings" class="card settings">{csrf_field(req)}
<label>Invite code (needed to self-register; blank = registration off)
<input name="invite_code" value="{e(s['invite_code'])}"></label>
<label>Starting balance for new users <input name="starting_balance" type="number" min="0" value="{e(s['starting_balance'])}"></label>
<label>Max bet (0 = no limit) <input name="max_bet" type="number" min="0" value="{e(s['max_bet'])}"></label>
<label>Liquidity (higher = line moves less per bet) <input name="liquidity" type="number" min="1" value="{e(s['liquidity'])}"></label>
<button class="primary">Save settings</button></form>""")
    return layout(req, "Admin", "".join(parts), ticker=False)


def error_page(req, code, msg):
    return layout(req, str(code), f'<section class="card narrow"><h1>{code}</h1><p>{e(msg)}</p>'
                                  '<p><a href="/">Back to markets</a></p></section>', ticker=False)
