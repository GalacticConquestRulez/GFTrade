"""
GFTrade entry point. Run: python main.py

Starts the Telegram bot and the background scanner in whatever mode the
DRY_RUN environment variable specifies (default: dry run). Check /start in
Telegram to confirm the mode before assuming anything about real funds.
"""
import asyncio
import contextlib
import logging
import signal
import sys

import httpx

from gftrade import config
from gftrade import wallet as wallet_mod
from gftrade.clients.dexscreener import DexScreener
from gftrade.clients.geckoterminal import GeckoTerminal
from gftrade.clients.jupiter import Jupiter
from gftrade.clients.rugcheck import RugCheck
from gftrade.discovery.safety import SafetyChecker
from gftrade.discovery.trend import PriceHistory
from gftrade.factors import FactorLog
from gftrade.scanner import Scanner
from gftrade.solana_rpc import SolanaRpc
from gftrade.store import Store
from gftrade.tg.bot import BOT_COMMANDS, Deps, build_application, publish_events
from gftrade.trading.engine import TradingEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("gftrade")


def validate_config() -> None:
    problems = []
    if not config.TELEGRAM_BOT_TOKEN:
        problems.append("TELEGRAM_BOT_TOKEN is not set (get one from @BotFather).")
    if not config.AUTHORIZED_IDS:
        problems.append("TELEGRAM_USER_IDS is not set (comma-separated numeric "
                        "Telegram user ids; message @userinfobot for yours).")
    if problems:
        for problem in problems:
            print(f"config error: {problem}", file=sys.stderr)
        print("Copy .env.example to .env and fill it in.", file=sys.stderr)
        raise SystemExit(1)


def load_keypair_or_exit():
    if config.DRY_RUN:
        return None
    try:
        return wallet_mod.load_wallet()
    except FileNotFoundError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        print("DRY_RUN=false requires a funded wallet. Generate one with "
              "`python -m gftrade.wallet` — and read the README's risk section first.",
              file=sys.stderr)
        raise SystemExit(1)


async def run() -> None:
    keypair = load_keypair_or_exit()

    http = httpx.AsyncClient(follow_redirects=True)
    rpc = SolanaRpc(config.SOLANA_RPC_URL, http)
    dex = DexScreener(http)
    jupiter = Jupiter(http)
    store = Store()
    rugcheck = RugCheck(http) if config.LP_CHECK_ENABLED else None
    safety = SafetyChecker(rpc, rugcheck)
    factors = FactorLog()
    prices = PriceHistory()
    gecko = GeckoTerminal(http)
    engine = TradingEngine(store, dex, jupiter, rpc, keypair,
                           factors=factors, price_history=prices, gecko=gecko)
    scanner = Scanner(store, dex, engine, safety, gecko=gecko,
                      factors=factors, prices=prices)
    deps = Deps(store=store, dex=dex, engine=engine, scanner=scanner,
                safety=safety, rpc=rpc, keypair=keypair, factors=factors)
    app = build_application(deps)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    mode = "🧪 DRY RUN" if config.DRY_RUN else "🔴 LIVE"
    logger.info("starting in %s mode (scan every %ss)",
                mode, config.SCAN_INTERVAL_SECONDS)

    async with app:
        await app.bot.set_my_commands(BOT_COMMANDS)
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        extra = ("" if config.DRY_RUN else
                 "\n⚠️ Live mode: this bot can spend real SOL from its wallet.")
        for user_id in config.AUTHORIZED_IDS:
            try:
                await app.bot.send_message(
                    user_id,
                    f"🚀 GFTrade online — {mode}. Send /start for the menu.{extra}",
                )
            except Exception:
                logger.warning("could not message user %s on startup — they need "
                               "to open a chat with the bot and press Start once",
                               user_id)

        scan_task = asyncio.create_task(
            scanner.run_forever(lambda events: publish_events(app, deps, events))
        )
        try:
            await stop_event.wait()
        finally:
            scan_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scan_task
            await app.updater.stop()
            await app.stop()
    await http.aclose()
    factors.close()
    logger.info("shut down cleanly")


def main() -> None:
    validate_config()
    asyncio.run(run())


if __name__ == "__main__":
    main()
