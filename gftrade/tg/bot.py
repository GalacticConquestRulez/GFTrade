"""
Application wiring: dependency container, handler registration, and the
publisher that turns scanner events into Telegram messages.
"""
import logging
import time
from dataclasses import dataclass

from telegram import BotCommand
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from .. import config
from . import formatting as fmt
from . import handlers
from . import keyboards as kb

logger = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand("start", "Main menu + status"),
    BotCommand("scan", "Find good coins right now"),
    BotCommand("buy", "Token card or instant buy: /buy <mint> [SOL]"),
    BotCommand("positions", "Open positions with sell buttons"),
    BotCommand("trades", "Performance and recent trades"),
    BotCommand("settings", "Runtime settings"),
    BotCommand("wallet", "Address and balance"),
    BotCommand("panic", "Market-sell everything"),
    BotCommand("help", "How this bot works"),
]


@dataclass
class Deps:
    store: object
    dex: object
    engine: object
    scanner: object
    safety: object
    rpc: object = None
    keypair: object = None


def build_application(deps: Deps) -> Application:
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    app.bot_data["deps"] = deps

    app.add_handler(CommandHandler("start", handlers.cmd_start))
    app.add_handler(CommandHandler("help", handlers.cmd_help))
    app.add_handler(CommandHandler("buy", handlers.cmd_buy))
    app.add_handler(CommandHandler(["positions", "sell"], handlers.cmd_positions))
    app.add_handler(CommandHandler("trades", handlers.cmd_trades))
    app.add_handler(CommandHandler("settings", handlers.cmd_settings))
    app.add_handler(CommandHandler("wallet", handlers.cmd_wallet))
    app.add_handler(CommandHandler("scan", handlers.cmd_scan))
    app.add_handler(CommandHandler("mute", handlers.cmd_mute))
    app.add_handler(CommandHandler("panic", handlers.cmd_panic))
    app.add_handler(CallbackQueryHandler(handlers.on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.on_text))
    app.add_error_handler(on_error)
    return app


async def on_error(update, context) -> None:
    logger.exception("update handling failed", exc_info=context.error)


# scan_error dedupe: the same broken feed shouldn't page the owner every 90s
_last_scan_error_at = {}
SCAN_ERROR_COOLDOWN = 1800


def render_event(deps: Deps, event: dict):
    """event dict -> (text, reply_markup) or (None, None) to skip."""
    kind = event.get("type")
    if kind == "signal":
        verdict = event["verdict"]
        markup = kb.token_kb(
            verdict["mint"], deps.store.settings["buy_presets"],
            verdict["pair"].get("url"), include_mute=True,
        )
        return fmt.signal_card(verdict), markup
    if kind == "autobuy":
        return fmt.autobuy_card(event["verdict"], event["result"]), None
    if kind == "autobuy_error":
        verdict = event["verdict"]
        symbol = (verdict["pair"].get("baseToken") or {}).get("symbol", "?")
        return (
            f"⚠️ Autobuy failed for {fmt.esc(symbol)}: {fmt.esc(event['error'])}",
            None,
        )
    if kind == "exit":
        return fmt.exit_event_text(event), None
    if kind == "exit_error":
        return fmt.exit_error_text(event), None
    if kind == "scan_error":
        where = event.get("where", "?")
        last = _last_scan_error_at.get(where, 0)
        if time.time() - last < SCAN_ERROR_COOLDOWN:
            return None, None
        _last_scan_error_at[where] = time.time()
        return (
            f"⚠️ Scanner problem in {fmt.esc(where)} (muting repeats for 30m):\n"
            f"{fmt.esc(event['error'])[:400]}",
            None,
        )
    logger.warning("unknown scanner event type: %r", kind)
    return None, None


async def publish_events(app: Application, deps: Deps, events: list) -> None:
    """Broadcast each event to every authorized user. Per-recipient failures
    (someone hasn't opened a chat with the bot yet, blocked it, ...) are
    logged and never block the other recipients."""
    for event in events:
        try:
            text, markup = render_event(deps, event)
        except Exception:
            logger.exception("failed to render event %r", event.get("type"))
            continue
        if not text:
            continue
        for user_id in config.AUTHORIZED_IDS:
            try:
                await app.bot.send_message(
                    chat_id=user_id, text=text, reply_markup=markup,
                    parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                )
            except Exception:
                logger.exception("failed to deliver %r to user %s",
                                 event.get("type"), user_id)
