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
- Two feeds into a rolling candidate pool, **re-checked every tick** — a
  token that was too young or too thin ten minutes ago gets re-evaluated
  until it qualifies or ages out:
  - GeckoTerminal's new-pools feed: every fresh Solana pool (pump.fun
    graduations included) within minutes of creation — the early feed.
  - DexScreener's token profiles: tokens whose creators made a profile —
    later, but carries tokens the pool feed has already rotated past.
- Hard screens: age window, liquidity floor, liquidity/market-cap sanity
  band (the core anti-manipulation check), volume floor, organic-activity
  floors, wash-trading ratio cap, and exclusion of anything paying
  DexScreener for placement.
- On-chain safety checks via Solana RPC: mint authority renounced
  ("contract renounced"), no freeze authority (the honeypot mechanic),
  top-10 holder concentration (excluding the LP account), and classic SPL
  token program only — Token-2022 mints are rejected as known-risk, since
  their extensions (transfer hooks/fees, permanent delegates) enable
  sell-traps the authority checks can't see.
- Sellability evidence in the market screens: a coin with real buy flow
  and zero recorded sells is rejected as a honeypot signature — dry-run
  "wins" on such coins would be fiction, since the sim can't know a sell
  transaction would have failed on-chain.
- LP lock check via a chain of sources, cheapest and most trustworthy
  first. Rung 0 is **our own RPC reading the chain directly** (no
  third-party rate limits): for classic Raydium AMM v4 pools the pool
  account is parsed (validated by program owner, layout size, and that
  its base/quote mints match the pair — any mismatch means "no verdict",
  never a wrong verdict), burned LP is computed as lpReserve minus the
  LP mint's remaining supply, and remaining LP custodied by the
  incinerator or a recognized locker (Streamflow, Jupiter Lock — an
  extendable list in `discovery/lp_onchain.py`) counts as locked, shown
  as `·chain`. The on-chain reading is accepted ONLY as proof of a lock
  (>= threshold); a low reading is never trusted as unlock evidence —
  our locker list can't be exhaustive — and falls through to the APIs.
  Locker unlock timestamps are not yet read (burned LP needs none).
  Then RugCheck, then GoPlus Security
  as the independent keyless backup when RugCheck is down, rate-limited,
  or hasn't indexed a coin. At least `MIN_LP_LOCKED_PCT` (default 80%)
  of the main pool's LP must be locked or burned. Evidence rules are
  deliberately conservative: when RugCheck's per-market numbers conflict
  (a burned dust pool next to an unlocked real pool) the verdict is
  "unknown" and the backup decides by pool TVL; GoPlus can *prove* a
  lock but never proves an unlock, so a 🚫 banishment always rests on
  positive evidence. GoPlus also backfills mint/freeze authority when
  direct RPC reads fail. In strict mode (default) a token whose LP
  status can't be verified by either source is rejected — no lock proof,
  no trade. (DexScreener's public API exposes no lock data at all — the
  website's padlock comes from private frontend endpoints — and
  GeckoTerminal's lock field refreshes only daily, useless for fresh
  launches; that's why the chain is RugCheck → GoPlus.) A third,
  structural rung fires only when both APIs answer unknown: pump.fun and
  Raydium LaunchLab bonding-curve pairs have no LP tokens at all
  (liquidity is program escrow — nothing to pull, shown `·curve`), and
  PumpSwap pools for pump.fun-minted coins were created by the
  graduation migration, which locks liquidity permanently (shown `·pf`).
  Plain Raydium pools are deliberately excluded from this inference —
  anyone can open one and keep the LP tokens, and a "pump"-suffix mint
  can be vanity-ground to spoof suffix-based checks — so Raydium pools
  must prove their lock through RugCheck/GoPlus like everyone else, and
  real evidence from either API always overrides the structural answer.
- **Known-risky coins are never listed, period.** Any coin with a
  known-bad check — unlocked LP, live mint or freeze authority,
  Token-2022, whale-heavy holders — is banished from `/scan` entirely
  (the header shows a 🛡 count of what was removed), can never alert or
  autobuy, and even near-miss filler is safety-checked before it may
  appear. Pasting such a mint manually still shows its card, but it
  leads with the specific dangers and loses one-tap buy buttons — only
  the typed-amount path remains.
- `/scan` lists what's left, paged 5 at a time with ◀️ ▶️ arrows: 🟢 safe
  coins (everything proven, LP lock included) first, then 🟡 unverified,
  each tier sorted by **pure market-quality score** (no safety points) —
  quality and risk are separate axes, never one blended number.
  By default only ✅ tokens alert; the `alert_unverified` setting extends
  alerts to ❓-only coins (nothing known-bad, some checks incomplete),
  clearly labeled for small manual flips. Known-bad (🚫) tokens never
  alert, and autobuy only ever touches ✅.
- Momentum/accumulation patterns + a weighted 0–100 composite score.
- Signal cards with buy buttons; per-token mute; 24h re-alert cooldown.
- Optional autobuy above a stricter score, with TP/SL/trailing management
  and Telegram notification of every action.
- **Signal report card**: every signal's price is checkpointed 1h/6h/24h
  later (a vanished market counts as −100%), and `/trades` shows
  per-pattern hit rates and medians — tune thresholds on evidence, not
  vibes.

**Staged exits**
- At take-profit, only `tp_sell_pct` (default 50%) is sold; the remainder
  becomes a *runner* protected by a `runner_trailing_pct` (default 20%)
  trailing stop off the peak that never **triggers** below the entry
  price. You bank the base hit and keep exposure to the runners that go
  5–10x. Set `tp_sell_pct` to 100 in `/settings` for classic
  all-out-at-TP behavior. (Honesty note: the floor governs when the exit
  *fires* — a violent one-tick crash can still fill below entry, because
  these are monitored stops, not resting orders.)

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

`/scan` is also the tuning feedback loop: when fewer than 10 coins pass
the screens, the list fills with the best-scored **near-misses**, each
marked 🔻 with the exact threshold it failed ("liquidity $7,400 below
floor $10,000"). If the same reason keeps appearing on coins you'd want
to see, loosen that threshold — the big five (min liquidity, min 1h
volume, min 1h buys, min age in minutes, max age in hours) are editable
live from `/settings`, no restart needed. Near-misses are display-only:
they can never be alerted or auto-bought.

Min age deserves its own warning: the default 20-minute delay exists
because a pair's first minutes are peak rug/honeypot territory. Lowering
it (0 disables the delay) lets alerts and autobuy reach coins minutes
after launch — autobuy still demands every safety check pass, but
younger means less history for every heuristic to chew on.

Age is a two-dial system: the global min age gates screening/alerts,
and `autobuy_min_age_minutes` (also in `/settings`, 0 = no extra wait)
adds a separate, usually higher bar before the bot's own buying acts —
so alerts can fire at 5 minutes for manual flip decisions while autobuy
waits until a coin has survived to 15–20.

The remaining constants live in `config.py` (server edit + restart):

- `MIN/MAX_LIQ_TO_MCAP_RATIO` — the manipulation-sanity core of the system
- `MIN_BUYS_5M`, `MAX_BUY_SELL_IMBALANCE` — short-window activity shape
- `MAX_TOP10_HOLDER_PCT` — holder-concentration cap
- `MIN_LP_LOCKED_PCT` — the LP-lock bar

`/start` shows live diagnostics: bot version, candidate-pool size, last
sweep's checked/passed/signal counts, and per-feed health (profiles /
new-pools ✓ ∅ ✗) — the first place to look when results seem thin.

Add your own pattern: write a function in `discovery/patterns.py` with
signature `(pair: dict) -> (triggered, confidence 0-1, name)` using any
fields from DexScreener's pair object, append it to `ALL_PATTERNS`.

## Two price sources

Market data comes from two independent keyless APIs with distinct jobs:
DexScreener carries screening and exit checks (it's the only one with the
txn/volume detail the filters need); GeckoTerminal carries the background
price checkpoints (signal report card, factor log) so that load never
competes with exits — and acts as the automatic failover when DexScreener
rate-limits or errors, so stop-losses keep firing through an outage. A
token counts as dead for checkpoint purposes only when *both* sources
fail to price it past the grace window. (Other aggregators — dex.guru,
Defined.fi, GMGN, Ave — need API keys or have no public API, so they're
deliberately not integrated.)

## Trend gating, factor logging, and analysis

DexScreener has no OHLCV, so the bot builds its own price memory: every
discovery sweep records each candidate's price into a rolling ~2h buffer
(`discovery/trend.py`, in-memory — it refills within a couple of ticks
after a restart). Two features run on it:

- **Extension gate** (`max_entry_extension_pct`, default 60, 0 = off):
  alerts and autobuy are blocked when price already sits more than that
  far above its own 1h low — a +80%-off-the-low entry is late in the
  move, which is exactly how a pump gets bought at the top and ridden
  through the stop. Token cards show "↗ +X% off its 1h low"; manual buys
  are never blocked, just informed.
- **Volatility-scaled exits** (`vol_scaled_exits` toggle, off by
  default): TP/SL percentages are multiplied by the token's own recent
  volatility relative to `VOL_REFERENCE_PCT`, clamped ×0.5–×2.0 — so a
  wild coin gets room to breathe and a calm one exits tighter. Falls back
  to your flat percentages when history is thin; receipts show the
  applied factor.

**Factor log** (`factors.py`, SQLite in `factor_log.db`): every candidate
the scanner evaluates — passed or failed, traded or not — has 18 factors
snapshotted (liquidity, mcap ratio, age, buy/sell flow, volume surge,
price changes, pattern confidence, score, extension …), deduped to one
row per coin per 30 minutes. Outcomes close the loop from both ends:
price checkpoints at 1h/6h/24h (a vanished market records −100%), and
real trade results attached when a position opened on a logged coin
closes. **`/factors`** (or `python -m gftrade.analysis`) ranks every
factor by correlation with 24h returns and with actual trade wins, plus
average factor values in wins vs losses — printed with the two caveats
that matter: correlation isn't causation, and small samples lie.

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
