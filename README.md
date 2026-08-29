# GFTrade — Telegram trading bot for Solana with automated coin discovery

A Trojan-style Telegram trading bot (paste a mint address → token card with
one-tap buy buttons, positions with sell buttons, panic sell, wallet
management) combined with an **automated coin scanner**: it continuously
screens new Solana pairs on DexScreener, throws away anything that's paying
for promotion or looks manipulated, safety-checks the token contract
on-chain, scores what's left 0–100, and pushes the best candidates to you
as signal cards — or, if you enable autobuy, trades them itself with
monitored take-profit / stop-loss / trailing-stop exits.

Execution is through Jupiter (Solana's swap aggregator). Everything runs in
**dry-run mode by default**: real market data, simulated fills, paper
balance, zero on-chain transactions.

**Read this whole file before setting `DRY_RUN=false`.** This trades real
funds on irreversible on-chain transactions with no support line to call
when something goes wrong.

---

## ⚠️ Risk, in plain terms

- **New/small-cap Solana tokens are the highest-scam-density corner of
  crypto.** Honeypots (you can buy, can't sell), rug pulls, and wash-traded
  volume are the norm, not the exception. The filters and on-chain checks
  here reduce exposure to some of this — **they do not eliminate it. No
  filter can.** Deployers split holdings across fresh wallets, renounce
  authorities and rug through liquidity anyway, and adapt to whatever
  screens people use.
- **The scoring is a heuristic starting point, not a validated edge.**
  DexScreener doesn't expose historical OHLCV, so no backtest is possible —
  dry-run mode *is* the validation step. Run it for days, not minutes, and
  look hard at the trade log before risking anything.
- **Autobuy hands your wallet to the heuristics above.** It's off by
  default, sized small by default, and gated behind a stricter score — but
  ON means the bot spends real SOL with nobody in the loop.
- **Exits only fire while the bot is running** (see "Exit handling" below).
- **Nothing here is investment advice.** Fund the bot wallet only with
  money you are fully prepared to lose.

---

## Feature overview

**Trojan-style interactive trading**
- Paste any mint address → live token card (price, MC, liquidity, volume,
  txns, age) **plus** the scanner's verdict: 0–100 score, on-chain safety
  line, triggered patterns, and exactly why the scanner would reject it.
- One-tap buys at your preset sizes (editable), custom amounts, re-buys
  that average your entry.
- `/positions`: live PnL per position with 25% / 50% / 100% sell buttons.
- `/panic`: market-sell everything, with confirmation.
- Settings menu with buttons for everything runtime-tunable: presets,
  slippage, TP/SL, trailing stop, autobuy, score thresholds, max positions.
- Wallet: generate or import, balance view, guarded private-key export.
- Access control: every command and button is ignored unless it comes from
  a Telegram user id in `TELEGRAM_USER_IDS`. Multiple ids share one bot —
  same wallet, positions, and settings, and everyone gets every alert —
  but private-key export is restricted to the first id (the primary owner).

**Automated coin discovery ("find good coins")**
- Rolling candidate pool from DexScreener's newest token profiles,
  **re-checked every tick** — a token that was too young or too thin ten
  minutes ago gets re-evaluated until it qualifies or ages out.
- Hard screens: age window, liquidity floor, liquidity/market-cap sanity
  band (the core anti-manipulation check), volume floor, organic-activity
  floors, wash-trading ratio cap, and exclusion of anything paying
  DexScreener for placement.
- On-chain safety checks via Solana RPC: mint authority renounced
  ("contract renounced"), no freeze authority (the honeypot mechanic),
  top-10 holder concentration (excluding the LP account).
- LP lock check via RugCheck: at least `MIN_LP_LOCKED_PCT` (default 80%)
  of the pool's LP tokens must be locked or burned, so the deployer can't
  simply pull the liquidity. In strict mode (default) a token whose LP
  status can't be verified is rejected — no lock proof, no trade.
- `/scan` lists every candidate passing the market screens, paged 5 at a
  time with ◀️ ▶️ arrows: fully-safe tokens (✅ renounced, no freeze,
  holder-sane, LP locked) rank first by score, the rest follow with a
  badge saying exactly why not (🚫 LP 5%, 🚫 mint active, ❓ unverified).
  Only ✅ tokens can ever be alerted or auto-bought — the rest are
  browse-only context.
- Momentum/accumulation patterns + a weighted 0–100 composite score.
- Signal cards with buy buttons; per-token mute; 24h re-alert cooldown.
- Optional autobuy above a stricter score, with TP/SL/trailing management
  and Telegram notification of every action.

---

## Setup

### 1. Install

```bash
pip install -r requirements.txt
```

### 2. Create the Telegram bot

1. Message **@BotFather** → `/newbot` → copy the token.
2. Message **@userinfobot** → copy your numeric user id (each person who
   should have access does this).

### 3. Configure

```bash
cp .env.example .env
# edit .env: TELEGRAM_BOT_TOKEN, TELEGRAM_USER_IDS
```

`TELEGRAM_USER_IDS` is comma-separated; list yourself first — the first id
is the primary owner and the only one who can export the wallet key.
Everyone listed can otherwise do everything, including spending from the
shared wallet in live mode — only add people you trust with the money in
it. Each person must open the bot and press **Start** once before it can
message them.

`DRY_RUN=true` is the default — leave it while evaluating.

### 4. Run

```bash
python main.py
```

Open your bot in Telegram and send `/start`. Paste any mint address to see
a card; send `/scan` to sweep for candidates on demand; the background
scanner alerts you as qualifying tokens appear.

### 5. Watch dry-run until it earns your trust

The bot screens real market data and simulates fills against live prices
with a 1-SOL paper wallet (`/trades` shows win rate and paper PnL). If the
results don't look reasonable **after days of observation**, tune the
thresholds (`/settings` for runtime knobs, `gftrade/config.py` for the
screening constants) — don't go live hoping it improves.

### 6. Going live (only after step 5 informed you of something)

```bash
python -m gftrade.wallet        # creates wallet.json — plaintext private key!
# fund the printed address with a SMALL amount of SOL
# in .env:
#   DRY_RUN=false
#   SOLANA_RPC_URL=<a paid RPC — Helius/QuickNode/Triton>
#   JUPITER_API_KEY=<free key from portal.jup.ag>
python main.py
```

Wallet security, non-negotiable:
- `wallet.json` is your private key in plaintext. It's already in
  `.gitignore`; never move it out, upload it, or paste it anywhere.
- This is a **hot wallet for bot capital only**. Never import your main
  wallet.
- The public mainnet RPC rate-limits aggressively; for live trading use a
  paid RPC or exits may lag exactly when you need them.

---

## Running it on a server

Exits (TP / stop-loss / trailing) only fire while the bot is running, so a
server that keeps it alive — through SSH disconnects and reboots — is the
right home for it.

Fresh Ubuntu/Debian doesn't ship `pip`, and recent versions refuse
system-wide installs (PEP 668) — use a virtualenv:

```bash
apt update && apt install -y python3-venv python3-pip
cd ~/GFTrade
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # then edit .env, and: chmod 600 .env
.venv/bin/python main.py   # foreground test first
```

Then run it under systemd so it restarts on failure and comes back after
reboots (adjust paths if you didn't clone to /root/GFTrade):

```ini
# /etc/systemd/system/gftrade.service
[Unit]
Description=GFTrade Telegram trading bot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/root/GFTrade
ExecStart=/root/GFTrade/.venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now gftrade
journalctl -u gftrade -f     # live logs
```

The bot messages every authorized user on startup, so you'll know each
time it comes back. `systemctl stop gftrade` before editing config; if
you'll keep it stopped for long in live mode, close positions first.

## Commands

| Command | What it does |
|---|---|
| `/start` | Main menu: mode, scanner status, positions/PnL summary |
| `/scan` | Sweep now; ranked + safety-badged candidate list, paged with ◀️ ▶️ |
| `/buy <mint> [SOL]` | Token card — or instant buy when an amount is given |
| *(paste a mint)* | Same as `/buy <mint>` |
| `/positions` (`/sell`) | Open positions, live PnL, sell buttons |
| `/trades` | Win rate, realized PnL, recent closed trades |
| `/settings` | Every runtime-tunable knob, as buttons |
| `/wallet` | Address + balance (paper balance in dry-run) |
| `/mute <mint>` | Never alert this token again |
| `/panic` | Market-sell every open position (asks to confirm) |
| `/help` | Cheat sheet |

---

## How discovery works (and how to tune it)

Pipeline per scan tick (default every 90s):

1. **Manage positions first** — update price peaks, fire TP / SL /
   trailing exits.
2. **Ingest** DexScreener's latest token profiles (the closest keyless
   proxy for "new token") into a rolling pool of up to 400 candidates.
3. **Re-check the pool** in batches against live pair data.
4. **Hard screens** (`gftrade/discovery/filters.py`, thresholds in
   `config.py`): every rejection is logged with its reason.
5. **Safety checks** (`discovery/safety.py`): mint/freeze authority and
   holder concentration from Solana RPC, plus LP locked/burned percentage
   from RugCheck's public API (`clients/rugcheck.py`) — reading LP locks
   generically from raw RPC would mean parsing every DEX's pool layout.
   `security_strict` (in `/settings`) decides whether an *unknown* result
   (RPC or RugCheck down) rejects or passes. If RugCheck is unreachable
   from your server, every token shows `LP ❓` and strict mode will pass
   nothing — either fix connectivity or set `LP_CHECK_ENABLED=false` in
   the environment to drop the LP requirement.
6. **Patterns + score** (`discovery/patterns.py`, `discovery/scoring.py`):
   alert requires a triggered pattern **and** score ≥ `min_alert_score`;
   autobuy additionally requires score ≥ `min_autobuy_score`.

Key `config.py` constants to tune while watching the logs:

- `MIN_PAIR_AGE_MINUTES` / `MAX_PAIR_AGE_HOURS` — the freshness window
- `MIN_LIQUIDITY_USD`, `MIN/MAX_LIQ_TO_MCAP_RATIO` — the
  manipulation-sanity core of the system
- `MIN_BUYS_5M`, `MIN_BUYS_H1`, `MAX_BUY_SELL_IMBALANCE` — organic-activity
  floors
- `MAX_TOP10_HOLDER_PCT` — holder-concentration cap

Add your own pattern: write a function in `discovery/patterns.py` with
signature `(pair: dict) -> (triggered, confidence 0-1, name)` using any
fields from DexScreener's pair object, append it to `ALL_PATTERNS`.

## Exit handling

TP / stop-loss / trailing exits are **monitored by the bot**: each tick it
checks live prices and market-sells through Jupiter when a level is
crossed. This keeps one code path that behaves identically in dry-run and
live, makes partial/button sells always work, and enables the trailing
stop — but it means **exits do not fire while the bot is down**. Run it
somewhere reliable, and close positions before stopping it for long.

The alternative — Jupiter Trigger (on-chain limit) orders — survives the
bot dying but has changed API shape repeatedly and fights with
Trojan-style button sells (every partial sell needs order cancels). If you
want belt-and-suspenders exits for live positions, that's the extension
point: `gftrade/clients/jupiter.py` is where a trigger-order client would
sit (see https://dev.jup.ag/docs/trigger for the current contract).

## Architecture

```
main.py                     wiring + lifecycle (bot + scanner task)
gftrade/
  config.py                 env secrets, strategy constants, DEFAULT_SETTINGS
  store.py                  atomic JSON state: settings/positions/trades/alerts
  wallet.py                 keypair generate/import/load/export
  solana_rpc.py             thin async JSON-RPC client (httpx + solders only)
  clients/dexscreener.py    market data + discovery feeds (keyless)
  clients/jupiter.py        quote -> build -> sign -> send -> confirm
  discovery/filters.py      hard screens with logged rejection reasons
  discovery/safety.py       mint/freeze authority, holder concentration
  discovery/patterns.py     momentum/accumulation triggers
  discovery/scoring.py      weighted 0-100 composite score
  trading/engine.py         buys/sells/exits; dry-run simulation vs live swaps
  scanner.py                candidate pool + tick loop + /scan
  tg/                       handlers, keyboards, formatting, app wiring
tests/                      60 offline tests (fixture pairs, fake clients)
```

Run the tests:

```bash
pip install -r requirements-dev.txt
python -m pytest
```

## Deliberately not included

Copy-trading, launch sniping via on-chain listeners (buying in the same
block as pool creation), multi-wallet support, DCA/limit *entry* orders,
and MEV-protected sends (Jito bundles) are all things Trojan-class bots
offer that this codebase intentionally leaves out — each is either an
order of magnitude more infrastructure or an order of magnitude more risk.
The module boundaries above are designed so any of them can be added
without rewriting what's here.
