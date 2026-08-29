"""
Command handlers, the pasted-mint flow, and the inline-button dispatcher.

Every entry point is gated by authorized_only: this bot drives a wallet, so
any update not from a user in config.AUTHORIZED_IDS is dropped without a
reply (replying would confirm the bot exists to whoever is probing it).
Authorized users share everything — wallet, positions, settings, alerts —
except private-key export, which only the primary owner (the first id in
the list) can perform.
"""
import asyncio
import contextlib
import functools
import logging
import re
import time

from solders.pubkey import Pubkey
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from .. import config, constants, wallet as wallet_mod
from ..trading.engine import TradeError
from . import formatting as fmt
from . import keyboards as kb

logger = logging.getLogger(__name__)

MINT_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


def deps_of(context: ContextTypes.DEFAULT_TYPE):
    return context.application.bot_data["deps"]


def is_authorized(user_id) -> bool:
    return user_id in config.AUTHORIZED_IDS


def authorized_only(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        chat = update.effective_chat
        if (user and is_authorized(user.id)) or (chat and is_authorized(chat.id)):
            return await func(update, context)
        logger.warning("ignored update from unauthorized user=%s chat=%s",
                       user.id if user else None, chat.id if chat else None)
    return wrapper


def extract_mint(text: str):
    for match in MINT_RE.findall(text or ""):
        try:
            Pubkey.from_string(match)
            return match
        except Exception:
            continue
    return None


async def safe_edit(query, text: str, reply_markup=None):
    try:
        await query.edit_message_text(
            text, reply_markup=reply_markup, parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


# ---------- shared views ----------

async def render_token_view(deps, mint: str):
    """Token card + buy keyboard for any mint. Runs the same evaluation the
    scanner uses so the card shows score/safety, plus why the scanner would
    reject it (if it would)."""
    pair = await deps.dex.best_pair(constants.CHAIN_ID, mint)
    if pair is None:
        return None, None
    verdict = await deps.scanner.evaluate_pair(pair, set())
    text = fmt.token_card(
        pair, verdict["safety"], verdict["score"], verdict["breakdown"],
        verdict["patterns"], extension_pct=verdict.get("extension_pct"),
    )
    if not verdict["screened_ok"]:
        shown = "\n".join(f"  · {fmt.esc(r)}" for r in verdict["reject_reasons"][:4])
        text += f"\n⚠️ <b>Scanner would reject this:</b>\n{shown}"
    markup = kb.token_kb(mint, deps.store.settings["buy_presets"], pair.get("url"))
    return text, markup


async def positions_view(deps):
    positions = deps.store.positions
    best_pairs = {}
    sol_price = 0.0
    if positions:
        pairs = await deps.dex.pairs_for_tokens(constants.CHAIN_ID, list(positions.keys()))
        for pair in pairs:
            mint = (pair.get("baseToken") or {}).get("address")
            liq = (pair.get("liquidity") or {}).get("usd") or 0
            best = best_pairs.get(mint)
            if best is None or liq > ((best.get("liquidity") or {}).get("usd") or 0):
                best_pairs[mint] = pair
        sol_price = await deps.dex.sol_price_usd()
    text = fmt.positions_text(positions, best_pairs, sol_price)
    return text, kb.positions_kb(positions)


async def wallet_view(deps):
    if deps.engine.dry_run:
        text = fmt.wallet_text(True, sim_balance=deps.store.stats["sim_balance_sol"])
    else:
        address = str(deps.keypair.pubkey())
        balance = await deps.rpc.get_balance_sol(address)
        text = fmt.wallet_text(False, address=address, balance_sol=balance)
    return text, kb.wallet_kb(deps.engine.dry_run)


def start_view(deps):
    from .. import __version__
    text = fmt.start_text(
        deps.engine.dry_run, deps.store.summary(),
        deps.store.settings["scanner_on"], len(deps.scanner.pool),
        deps.scanner.last_tick_at, deps.scanner.last_tick_stats,
        deps.scanner.feed_status, version=__version__,
    )
    return text, kb.main_menu_kb()


SCAN_PAGE_SIZE = 5
SCAN_CACHE_MAX_AGE = 600  # seconds a cached /scan stays pageable


def scan_page_view(deps, page: int):
    """Page through the most recent /scan sweep (best score first). With
    scan_safe_only on, only fully-✅ coins render and the header counts
    what was hidden — filtering happens at view time, so flipping the
    toggle re-slices the cached sweep without a re-scan."""
    cache = deps.scanner.last_scan or {"verdicts": [], "at": 0}
    verdicts = cache["verdicts"]
    hidden = 0
    if deps.store.settings.get("scan_safe_only"):
        safe = [v for v in verdicts if v.get("safety_ok")]
        hidden = len(verdicts) - len(safe)
        verdicts = safe
    total_pages = max(1, (len(verdicts) + SCAN_PAGE_SIZE - 1) // SCAN_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    chunk = verdicts[page * SCAN_PAGE_SIZE:(page + 1) * SCAN_PAGE_SIZE]
    text = fmt.scan_page_text(verdicts, page, SCAN_PAGE_SIZE,
                              evaluated=cache.get("evaluated"),
                              hidden_unsafe=hidden)
    markup = kb.scan_page_kb(chunk, page, total_pages,
                             start_rank=page * SCAN_PAGE_SIZE + 1)
    return text, markup


# ---------- trade execution shared by commands and buttons ----------

async def do_buy(deps, message, mint: str, sol_amount: float):
    try:
        result = await deps.engine.buy(mint, sol_amount, source="manual")
    except TradeError as exc:
        await message.reply_text(f"❌ {exc}")
        return
    await message.reply_text(
        fmt.buy_receipt(result), parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def do_sell(deps, message, mint: str, pct: float):
    try:
        result = await deps.engine.sell(mint, pct, reason="manual")
    except TradeError as exc:
        await message.reply_text(f"❌ {exc}")
        return
    await message.reply_text(
        fmt.sell_receipt(result), parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


# ---------- commands ----------

@authorized_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, markup = start_view(deps_of(context))
    await update.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)


@authorized_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(fmt.help_text(), parse_mode=ParseMode.HTML)


@authorized_only
async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    deps = deps_of(context)
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: /buy <mint> [SOL amount]\nOr just paste a token address."
        )
        return
    mint = extract_mint(args[0])
    if mint is None:
        await update.message.reply_text("That doesn't look like a valid Solana mint address.")
        return
    if len(args) >= 2:
        try:
            amount = float(args[1])
        except ValueError:
            await update.message.reply_text("Amount must be a number, e.g. /buy <mint> 0.5")
            return
        await do_buy(deps, update.message, mint, amount)
        return
    text, markup = await render_token_view(deps, mint)
    if text is None:
        await update.message.reply_text("No DexScreener pair found for that token.")
        return
    await update.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML,
                                    disable_web_page_preview=True)


@authorized_only
async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, markup = await positions_view(deps_of(context))
    await update.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML,
                                    disable_web_page_preview=True)


@authorized_only
async def cmd_trades(update: Update, context: ContextTypes.DEFAULT_TYPE):
    deps = deps_of(context)
    text = fmt.trades_text(deps.store.summary(), deps.store.data["closed_trades"],
                           deps.engine.dry_run, deps.store.signal_log)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@authorized_only
async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    deps = deps_of(context)
    text = fmt.settings_text(deps.store.settings, deps.engine.dry_run)
    await update.message.reply_text(text, reply_markup=kb.settings_kb(deps.store.settings),
                                    parse_mode=ParseMode.HTML)


@authorized_only
async def cmd_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, markup = await wallet_view(deps_of(context))
    await update.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML,
                                    disable_web_page_preview=True)


@authorized_only
async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    deps = deps_of(context)
    waiting = await update.message.reply_text(
        "🔎 Sweeping DexScreener + safety-checking… (can take up to a minute)"
    )
    try:
        await deps.scanner.scan_now()
    except Exception as exc:
        logger.exception("manual scan failed")
        await waiting.edit_text(f"Scan failed: {fmt.esc(str(exc))[:300]}")
        return
    text, markup = scan_page_view(deps, 0)
    await waiting.edit_text(text, reply_markup=markup, parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True)


@authorized_only
async def cmd_factors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    deps = deps_of(context)
    if deps.factors is None:
        await update.message.reply_text("Factor logging isn't enabled in this build.")
        return
    from ..analysis import compute_report
    report = compute_report(deps.factors.all_rows())
    await update.message.reply_text(f"<pre>{fmt.esc(report)}</pre>",
                                    parse_mode=ParseMode.HTML)


@authorized_only
async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    deps = deps_of(context)
    mint = extract_mint(" ".join(context.args or []))
    if mint is None:
        await update.message.reply_text("Usage: /mute <mint address>")
        return
    deps.store.mute(mint)
    deps.scanner.pool.pop(mint, None)
    await update.message.reply_text("🔇 Muted — this token will never be alerted again.")


@authorized_only
async def cmd_panic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    deps = deps_of(context)
    count = len(deps.store.positions)
    if count == 0:
        await update.message.reply_text("No open positions to sell.")
        return
    await update.message.reply_text(
        f"🚨 Market-sell ALL {count} open position(s) right now?",
        reply_markup=kb.confirm_kb("panic2", "Yes — sell everything"),
    )


# ---------- plain text: settings input, buy amounts, pasted mints ----------

def _parse_setting(key: str, raw: str):
    """Returns the validated stored value. Raises ValueError with a
    user-facing message."""
    raw = raw.strip().replace("%", "")
    if key == "buy_presets":
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if not 1 <= len(parts) <= 3:
            raise ValueError("Send 1 to 3 comma-separated SOL amounts, e.g. 0.1, 0.5, 1")
        presets = []
        for part in parts:
            value = float(part)
            if not 0.001 <= value <= 1000:
                raise ValueError("Each preset must be between 0.001 and 1000 SOL.")
            presets.append(value)
        return presets
    if key == "slippage_bps":
        value = float(raw)
        if not 0.1 <= value <= 50:
            raise ValueError("Slippage must be between 0.1% and 50%.")
        return int(round(value * 100))
    if key in ("take_profit_pct", "stop_loss_pct", "trailing_stop_pct"):
        value = float(raw)
        lo = 0 if key == "trailing_stop_pct" else 1
        hi = 1000 if key == "take_profit_pct" else 95
        if not lo <= value <= hi:
            raise ValueError(f"Value must be between {lo} and {hi} (percent).")
        return value
    if key == "tp_sell_pct":
        value = float(raw)
        if not 10 <= value <= 100:
            raise ValueError("TP sell portion must be 10-100% (100 = sell everything at TP).")
        return value
    if key == "runner_trailing_pct":
        value = float(raw)
        if not 5 <= value <= 95:
            raise ValueError("Runner trailing stop must be 5-95%.")
        return value
    if key == "autobuy_sol":
        value = float(raw)
        if not 0.001 <= value <= 1000:
            raise ValueError("Autobuy size must be between 0.001 and 1000 SOL.")
        return value
    if key in ("min_alert_score", "min_autobuy_score"):
        value = int(float(raw))
        if not 0 <= value <= 100:
            raise ValueError("Score must be 0-100.")
        return value
    if key == "max_positions":
        value = int(float(raw))
        if not 1 <= value <= 25:
            raise ValueError("Max positions must be 1-25.")
        return value
    if key in ("min_liquidity_usd", "min_volume_h1_usd"):
        value = float(raw.replace("$", "").replace(",", ""))
        if not 0 <= value <= 10_000_000:
            raise ValueError("Value must be between 0 and 10,000,000 (USD).")
        return value
    if key == "min_buys_h1":
        value = int(float(raw))
        if not 0 <= value <= 1000:
            raise ValueError("Min 1h buys must be 0-1000.")
        return value
    if key == "max_pair_age_hours":
        value = float(raw)
        if not 1 <= value <= 168:
            raise ValueError("Max pair age must be 1-168 hours.")
        return value
    if key == "min_pair_age_minutes":
        value = float(raw)
        if not 0 <= value <= 120:
            raise ValueError("Min pair age must be 0-120 minutes (0 = no delay).")
        return value
    if key == "autobuy_min_age_minutes":
        value = float(raw)
        if not 0 <= value <= 240:
            raise ValueError("Autobuy min age must be 0-240 minutes "
                             "(0 = same as the global min age).")
        return value
    if key == "max_entry_extension_pct":
        value = float(raw)
        if not 0 <= value <= 500:
            raise ValueError("Max entry extension must be 0-500% (0 = gate off).")
        return value
    raise ValueError("This setting can't be edited here.")


SETTING_PROMPTS = {
    "buy_presets": "Send new quick-buy amounts in SOL, comma-separated (current: {v})",
    "slippage_bps": "Send new slippage in percent (current: {v}%)",
    "take_profit_pct": "Send new take-profit percent (current: {v}%)",
    "stop_loss_pct": "Send new stop-loss percent (current: {v}%)",
    "tp_sell_pct": "How much of the position to sell when TP hits, in percent "
                   "— 100 sells everything, less leaves a runner (current: {v}%)",
    "runner_trailing_pct": "Trailing stop for the post-TP runner, percent off "
                           "the peak with a breakeven floor (current: {v}%)",
    "trailing_stop_pct": "Send new trailing-stop percent, 0 to disable (current: {v})",
    "autobuy_sol": "Send new autobuy size in SOL (current: {v})",
    "min_alert_score": "Send new minimum score for alerts, 0-100 (current: {v})",
    "min_autobuy_score": "Send new minimum score for autobuy, 0-100 (current: {v})",
    "max_positions": "Send new max open positions (current: {v})",
    "min_liquidity_usd": "Send new minimum pool liquidity in USD — lower shows "
                         "more (riskier) coins (current: ${v})",
    "min_volume_h1_usd": "Send new minimum 1-hour volume in USD (current: ${v})",
    "min_buys_h1": "Send new minimum buys in the last hour (current: {v})",
    "max_pair_age_hours": "Send new maximum pair age in hours — higher keeps "
                          "coins in view longer (current: {v}h)",
    "min_pair_age_minutes": "Send new minimum pair age in minutes before a coin "
                            "can screen/alert/autobuy — 0 disables the delay. "
                            "⚠️ The first minutes are peak rug territory "
                            "(current: {v}m)",
    "autobuy_min_age_minutes": "Send the minimum age in minutes before AUTOBUY "
                               "may act — alerts still fire from the global min "
                               "age, so you can flip young coins manually while "
                               "the bot waits. 0 = no extra wait (current: {v}m)",
    "max_entry_extension_pct": "Block alerts/autobuy when price is already this "
                               "% above its own 1h low — late entries buy tops. "
                               "0 disables the gate (current: {v}%)",
}


def _setting_current(settings: dict, key: str):
    if key == "slippage_bps":
        return f"{settings[key] / 100:g}"
    if key == "buy_presets":
        return ", ".join(f"{p:g}" for p in settings[key])
    value = settings[key]
    return f"{value:g}" if isinstance(value, float) else value


@authorized_only
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    deps = deps_of(context)
    text = (update.message.text or "").strip()
    awaiting = context.user_data.pop("awaiting", None)

    if awaiting:
        kind, key = awaiting
        if kind == "buy_amount":
            try:
                amount = float(text.replace("SOL", "").strip())
            except ValueError:
                context.user_data["awaiting"] = awaiting
                await update.message.reply_text("Send a number of SOL, e.g. 0.25")
                return
            await do_buy(deps, update.message, key, amount)
            return
        if kind == "setting":
            try:
                value = _parse_setting(key, text)
            except ValueError as exc:
                context.user_data["awaiting"] = awaiting
                await update.message.reply_text(f"❌ {exc}")
                return
            deps.store.set_setting(key, value)
            await update.message.reply_text(
                fmt.settings_text(deps.store.settings, deps.engine.dry_run),
                reply_markup=kb.settings_kb(deps.store.settings),
                parse_mode=ParseMode.HTML,
            )
            return

    mint = extract_mint(text)
    if mint:
        card, markup = await render_token_view(deps, mint)
        if card is None:
            await update.message.reply_text(
                "No DexScreener pair found for that address — token may be "
                "unlisted, delisted, or not a token mint."
            )
            return
        await update.message.reply_text(card, reply_markup=markup,
                                        parse_mode=ParseMode.HTML,
                                        disable_web_page_preview=True)


# ---------- inline buttons ----------

@authorized_only
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    deps = deps_of(context)
    data = query.data or ""
    try:
        await dispatch_callback(query, context, deps, data)
    except TradeError as exc:
        await query.answer(str(exc)[:190], show_alert=True)
    except Exception as exc:
        logger.exception("callback %r failed", data)
        await query.answer(f"Error: {str(exc)[:150]}", show_alert=True)


async def dispatch_callback(query, context, deps, data: str):
    parts = data.split(":")
    verb = parts[0]

    if verb == "m":
        await query.answer()
        text, markup = start_view(deps)
        await safe_edit(query, text, markup)

    elif verb == "pos":
        await query.answer()
        text, markup = await positions_view(deps)
        await safe_edit(query, text, markup)

    elif verb == "help":
        await query.answer()
        await safe_edit(query, fmt.help_text())

    elif verb == "scan":
        await query.answer("Sweeping + safety-checking… (up to a minute)")
        await deps.scanner.scan_now()
        text, markup = scan_page_view(deps, 0)
        await safe_edit(query, text, markup)

    elif verb == "scp":
        cache = deps.scanner.last_scan
        if cache is None or time.time() - cache["at"] > SCAN_CACHE_MAX_AGE:
            await query.answer("These results expired — tap 🔄 Re-scan.",
                               show_alert=True)
            return
        await query.answer()
        text, markup = scan_page_view(deps, int(parts[1]))
        await safe_edit(query, text, markup)

    elif verb == "r":
        text, markup = await render_token_view(deps, parts[1])
        if text is None:
            await query.answer("No pair found for this token anymore.", show_alert=True)
            return
        await query.answer()
        await safe_edit(query, text, markup)

    elif verb == "b":
        # Answer immediately: live swaps can outlive the ~15s answer window,
        # so all outcomes (including errors) are reported as messages.
        mint, amount = parts[1], float(parts[2])
        await query.answer(f"Buying {amount:g} SOL…")
        await do_buy(deps, query.message, mint, amount)

    elif verb == "bc":
        await query.answer()
        context.user_data["awaiting"] = ("buy_amount", parts[1])
        await query.message.reply_text("How much SOL? Send a number, e.g. 0.25")

    elif verb == "s":
        mint, pct = parts[1], float(parts[2])
        await query.answer(f"Selling {pct:g}%…")
        await do_sell(deps, query.message, mint, pct)
        with contextlib.suppress(Exception):  # cosmetic refresh of the old view
            text, markup = await positions_view(deps)
            await safe_edit(query, text, markup)

    elif verb == "mute":
        deps.store.mute(parts[1])
        deps.scanner.pool.pop(parts[1], None)
        await query.answer("Muted — no more alerts for this token.")

    elif verb == "st":
        await query.answer()
        await safe_edit(query, fmt.settings_text(deps.store.settings, deps.engine.dry_run),
                        kb.settings_kb(deps.store.settings))

    elif verb == "stt":
        key = parts[1]
        current = bool(deps.store.settings.get(key))
        deps.store.set_setting(key, not current)
        note = ""
        if key == "autobuy" and not current:
            note = " ⚠️ Autobuy is now ON — the bot will spend on its own."
        await query.answer(f"{key} → {'on' if not current else 'off'}.{note}",
                           show_alert=bool(note))
        await safe_edit(query, fmt.settings_text(deps.store.settings, deps.engine.dry_run),
                        kb.settings_kb(deps.store.settings))

    elif verb == "ste":
        key = parts[1]
        await query.answer()
        context.user_data["awaiting"] = ("setting", key)
        prompt = SETTING_PROMPTS[key].format(v=_setting_current(deps.store.settings, key))
        await query.message.reply_text(prompt)

    elif verb == "w":
        await query.answer()
        text, markup = await wallet_view(deps)
        await safe_edit(query, text, markup)

    elif verb == "we":
        if query.from_user is None or query.from_user.id != config.OWNER_ID:
            await query.answer("Only the primary owner (first id in "
                               "TELEGRAM_USER_IDS) can export the wallet key.",
                               show_alert=True)
            return
        if deps.keypair is None:
            await query.answer("No live wallet loaded in dry-run mode.", show_alert=True)
            return
        await query.answer()
        await query.message.reply_text(
            "🔑 Export the wallet's PRIVATE KEY into this chat?\n\n"
            "Anyone who sees it can drain the wallet irreversibly, and it will "
            "pass through Telegram's servers. The message self-deletes after "
            "60 seconds, but treat the key as exposed once sent.",
            reply_markup=kb.confirm_kb("wec", "I understand — show the key"),
        )

    elif verb == "wec":
        if query.from_user is None or query.from_user.id != config.OWNER_ID:
            await query.answer("Only the primary owner (first id in "
                               "TELEGRAM_USER_IDS) can export the wallet key.",
                               show_alert=True)
            return
        if deps.keypair is None:
            await query.answer("No live wallet loaded in dry-run mode.", show_alert=True)
            return
        await query.answer()
        secret = wallet_mod.export_base58(deps.keypair)
        message = await query.message.reply_text(
            f"<code>{secret}</code>\n\nImport into Phantom/Solflare now. "
            "Deleting in 60s.",
            parse_mode=ParseMode.HTML,
        )
        await safe_edit(query, "Key sent below — it self-deletes in 60s.")

        async def burn():
            await asyncio.sleep(60)
            try:
                await message.delete()
            except Exception:
                pass
        context.application.create_task(burn())

    elif verb == "panic":
        await query.answer()
        count = len(deps.store.positions)
        if count == 0:
            await safe_edit(query, "No open positions to sell.")
            return
        await query.message.reply_text(
            f"🚨 Market-sell ALL {count} open position(s) right now?",
            reply_markup=kb.confirm_kb("panic2", "Yes — sell everything"),
        )

    elif verb == "panic2":
        await query.answer("Selling everything…")
        results = await deps.engine.panic_sell_all()
        lines = ["🚨 <b>Panic sell finished</b>"]
        for result in results:
            if result["ok"]:
                trade = result["trade"]
                lines.append(f"🟢 {fmt.esc(result['symbol'])}: closed, "
                             f"{trade['pnl_sol']:+.4f} SOL")
            else:
                lines.append(f"🔴 {fmt.esc(result['symbol'])}: FAILED — "
                             f"{fmt.esc(result['error'])}")
        await safe_edit(query, "\n".join(lines))

    elif verb == "cancel":
        await query.answer("Cancelled.")
        await safe_edit(query, "Cancelled.")

    else:
        await query.answer()
