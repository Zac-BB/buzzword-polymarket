"""Market rules: opening line, moving line, pari-mutuel payouts.

How a market works
------------------
1. suggested  - any user suggests a phrase; others can upvote it.
2. opening    - admin approves it. Trusted users submit blind guesses of the count.
3. open       - admin opens betting. The opening line is the median guess
                (admin may override). Users bet OVER or UNDER the *current* line.
                Every bet nudges the line: OVER pushes it up, UNDER pushes it down.
4. locked     - admin locks betting when the meeting starts.
5. resolved   - admin enters the real count. Each bet is judged against the line
                it was placed at. All losing stakes + winning stakes form one pot,
                which winners split in proportion to their stake. A bet whose line
                equals the count exactly is a push (refunded). If nobody won,
                everybody is refunded.
   cancelled  - admin can cancel any time before resolving; all bets refunded.
"""
import statistics

from . import db


class MarketError(ValueError):
    pass


def get_market(conn, market_id):
    m = conn.execute("SELECT * FROM markets WHERE id = ?", (market_id,)).fetchone()
    if not m:
        raise MarketError("No such market.")
    return m


def _ledger(conn, user_id, amount, reason):
    conn.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (amount, user_id))
    conn.execute("INSERT INTO ledger(user_id, amount, reason, ts) VALUES (?, ?, ?, ?)",
                 (user_id, amount, reason, db.now()))


def _record_line(conn, market_id, line):
    conn.execute("INSERT INTO line_history(market_id, ts, line) VALUES (?, ?, ?)",
                 (market_id, db.now(), line))


# ---------------------------------------------------------------- suggestions

def suggest(conn, user_id, phrase, description="", meeting=""):
    phrase = (phrase or "").strip()
    if not phrase or len(phrase) > 80:
        raise MarketError("Phrase is required (max 80 characters).")
    dupe = conn.execute(
        "SELECT 1 FROM markets WHERE phrase = ? COLLATE NOCASE AND status IN ('suggested','opening','open','locked')",
        (phrase,)).fetchone()
    if dupe:
        raise MarketError("That phrase already has an active market or suggestion.")
    cur = conn.execute(
        "INSERT INTO markets(phrase, description, meeting, suggested_by, status, created_at) "
        "VALUES (?, ?, ?, ?, 'suggested', ?)",
        (phrase, (description or "").strip()[:300], (meeting or "").strip()[:80], user_id, db.now()))
    conn.execute("INSERT OR IGNORE INTO votes(market_id, user_id) VALUES (?, ?)", (cur.lastrowid, user_id))
    return cur.lastrowid


def toggle_vote(conn, market_id, user_id):
    m = get_market(conn, market_id)
    if m["status"] != "suggested":
        raise MarketError("Voting is only for suggestions.")
    if conn.execute("DELETE FROM votes WHERE market_id = ? AND user_id = ?", (market_id, user_id)).rowcount == 0:
        conn.execute("INSERT INTO votes(market_id, user_id) VALUES (?, ?)", (market_id, user_id))


def approve(conn, market_id):
    m = get_market(conn, market_id)
    if m["status"] != "suggested":
        raise MarketError("Only suggestions can be approved.")
    conn.execute("UPDATE markets SET status = 'opening' WHERE id = ?", (market_id,))


def reject(conn, market_id):
    m = get_market(conn, market_id)
    if m["status"] != "suggested":
        raise MarketError("Only suggestions can be rejected.")
    conn.execute("UPDATE markets SET status = 'rejected' WHERE id = ?", (market_id,))


# --------------------------------------------------------------- opening line

def submit_guess(conn, market_id, user, guess):
    m = get_market(conn, market_id)
    if m["status"] != "opening":
        raise MarketError("This market isn't taking opening guesses.")
    if not (user["is_trusted"] or user["is_admin"]):
        raise MarketError("Only trusted users set the opening line.")
    try:
        guess = float(guess)
    except (TypeError, ValueError):
        raise MarketError("Guess must be a number.")
    if guess < 0 or guess > 10000:
        raise MarketError("Guess must be between 0 and 10000.")
    conn.execute(
        "INSERT INTO guesses(market_id, user_id, guess, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(market_id, user_id) DO UPDATE SET guess = excluded.guess, created_at = excluded.created_at",
        (market_id, user["id"], guess, db.now()))


def suggested_opening_line(conn, market_id):
    rows = conn.execute("SELECT guess FROM guesses WHERE market_id = ?", (market_id,)).fetchall()
    if not rows:
        return None
    med = statistics.median(r["guess"] for r in rows)
    # Use a half-point line so the opening line itself can't push.
    return float(int(med)) + 0.5 if med == int(med) else round(med, 2)


def open_market(conn, market_id, line=None):
    m = get_market(conn, market_id)
    if m["status"] != "opening":
        raise MarketError("Market must be in the opening stage.")
    if line in (None, ""):
        line = suggested_opening_line(conn, market_id)
        if line is None:
            raise MarketError("No guesses yet - enter an opening line manually.")
    try:
        line = round(float(line), 2)
    except (TypeError, ValueError):
        raise MarketError("Opening line must be a number.")
    if line < 0:
        raise MarketError("Opening line can't be negative.")
    conn.execute("UPDATE markets SET status = 'open', opening_line = ?, line = ?, opened_at = ? WHERE id = ?",
                 (line, line, db.now(), market_id))
    _record_line(conn, market_id, line)


# ---------------------------------------------------------------------- bets

def line_move(line, stake, side, liquidity):
    """How far the line moves for a bet. Scales with the size of the line so a
    100-point bet moves a 3.5 line a little and a 40.5 line a bit more."""
    delta = (stake / max(liquidity, 1)) * max(1.0, line) * 0.5
    return delta if side == "over" else -delta


def place_bet(conn, market_id, user, side, stake):
    m = get_market(conn, market_id)
    if m["status"] != "open":
        raise MarketError("Betting is closed on this market.")
    if side not in ("over", "under"):
        raise MarketError("Pick over or under.")
    try:
        stake = int(stake)
    except (TypeError, ValueError):
        raise MarketError("Stake must be a whole number.")
    if stake <= 0:
        raise MarketError("Stake must be positive.")
    limit = bet_limit(conn, market_id)
    if limit["max"] and stake > limit["max"]:
        if limit["early"]:
            raise MarketError(f"This market just opened: max bet is {limit['max']} points until "
                              f"{limit['early_bets']} bets are in ({limit['left']} to go).")
        raise MarketError(f"Max bet is {limit['max']} points.")
    balance = conn.execute("SELECT balance FROM users WHERE id = ?", (user["id"],)).fetchone()["balance"]
    if stake > balance:
        raise MarketError(f"You only have {balance} points.")
    line = m["line"]
    conn.execute("INSERT INTO bets(market_id, user_id, side, stake, line, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                 (market_id, user["id"], side, stake, line, db.now()))
    _ledger(conn, user["id"], -stake, f"Bet {side.upper()} {line:g} on \"{m['phrase']}\"")
    new_line = round(max(0.0, line + line_move(line, stake, side, db.get_int_setting(conn, "liquidity"))), 2)
    conn.execute("UPDATE markets SET line = ? WHERE id = ?", (new_line, market_id))
    _record_line(conn, market_id, new_line)
    return line, new_line


def bet_limit(conn, market_id):
    """Current stake cap for a market. 'max' of 0 means no limit."""
    max_bet = db.get_int_setting(conn, "max_bet")
    early_bets = db.get_int_setting(conn, "early_bets")
    early_max = db.get_int_setting(conn, "early_max_bet")
    n = conn.execute("SELECT COUNT(*) FROM bets WHERE market_id = ?", (market_id,)).fetchone()[0]
    if early_bets > 0 and early_max > 0 and n < early_bets and (max_bet == 0 or early_max < max_bet):
        return {"max": early_max, "early": True, "early_bets": early_bets, "left": early_bets - n}
    return {"max": max_bet, "early": False, "early_bets": early_bets, "left": 0}


def lock(conn, market_id):
    m = get_market(conn, market_id)
    if m["status"] != "open":
        raise MarketError("Only open markets can be locked.")
    conn.execute("UPDATE markets SET status = 'locked', locked_at = ? WHERE id = ?", (db.now(), market_id))


def reopen(conn, market_id):
    m = get_market(conn, market_id)
    if m["status"] != "locked":
        raise MarketError("Only locked markets can be reopened.")
    conn.execute("UPDATE markets SET status = 'open', locked_at = NULL WHERE id = ?", (market_id,))


def bet_outcome(side, line, actual):
    if actual == line:
        return "push"
    if (actual > line) == (side == "over"):
        return "win"
    return "lose"


def compute_payouts(bets, actual):
    """Pure function: list of dicts(id, side, stake, line) -> {bet_id: (result, payout)}."""
    results = {}
    pot_bets = []
    for b in bets:
        outcome = bet_outcome(b["side"], b["line"], actual)
        if outcome == "push":
            results[b["id"]] = ("push", b["stake"])
        else:
            pot_bets.append((b, outcome))
    pot = sum(b["stake"] for b, _ in pot_bets)
    winners = [b for b, o in pot_bets if o == "win"]
    win_stake = sum(b["stake"] for b in winners)
    if win_stake == 0:
        for b, _ in pot_bets:
            results[b["id"]] = ("refund", b["stake"])
        return results
    paid = 0
    for b, o in pot_bets:
        if o == "win":
            p = (b["stake"] * pot) // win_stake
            paid += p
            results[b["id"]] = ("win", p)
        else:
            results[b["id"]] = ("lose", 0)
    # Hand rounding leftovers to the biggest winner so no points vanish.
    leftover = pot - paid
    if leftover:
        top = max(winners, key=lambda b: (b["stake"], -b["id"]))
        results[top["id"]] = ("win", results[top["id"]][1] + leftover)
    return results


def resolve(conn, market_id, actual):
    m = get_market(conn, market_id)
    if m["status"] not in ("open", "locked"):
        raise MarketError("Only open or locked markets can be resolved.")
    try:
        actual = int(actual)
    except (TypeError, ValueError):
        raise MarketError("Count must be a whole number.")
    if actual < 0:
        raise MarketError("Count can't be negative.")
    bets = [dict(b) for b in conn.execute("SELECT * FROM bets WHERE market_id = ?", (market_id,))]
    results = compute_payouts(bets, actual)
    labels = {"win": "Won", "push": "Push", "refund": "Refund (no winners)"}
    for b in bets:
        result, payout = results[b["id"]]
        conn.execute("UPDATE bets SET result = ?, payout = ? WHERE id = ?", (result, payout, b["id"]))
        if payout:
            _ledger(conn, b["user_id"], payout, f"{labels[result]}: \"{m['phrase']}\" said {actual}x")
    conn.execute("UPDATE markets SET status = 'resolved', actual = ?, resolved_at = ?, "
                 "locked_at = COALESCE(locked_at, ?) WHERE id = ?",
                 (actual, db.now(), db.now(), market_id))
    return results


def cancel(conn, market_id):
    m = get_market(conn, market_id)
    if m["status"] in ("resolved", "cancelled", "rejected"):
        raise MarketError("Market is already finished.")
    for b in conn.execute("SELECT * FROM bets WHERE market_id = ?", (market_id,)).fetchall():
        conn.execute("UPDATE bets SET result = 'refund', payout = ? WHERE id = ?", (b["stake"], b["id"]))
        _ledger(conn, b["user_id"], b["stake"], f"Refund: \"{m['phrase']}\" cancelled")
    conn.execute("UPDATE markets SET status = 'cancelled', resolved_at = ? WHERE id = ?", (db.now(), market_id))


def adjust_balance(conn, user_id, amount, reason="Admin adjustment"):
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        raise MarketError("Amount must be a whole number.")
    _ledger(conn, user_id, amount, reason or "Admin adjustment")


# --------------------------------------------------------------- read helpers

def pool(conn, market_id):
    row = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN side='over' THEN stake END),0) AS over_, "
        "COALESCE(SUM(CASE WHEN side='under' THEN stake END),0) AS under_, COUNT(*) AS n "
        "FROM bets WHERE market_id = ?", (market_id,)).fetchone()
    return {"over": row["over_"], "under": row["under_"], "total": row["over_"] + row["under_"], "n": row["n"]}


def history(conn, market_id):
    return [(r["ts"], r["line"]) for r in conn.execute(
        "SELECT ts, line FROM line_history WHERE market_id = ? ORDER BY ts, id", (market_id,))]


def estimated_multiplier(pool_, side, stake):
    """Rough 'if your side wins' multiplier, assuming everyone on your side wins too."""
    mine = pool_[side] + stake
    return (pool_["total"] + stake) / mine if mine else 0
