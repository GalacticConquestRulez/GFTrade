"""
Inline keyboards + the callback-data grammar.

Telegram caps callback_data at 64 bytes; a Solana mint is up to 44 chars,
so every verb is kept to 1-4 chars:

  m                  main menu            w        wallet view
  pos                positions view       we/wec   export ask / confirmed
  scan               run a sweep now      st       settings view
  b:<mint>:<amt>     buy amt SOL          stt:<k>  toggle boolean setting
  bc:<mint>          buy custom amount    ste:<k>  prompt to edit setting
  s:<mint>:<pct>     sell pct of holding  panic/panic2  ask / confirmed
  r:<mint>           refresh token card   mute:<mint>   silence a token
  help               help card            cancel   dismiss a confirm
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .. import constants


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Scan now", callback_data="scan"),
         InlineKeyboardButton("📂 Positions", callback_data="pos")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="st"),
         InlineKeyboardButton("👛 Wallet", callback_data="w")],
        [InlineKeyboardButton("❓ Help", callback_data="help")],
    ])


def token_kb(mint: str, presets: list, pair_url: str = None,
             include_mute: bool = False) -> InlineKeyboardMarkup:
    buy_row = [
        InlineKeyboardButton(f"Buy {amount:g} SOL", callback_data=f"b:{mint}:{amount:g}")
        for amount in presets[:3]
    ]
    action_row = [
        InlineKeyboardButton("Buy X…", callback_data=f"bc:{mint}"),
        InlineKeyboardButton("🔄 Refresh", callback_data=f"r:{mint}"),
    ]
    rows = [buy_row, action_row]
    link_row = []
    if pair_url:
        link_row.append(InlineKeyboardButton("📈 Chart", url=pair_url))
    link_row.append(InlineKeyboardButton(
        "🔍 Solscan", url=constants.SOLSCAN_TOKEN_URL.format(mint=mint)
    ))
    if include_mute:
        link_row.append(InlineKeyboardButton("🔇 Mute", callback_data=f"mute:{mint}"))
    rows.append(link_row)
    return InlineKeyboardMarkup(rows)


def positions_kb(positions: dict) -> InlineKeyboardMarkup:
    rows = []
    for mint, pos in positions.items():
        symbol = (pos.get("symbol") or "?")[:10]
        rows.append([
            InlineKeyboardButton(f"{symbol} 25%", callback_data=f"s:{mint}:25"),
            InlineKeyboardButton("50%", callback_data=f"s:{mint}:50"),
            InlineKeyboardButton("100%", callback_data=f"s:{mint}:100"),
        ])
    bottom = [InlineKeyboardButton("🔄 Refresh", callback_data="pos")]
    if positions:
        bottom.append(InlineKeyboardButton("🚨 Panic sell all", callback_data="panic"))
    rows.append(bottom)
    rows.append([InlineKeyboardButton("« Menu", callback_data="m")])
    return InlineKeyboardMarkup(rows)


def scan_page_kb(page_verdicts: list, page: int, total_pages: int,
                 start_rank: int) -> InlineKeyboardMarkup:
    rows = []
    for offset, verdict in enumerate(page_verdicts):
        base = verdict["pair"].get("baseToken") or {}
        badge = "✅" if verdict.get("safety_ok") else "⚠️"
        label = (f"{badge} #{start_rank + offset} "
                 f"{(base.get('symbol') or '?')[:12]} · {verdict['score']}/100")
        rows.append([InlineKeyboardButton(label, callback_data=f"r:{base.get('address')}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"scp:{page - 1}"))
    if total_pages > 1:
        nav.append(InlineKeyboardButton(f"· {page + 1}/{total_pages} ·",
                                        callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"scp:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🔄 Re-scan", callback_data="scan"),
                 InlineKeyboardButton("« Menu", callback_data="m")])
    return InlineKeyboardMarkup(rows)


def settings_kb(settings: dict) -> InlineKeyboardMarkup:
    def onoff(flag):
        return "🟢" if flag else "⚪️"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{onoff(settings['scanner_on'])} Scanner",
                              callback_data="stt:scanner_on"),
         InlineKeyboardButton(f"{onoff(settings['autobuy'])} Autobuy",
                              callback_data="stt:autobuy"),
         InlineKeyboardButton(f"{onoff(settings['security_strict'])} Strict safety",
                              callback_data="stt:security_strict")],
        [InlineKeyboardButton("✏️ Buy presets", callback_data="ste:buy_presets"),
         InlineKeyboardButton("✏️ Slippage %", callback_data="ste:slippage_bps")],
        [InlineKeyboardButton("✏️ Take profit %", callback_data="ste:take_profit_pct"),
         InlineKeyboardButton("✏️ Stop loss %", callback_data="ste:stop_loss_pct")],
        [InlineKeyboardButton("✏️ TP sell portion %", callback_data="ste:tp_sell_pct"),
         InlineKeyboardButton("✏️ Runner trail %", callback_data="ste:runner_trailing_pct")],
        [InlineKeyboardButton("✏️ Trailing stop %", callback_data="ste:trailing_stop_pct"),
         InlineKeyboardButton("✏️ Max positions", callback_data="ste:max_positions")],
        [InlineKeyboardButton("✏️ Autobuy SOL", callback_data="ste:autobuy_sol"),
         InlineKeyboardButton("✏️ Alert score", callback_data="ste:min_alert_score"),
         InlineKeyboardButton("✏️ Autobuy score", callback_data="ste:min_autobuy_score")],
        [InlineKeyboardButton("« Menu", callback_data="m")],
    ])


def wallet_kb(dry_run: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("🔄 Refresh", callback_data="w")]]
    if not dry_run:
        rows[0].append(InlineKeyboardButton("🔑 Export key", callback_data="we"))
    rows.append([InlineKeyboardButton("« Menu", callback_data="m")])
    return InlineKeyboardMarkup(rows)


def confirm_kb(yes_callback: str, yes_label: str = "Yes, do it") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(yes_label, callback_data=yes_callback)],
        [InlineKeyboardButton("Cancel", callback_data="cancel")],
    ])
