# 🐝 BuzzMarket

A small, Polymarket-style betting game for office buzzwords. Players bet play-money points on whether
a phrase will be said **over** or **under** a line during a meeting.

It's built to run on a Raspberry Pi with about 20 people: plain Python 3 and SQLite, **no packages
to install**, no build step, and roughly 20 MB of RAM.

## How it works

1. **Suggest**: anyone can suggest a buzzword (and which meeting it's for). Others upvote suggestions.
2. **Approve**: the admin approves the good ones.
3. **Opening line**: users you mark as *trusted* submit a blind guess of the count. The median becomes
   the opening line, rounded to a #.5 (a median of 4 → 4.5). You can override it.
4. **Betting**: players bet OVER or UNDER the current line. Since we're counting whole occurrences, the
   line is **always a #.5** (2.5, 3.5, 4.5 …), so a bet can never tie. Each bet locks in the line it was
   placed at, then adds hidden "pressure" up (over) or down (under). When enough net betting builds up,
   the line moves a **full step of 1**. The bet box shows roughly how many points would tip it
   either way. By default, 400 net points move a small line one step; bigger lines move faster. While a market is brand new
   (its first 5 bets), stakes are capped at 100 points so one early bet can't yank the line or hog a
   tiny pot. The scrolling ticker, the sparklines and the
   per-market chart show where the line is heading.
5. **Lock**: lock betting when the meeting starts.
6. **Resolve**: enter how many times it was actually said. Every bet is judged against *its own* line.
   All stakes form one pot, and the winners split it in proportion to their stakes (pari-mutuel).
   If nobody wins, everyone is refunded. Cancelling a market refunds every bet.

Everyone starts with 1,000 points (you can change this). The leaderboard ranks players by net worth
(cash plus points in open bets).

## Run it

```bash
python3 manage.py create-admin        # make your admin account (asks for a username, name and password)
python3 manage.py serve               # http://<pi-ip>:8000
```

Players can join two ways:
- **Invite code**: they go to `/register` and enter the invite code. The default is `buzzword`;
  change it in Admin → Settings, or leave it blank to turn self-signup off.
- **You create the account**: Admin → Add a user, with a password you hand out. They can change
  it later on their account page.

Also under Admin: make users trusted, adjust balances, reset passwords, disable accounts, set the
max bet, set the early-bet cap (amount and number of bets; 0 turns it off), and set how many net points it takes to move the line one step.

## Raspberry Pi setup (the Pi makes its own Wi-Fi)

The Pi broadcasts a **BuzzMarket** Wi-Fi network. People join it on their phones and the app pops up
automatically, like a hotel Wi-Fi login page. No internet or office network is needed.

You need Raspberry Pi OS **Bookworm or newer** (2023+). Do this once, while the Pi still has internet
(normal Wi-Fi or Ethernet):

```bash
git clone <this repo> ~/buzzword-polymarket && cd ~/buzzword-polymarket
sudo ./deploy/install.sh
```

It asks for a Wi-Fi name, a Wi-Fi password and your admin login, then:
- runs the app on boot at **http://10.42.0.1**
- turns the Pi's Wi-Fi into the hotspot, and sends any phone that joins straight to the app
- backs up the database every night into `backups/`, keeping 14 days
- makes QR codes for joining the Wi-Fi and opening the site, shown at **http://10.42.0.1/join**
  (put that on screen in the meeting)

⚠️ The last step takes the Pi off whatever Wi-Fi it was on. Run it with a keyboard and monitor, or over
SSH through an Ethernet cable, not over Wi-Fi.

**Things to know**
- The Pi has no internet while it's the hotspot. To update, plug in Ethernet and run `./deploy/update.sh`
  (it backs up the database first).
- Some phones warn "this Wi-Fi has no internet". Tell people to tap *Stay connected* / *Use without internet*.
- If the pop-up window forgets someone's login, they can open http://10.42.0.1 in Safari or Chrome instead.
- The Pi's built-in Wi-Fi is not a strong router. Some models are reported to handle only around 10
  phones at once in hotspot mode. Test with your group. If people get dropped, plug a cheap USB Wi-Fi
  adapter or a travel router into the Pi.
- To turn the hotspot off: `sudo nmcli connection down buzz-hotspot`. Turn it back on with `up`.
- Forgot the admin password? `python3 manage.py reset-password <username>`

**Not using the hotspot?** Just run `python3 manage.py serve` (port 8000) on any machine on your network.
Environment variables: `BUZZ_DB` (database path), `BUZZ_PORT`, `BUZZ_HOST`, `BUZZ_CAPTIVE_HOST`
(the hotspot address; turns on the pop-up redirect), and `BUZZ_SECURE_COOKIES=1` (set this if it's behind HTTPS).

## Tests

```bash
python3 -m unittest discover tests
```
