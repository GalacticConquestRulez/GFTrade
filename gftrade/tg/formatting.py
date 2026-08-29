"""
All user-visible message text, rendered as Telegram HTML.

Every dynamic string that originates outside this program (token symbols,
names, error text, API fields) MUST go through esc() — token creators
control their own metadata and will happily name a token `<b>` or worse.
"""
import html
import math
import time

from .. import config, constants
from ..discovery import filters


def esc(value) -> str:
    return html.escape(str(value), quote=False)


# ---------- number formatting ----------

def fmt_usd(value) -> str:
    value = float(value or 0)
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(value) >= div:
            return f"${value / div:,.2f}{suffix}"
    return f"${value:,.0f}"


def fmt_price(value) -> str:
    value = float(value or 0)
    if value <= 0:
        return "$?"
    if value >= 1:
        return f"${value:,.4f}"
    decimals = min(-math.floor(math.log10(value)) + 3, 12)
    return f"${value:.{decimals}f}"


def fmt_sol(value) -> str:
    return f"{value:,.4f} SOL"


def fmt_pct(value, signed: bool = True) -> str:
    return f"{value:+.1f}%" if signed else f"{value:.1f}%"


def fmt_age(hours: float) -> str:
    if hours < 0:
        return "?"
    if hours < 1:
        return f"{hours * 60:.0f}m"
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def mode_banner(dry_run: bool) -> str:
    return "🧪 DRY RUN" if dry_run else "🔴 LIVE"


# ---------- cards ----------

def token_card(pair: dict, safety=None, score=None, breakdown=None,
               pattern_hits=None, header: str = None) -> str:
    base = pair.get("baseToken") or {}
    symbol, name = esc(base.get("symbol") or "?"), esc(base.get("name") or "?")
    mint = base.get("address") or "?"
    liquidity = (pair.get("liquidity") or {}).get("usd") or 0
    market_cap = pair.get("marketCap") or pair.get("fdv") or 0
    volume = pair.get("volume") or {}
    change = pair.get("priceChange") or {}
    txns_5m = (pair.get("txns") or {}).get("m5") or {}
    age = filters.pair_age_hours(pair)
    ratio = f"{liquidity / market_cap * 100:.1f}%" if market_cap else "?"

    lines = []
    if header:
        lines.append(header)
    title = f"<b>{symbol}</b> — {name}"
    if score is not None:
        title += f"  |  score <b>{score}</b>/100"
    lines.append(title)
    lines.append(f"<code>{esc(mint)}</code>")
    lines.append(
        f"💵 {fmt_price(pair.get('priceUsd'))}  ·  MC {fmt_usd(market_cap)}  ·  "
        f"💧 {fmt_usd(liquidity)} ({ratio} of MC)"
    )
    lines.append(
        f"📈 5m {fmt_pct(change.get('m5', 0) or 0)} · 1h {fmt_pct(change.get('h1', 0) or 0)} · "
        f"6h {fmt_pct(change.get('h6', 0) or 0)} · 24h {fmt_pct(change.get('h24', 0) or 0)}"
    )
    lines.append(
        f"🔊 Vol 5m {fmt_usd(volume.get('m5', 0))} · 1h {fmt_usd(volume.get('h1', 0))} · "
        f"24h {fmt_usd(volume.get('h24', 0))}"
    )
    lines.append(
        f"🛒 5m: {txns_5m.get('buys', 0)} buys / {txns_5m.get('sells', 0)} sells  ·  "
        f"⏱ {fmt_age(age)} old  ·  {esc(pair.get('dexId') or '?')}"
    )
    if safety is not None:
        lines.append(f"🔒 {safety.line()}")
    if pattern_hits:
        shown = ", ".join(f"{esc(h['pattern'])} ({h['confidence']:.2f})" for h in pattern_hits)
        lines.append(f"📊 {shown}")
    if breakdown:
        parts = " · ".join(f"{k[:3]} {v:g}" for k, v in breakdown.items())
        lines.append(f"🧮 {parts}")
    return "\n".join(lines)


def signal_card(verdict: dict) -> str:
    return token_card(
        verdict["pair"], verdict["safety"], verdict["score"], verdict["breakdown"],
        verdict["patterns"], header="🚨 <b>New signal</b>",
    )


def autobuy_card(verdict: dict, result: dict) -> str:
    position = result["position"]
    text = token_card(
        verdict["pair"], verdict["safety"], verdict["score"], verdict["breakdown"],
        verdict["patterns"],
        header=f"🤖 <b>Auto-bought</b> [{mode_banner(position['dry_run'])}]",
    )
    return text + "\n" + buy_receipt(result, self_contained=False)


def buy_receipt(result: dict, self_contained: bool = True) -> str:
    position = result["position"]
    lines = []
    if self_contained:
        action = "Added to" if result.get("merged") else "Opened"
        lines.append(
            f"✅ [{mode_banner(position['dry_run'])}] <b>{action} {esc(position['symbol'])}</b>"
        )
    lines.append(
        f"💰 Spent {fmt_sol(position['sol_spent'])} → {position['token_amount']:,.2f} tokens "
        f"@ {fmt_price(result['price_usd'])}"
    )
    if result.get("merged"):
        lines.append(f"⚖️ New average entry {fmt_price(position['entry_price_usd'])}")
    lines.append(
        f"🎯 TP {fmt_price(position['tp_price_usd'])}  ·  🛑 SL {fmt_price(position['sl_price_usd'])}"
    )
    if result.get("signature"):
        url = constants.SOLSCAN_TX_URL.format(sig=result["signature"])
        lines.append(f'🧾 <a href="{url}">transaction</a>')
    return "\n".join(lines)


def sell_receipt(result: dict) -> str:
    if result["closed"]:
        trade = result["trade"]
        emoji = "🟢" if trade["pnl_sol"] >= 0 else "🔴"
        lines = [
            f"{emoji} [{mode_banner(trade['dry_run'])}] <b>Closed {esc(trade['symbol'])}</b> "
            f"({esc(trade['result'])})",
            f"PnL {trade['pnl_sol']:+.4f} SOL ({fmt_pct(trade['pnl_pct'])})  ·  "
            f"exit {fmt_price(trade['exit_price_usd'])}",
        ]
    else:
        position = result["position"]
        lines = [
            f"✂️ [{mode_banner(position['dry_run'])}] <b>Sold {result['pct']:g}% of "
            f"{esc(position['symbol'])}</b> for {fmt_sol(result['sol_out'])} "
            f"@ {fmt_price(result['price_usd'])}",
            f"Remaining: {position['token_amount']:,.2f} tokens",
        ]
    if result.get("signature"):
        url = constants.SOLSCAN_TX_URL.format(sig=result["signature"])
        lines.append(f'🧾 <a href="{url}">transaction</a>')
    return "\n".join(lines)


def exit_event_text(event: dict) -> str:
    emoji = {"take_profit": "🎯", "stop_loss": "🛑", "trailing_stop": "📉"}.get(
        event["reason"], "🔔"
    )
    return (
        f"{emoji} [{mode_banner(event['dry_run'])}] <b>{esc(event['symbol'])}</b> closed: "
        f"{esc(event['reason'])} — {event['pnl_sol']:+.4f} SOL ({fmt_pct(event['pnl_pct'])})"
    )


def exit_error_text(event: dict) -> str:
    return (
        f"⚠️ <b>EXIT FAILED</b> for {esc(event['symbol'])} ({esc(event['reason'])}):\n"
        f"{esc(event['error'])}\n"
        "The position is still open — intervene manually if this repeats."
    )


def positions_text(positions: dict, best_pairs: dict, sol_price: float) -> str:
    if not positions:
        return "No open positions."
    from ..trading.engine import price_in_sol  # local import to avoid a cycle

    blocks = []
    for mint, pos in positions.items():
        pair = best_pairs.get(mint)
        header = f"<b>{esc(pos['symbol'])}</b> [{mode_banner(pos['dry_run'])}] · {esc(pos['source'])}"
        if pair is None:
            blocks.append(f"{header}\n  price unavailable right now")
            continue
        price_usd = float(pair.get("priceUsd") or 0)
        change_pct = (
            (price_usd - pos["entry_price_usd"]) / pos["entry_price_usd"] * 100
            if pos["entry_price_usd"] else 0
        )
        value_sol = pos["token_amount"] * price_in_sol(pair, sol_price)
        unrealized = value_sol + pos["sol_received"] - pos["sol_spent"]
        emoji = "🟢" if change_pct >= 0 else "🔴"
        lines = [
            header,
            f"  {emoji} entry {fmt_price(pos['entry_price_usd'])} → now {fmt_price(price_usd)} "
            f"({fmt_pct(change_pct)})",
            f"  value {fmt_sol(value_sol)} · in {fmt_sol(pos['sol_spent'])} · "
            f"est PnL {unrealized:+.4f} SOL",
            f"  🎯 {fmt_price(pos['tp_price_usd'])} · 🛑 {fmt_price(pos['sl_price_usd'])} · "
            f"peak {fmt_price(pos['peak_price_usd'])}",
        ]
        if pos["sol_received"] > 0:
            lines.append(f"  recovered so far {fmt_sol(pos['sol_received'])}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def trades_text(summary: dict, last_trades: list, dry_run: bool) -> str:
    lines = [f"<b>Performance</b> [{mode_banner(dry_run)}]"]
    win_rate = f"{summary['win_rate'] * 100:.0f}%" if summary["win_rate"] is not None else "—"
    lines.append(
        f"Closed: {summary['closed_trades']} · Win rate: {win_rate} · "
        f"Realized PnL: {summary['realized_pnl_sol']:+.4f} SOL"
    )
    if dry_run:
        lines.append(f"Paper balance: {fmt_sol(summary['sim_balance_sol'])} "
                     f"(started {fmt_sol(config.SIM_START_BALANCE_SOL)})")
    if last_trades:
        lines.append("\n<b>Recent</b>")
        for trade in reversed(last_trades[-10:]):
            emoji = "🟢" if trade["pnl_sol"] >= 0 else "🔴"
            lines.append(
                f"{emoji} {esc(trade['symbol'])}: {esc(trade['result'])} "
                f"{trade['pnl_sol']:+.4f} SOL ({fmt_pct(trade['pnl_pct'])})"
            )
    return "\n".join(lines)


def safety_flag(verdict: dict) -> str:
    """Compact per-row safety verdict for the /scan list: ✅ proven safe,
    🚫 with the first known-bad reason, ❓ when it couldn't be verified."""
    if verdict.get("safety_ok"):
        return "✅"
    report = verdict.get("safety")
    if report is None:
        return "🚫"
    if report.mint_renounced is False:
        return "🚫 mint active"
    if report.freeze_none is False:
        return "🚫 freeze on"
    if report.top10_pct is not None and report.top10_pct > config.MAX_TOP10_HOLDER_PCT:
        return f"🚫 top10 {report.top10_pct:.0f}%"
    if config.LP_CHECK_ENABLED and report.lp_locked_pct is not None \
            and report.lp_locked_pct < config.MIN_LP_LOCKED_PCT:
        return f"🚫 LP {report.lp_locked_pct:.0f}%"
    return "❓ unverified"


def _is_unverified(verdict: dict) -> bool:
    report = verdict.get("safety")
    if report is None:
        return True
    return bool(report.error) or (
        config.LP_CHECK_ENABLED and report.lp_locked_pct is None
    )


def scan_page_text(verdicts: list, page: int, page_size: int) -> str:
    if not verdicts:
        return (
            "Scan finished: nothing currently passes the market screens "
            "(age window, liquidity, volume, organic activity).\n"
            "That's normal in quiet stretches — the background scanner "
            "keeps watching and will alert you when something qualifies."
        )
    total_pages = (len(verdicts) + page_size - 1) // page_size
    start = page * page_size
    safe_count = sum(1 for v in verdicts if v.get("safety_ok"))
    unverified = sum(1 for v in verdicts if not v.get("safety_ok") and _is_unverified(v))
    lines = [
        f"<b>Best candidates right now</b> — {len(verdicts)} screened, "
        f"page {page + 1}/{total_pages}",
        f"✅ {safe_count} pass every safety check · only those can be "
        "alerted or auto-bought",
    ]
    if unverified >= 3 and unverified * 2 >= len(verdicts):
        lines.append(
            "⚠️ <i>Safety data unavailable for most (❓) — your RPC or "
            "RugCheck is likely rate-limiting. ❓ means unverified, not "
            "safe. A free Helius/QuickNode RPC usually fixes this.</i>"
        )
    lines.append("")
    for offset, verdict in enumerate(verdicts[start:start + page_size]):
        pair = verdict["pair"]
        base = pair.get("baseToken") or {}
        pattern = verdict["patterns"][0]["pattern"] if verdict["patterns"] else "no pattern"
        lines.append(
            f"{safety_flag(verdict)} <b>#{start + offset + 1} "
            f"{esc(base.get('symbol') or '?')}</b> — "
            f"{verdict['score']}/100 · {esc(pattern)}\n"
            f"    liq {fmt_usd((pair.get('liquidity') or {}).get('usd'))} · "
            f"MC {fmt_usd(pair.get('marketCap') or pair.get('fdv'))} · "
            f"1h {fmt_pct((pair.get('priceChange') or {}).get('h1', 0) or 0)} · "
            f"{fmt_age(filters.pair_age_hours(pair))}"
        )
    lines.append("\nTap a token for its full card + buy buttons; ◀️ ▶️ to page.")
    return "\n".join(lines)


def settings_text(settings: dict, dry_run: bool) -> str:
    presets = ", ".join(f"{p:g}" for p in settings["buy_presets"])
    trailing = (
        f"{settings['trailing_stop_pct']:g}%" if settings["trailing_stop_pct"] > 0 else "off"
    )
    return "\n".join([
        f"<b>Settings</b> [{mode_banner(dry_run)}]",
        "",
        f"🔎 Scanner: {'on' if settings['scanner_on'] else 'off'}",
        f"🤖 Autobuy: {'ON' if settings['autobuy'] else 'off'}"
        f" · {settings['autobuy_sol']:g} SOL/entry · min score {settings['min_autobuy_score']}",
        f"🚨 Alert min score: {settings['min_alert_score']}",
        f"🛡 Security checks: {'strict (unknown = reject)' if settings['security_strict'] else 'lenient (unknown = allow)'}",
        "",
        f"💰 Buy presets: {presets} SOL",
        f"📉 Slippage: {settings['slippage_bps'] / 100:g}%",
        f"🎯 Take profit: {settings['take_profit_pct']:g}% · 🛑 Stop loss: {settings['stop_loss_pct']:g}%",
        f"📉 Trailing stop: {trailing}",
        f"📂 Max positions: {settings['max_positions']}",
        "",
        "Dry-run vs live is set by the DRY_RUN environment variable, not here — "
        "changing money-mode should require touching the deployment.",
    ])


def wallet_text(dry_run: bool, address: str = None, balance_sol: float = None,
                sim_balance: float = None) -> str:
    if dry_run:
        return "\n".join([
            "🧪 <b>Dry-run wallet</b>",
            f"Paper balance: {fmt_sol(sim_balance if sim_balance is not None else 0)}",
            "",
            "No real wallet is loaded or needed in dry-run mode. Set DRY_RUN=false "
            "(after reading the README's risk section) to trade with a real one.",
        ])
    url = constants.SOLSCAN_TOKEN_URL.format(mint=address)
    return "\n".join([
        "🔴 <b>Live wallet</b>",
        f"<code>{esc(address)}</code>",
        f"Balance: {fmt_sol(balance_sol or 0)}",
        f'<a href="{url}">view on Solscan</a>',
        "",
        "This is a hot wallet — keep only what the bot is allowed to lose in it.",
    ])


def start_text(dry_run: bool, summary: dict, scanner_on: bool, pool_size: int,
               last_tick_at: float = None) -> str:
    tick = "never"
    if last_tick_at:
        tick = f"{max(0, time.time() - last_tick_at):.0f}s ago"
    win_rate = f"{summary['win_rate'] * 100:.0f}%" if summary["win_rate"] is not None else "—"
    return "\n".join([
        f"<b>GFTrade</b> — {mode_banner(dry_run)}",
        "",
        f"🔎 Scanner {'on' if scanner_on else 'OFF'} · watching {pool_size} candidates · "
        f"last sweep {tick}",
        f"📂 {summary['open_positions']} open · {summary['closed_trades']} closed · "
        f"win rate {win_rate} · PnL {summary['realized_pnl_sol']:+.4f} SOL",
        "",
        "Paste any Solana token address for an instant card with buy buttons, "
        "or use the menu below.",
    ])


def help_text() -> str:
    return "\n".join([
        "<b>Commands</b>",
        "/start — main menu + status",
        "/scan — sweep the market now, show the top screened candidates",
        "/buy <code>&lt;mint&gt;</code> <code>[SOL]</code> — token card, or instant buy with an amount",
        "/positions — open positions with sell buttons",
        "/trades — performance and recent closed trades",
        "/settings — everything tunable at runtime",
        "/wallet — address and balance",
        "/mute <code>&lt;mint&gt;</code> — never alert this token again",
        "/panic — market-sell every open position (asks to confirm)",
        "",
        "Pasting a token mint address as a plain message opens its card.",
        "",
        "The background scanner screens new Solana pairs (DexScreener), drops "
        "promoted/manipulated-looking ones, safety-checks each token "
        "(renounced mint, no freeze authority, holder spread, LP locked), "
        "scores the rest 0-100, and alerts with buy buttons. Autobuy (off by "
        "default) acts on the strongest signals automatically. TP/SL/trailing "
        "exits are monitored by the bot — they only fire while the bot is "
        "running.",
    ])
