"""
Order execution and position management. One code path, two modes:

  DRY_RUN=True  -> fills are simulated against live DexScreener prices with
                   a flat SIM_FEE_PCT haircut per side, tracked against a
                   paper SOL balance. No wallet, nothing on-chain.
  DRY_RUN=False -> market swaps through Jupiter. Buys route SOL -> token;
                   sells route token -> SOL sized from the *actual on-chain
                   token balance*, so button sells stay correct even if a
                   fill estimate drifted.

Exits (TP / SL / trailing) are enforced by check_exits(), which the scanner
calls every tick — see clients/jupiter.py for why bot-monitored exits were
chosen over on-chain trigger orders, and what that trade-off means when the
bot isn't running.

Positions merge: buying a token you already hold averages the entry price
(weighted by token amount) and re-anchors TP/SL to the new average.
"""
import asyncio
import time

from .. import config, constants


class TradeError(Exception):
    """User-facing trade failure — message is shown in Telegram as-is."""


def price_in_sol(pair: dict, sol_price_usd: float) -> float:
    """Price of the base token denominated in SOL. For SOL-quoted pairs
    DexScreener's priceNative is exactly that; otherwise derive from USD."""
    quote_symbol = (pair.get("quoteToken") or {}).get("symbol", "").upper()
    if quote_symbol in ("SOL", "WSOL"):
        return float(pair.get("priceNative") or 0)
    price_usd = float(pair.get("priceUsd") or 0)
    return price_usd / sol_price_usd if sol_price_usd > 0 else 0.0


class TradingEngine:
    def __init__(self, store, dex, jupiter=None, rpc=None, keypair=None,
                 dry_run: bool = None, factors=None, price_history=None,
                 gecko=None):
        self.store = store
        self.dex = dex
        self.jupiter = jupiter
        self.rpc = rpc
        self.keypair = keypair
        self.dry_run = config.DRY_RUN if dry_run is None else dry_run
        self.factors = factors              # FactorLog, optional
        self.price_history = price_history  # PriceHistory, optional
        self.gecko = gecko                  # GeckoTerminal price failover, optional

    # ---------- balances ----------

    async def wallet_balance_sol(self) -> float:
        if self.dry_run:
            return self.store.stats["sim_balance_sol"]
        return await self.rpc.get_balance_sol(str(self.keypair.pubkey()))

    # ---------- buy ----------

    async def buy(self, mint: str, sol_amount: float, source: str = "manual",
                  pair: dict = None) -> dict:
        settings = self.store.settings
        if sol_amount <= 0:
            raise TradeError("Buy amount must be positive.")

        pair = pair or await self.dex.best_pair(constants.CHAIN_ID, mint)
        if pair is None:
            raise TradeError("No DexScreener pair found for that token.")
        price_usd = float(pair.get("priceUsd") or 0)
        sol_price = await self.dex.sol_price_usd()
        price_sol = price_in_sol(pair, sol_price)
        if price_usd <= 0 or price_sol <= 0:
            raise TradeError("Could not determine a live price for that token.")

        existing = self.store.get_position(mint)
        if existing is None and len(self.store.positions) >= settings["max_positions"]:
            raise TradeError(
                f"Max open positions reached ({settings['max_positions']}). "
                "Close something or raise the limit in /settings."
            )

        balance = await self.wallet_balance_sol()
        needed = sol_amount + (0 if self.dry_run else config.BALANCE_BUFFER_SOL)
        if balance < needed:
            raise TradeError(
                f"Insufficient SOL: balance {balance:.4f}, need {needed:.4f} "
                f"(amount{'' if self.dry_run else ' + fee buffer'})."
            )

        if self.dry_run:
            token_amount = sol_amount * (1 - config.SIM_FEE_PCT) / price_sol
            fill = {"token_amount": token_amount, "signature": None, "decimals": None,
                    "raw_received": None}
            self.store.sim_adjust_balance(-sol_amount)
        else:
            fill = await self._live_buy(mint, sol_amount)

        tp_pct, sl_pct, vol_factor = self._exit_pcts(mint, settings)
        position = self._apply_buy_fill(existing, pair, mint, sol_amount, price_usd,
                                        fill, source, tp_pct, sl_pct, vol_factor)
        self.store.put_position(position)
        return {
            "position": position,
            "price_usd": price_usd,
            "signature": fill["signature"],
            "merged": existing is not None,
        }

    async def _live_buy(self, mint: str, sol_amount: float) -> dict:
        lamports = int(sol_amount * constants.LAMPORTS_PER_SOL)
        raw_before, _, _ = await self.rpc.get_token_balance(str(self.keypair.pubkey()), mint)
        result = await self.jupiter.execute_swap(
            self.rpc, self.keypair, constants.SOL_MINT, mint, lamports,
            self.store.settings["slippage_bps"],
        )
        if not result["confirmed"]:
            raise TradeError(
                f"Buy transaction sent but not confirmed within 60s: {result['signature']}\n"
                "Check it on Solscan before retrying — it may still land."
            )
        # Reconcile what actually arrived (fills differ from quotes).
        raw_after, decimals = raw_before, None
        for attempt in range(3):
            raw_after, decimals, _ = await self.rpc.get_token_balance(
                str(self.keypair.pubkey()), mint
            )
            if raw_after > raw_before:
                break
            if attempt < 2:
                await asyncio.sleep(2)
        raw_received = max(raw_after - raw_before, 0)
        if raw_received == 0 or decimals is None:
            # fall back to the quote's estimate rather than recording nothing
            raw_received = int(result["quote"].get("outAmount") or 0)
            mint_info = await self.rpc.get_mint_info(mint)
            decimals = (mint_info or {}).get("decimals") or 9
        return {
            "token_amount": raw_received / (10 ** decimals),
            "raw_received": raw_received,
            "decimals": decimals,
            "signature": result["signature"],
        }

    def _exit_pcts(self, mint: str, settings: dict):
        """TP/SL percentages for a new fill — flat from settings, or scaled
        by the token's own recent volatility when vol_scaled_exits is on.
        Thin history falls back to the flat values (factor 1.0)."""
        tp_pct = settings["take_profit_pct"]
        sl_pct = settings["stop_loss_pct"]
        vol_factor = 1.0
        if settings.get("vol_scaled_exits") and self.price_history is not None:
            vol = self.price_history.volatility_pct(mint)
            if vol is not None and config.VOL_REFERENCE_PCT > 0:
                vol_factor = max(config.VOL_FACTOR_MIN,
                                 min(config.VOL_FACTOR_MAX,
                                     vol / config.VOL_REFERENCE_PCT))
                tp_pct *= vol_factor
                sl_pct = min(sl_pct * vol_factor, 95.0)
        return tp_pct, sl_pct, vol_factor

    def _factor_id(self, mint: str):
        if self.factors is None:
            return None
        try:
            return self.factors.latest_id_for_mint(mint)
        except Exception:
            return None

    def _apply_buy_fill(self, existing, pair, mint, sol_amount, price_usd, fill,
                        source, tp_pct, sl_pct, vol_factor) -> dict:
        base = pair.get("baseToken") or {}
        if existing:
            old_tokens = existing["token_amount"]
            new_tokens = fill["token_amount"]
            total = old_tokens + new_tokens
            avg_entry = (
                (existing["entry_price_usd"] * old_tokens + price_usd * new_tokens) / total
                if total > 0 else price_usd
            )
            position = existing
            position["token_amount"] = total
            position["entry_price_usd"] = avg_entry
            position["sol_spent"] += sol_amount
            if fill["raw_received"] is not None:
                position["token_amount_raw"] = (
                    (position.get("token_amount_raw") or 0) + fill["raw_received"]
                )
        else:
            avg_entry = price_usd
            position = {
                "mint": mint,
                "symbol": base.get("symbol") or "?",
                "name": base.get("name") or "?",
                "pair_address": pair.get("pairAddress"),
                "dex_id": pair.get("dexId"),
                "quote_symbol": (pair.get("quoteToken") or {}).get("symbol"),
                "entry_price_usd": price_usd,
                "sol_spent": sol_amount,
                "token_amount": fill["token_amount"],
                "token_amount_raw": fill["raw_received"],
                "token_decimals": fill["decimals"],
                "peak_price_usd": price_usd,
                "sol_received": 0.0,
                "partials": [],
                "opened_at": time.time(),
                "dry_run": self.dry_run,
                "source": source,
                "buy_signatures": [],
                "factor_log_id": self._factor_id(mint),
            }
        position["tp_price_usd"] = avg_entry * (1 + tp_pct / 100)
        position["sl_price_usd"] = avg_entry * (1 - sl_pct / 100)
        position["exit_vol_factor"] = vol_factor
        position["peak_price_usd"] = max(position.get("peak_price_usd") or 0, price_usd)
        if fill["signature"]:
            position["buy_signatures"].append(fill["signature"])
        return position

    # ---------- sell ----------

    async def sell(self, mint: str, pct: float, reason: str = "manual",
                   pair: dict = None) -> dict:
        position = self.store.get_position(mint)
        if position is None:
            raise TradeError("No open position for that token.")
        if bool(position.get("dry_run")) != self.dry_run:
            pos_mode = "DRY-RUN" if position.get("dry_run") else "LIVE"
            bot_mode = "DRY-RUN" if self.dry_run else "LIVE"
            raise TradeError(
                f"This position was opened in {pos_mode} mode but the bot is now "
                f"running {bot_mode}. Restart in the original mode to manage it "
                "(or clear it from state.json)."
            )
        pct = max(1.0, min(100.0, pct))

        pair = pair or await self.dex.best_pair(constants.CHAIN_ID, mint)
        if pair is None:
            raise TradeError("No DexScreener pair found — cannot price the sell.")
        price_usd = float(pair.get("priceUsd") or 0)
        sol_price = await self.dex.sol_price_usd()
        price_sol = price_in_sol(pair, sol_price)
        if price_sol <= 0:
            raise TradeError("Could not determine a live price for that token.")

        if self.dry_run:
            tokens_sold = position["token_amount"] * pct / 100
            sol_out = tokens_sold * price_sol * (1 - config.SIM_FEE_PCT)
            signature = None
            self.store.sim_adjust_balance(sol_out)
            position["token_amount"] -= tokens_sold
        else:
            raw_balance, decimals, _ = await self.rpc.get_token_balance(
                str(self.keypair.pubkey()), mint
            )
            if raw_balance <= 0:
                raise TradeError(
                    "On-chain token balance is zero — nothing to sell. "
                    "If you sold outside the bot, close the position with /positions."
                )
            raw_to_sell = raw_balance if pct >= 100 else int(raw_balance * pct / 100)
            result = await self.jupiter.execute_swap(
                self.rpc, self.keypair, mint, constants.SOL_MINT, raw_to_sell,
                self.store.settings["slippage_bps"],
            )
            if not result["confirmed"]:
                raise TradeError(
                    f"Sell transaction sent but not confirmed within 60s: {result['signature']}\n"
                    "Check it on Solscan before retrying — it may still land."
                )
            signature = result["signature"]
            # Estimate from the quote; on-chain truth is the next balance fetch.
            sol_out = int(result["quote"].get("outAmount") or 0) / constants.LAMPORTS_PER_SOL
            tokens_sold = raw_to_sell / (10 ** (decimals or position.get("token_decimals") or 9))
            position["token_amount"] = max(position["token_amount"] - tokens_sold, 0.0)
            position["token_amount_raw"] = max((raw_balance - raw_to_sell), 0)

        position["sol_received"] += sol_out
        position["partials"].append({
            "pct": pct, "tokens": tokens_sold, "sol_out": sol_out,
            "price_usd": price_usd, "reason": reason, "ts": time.time(),
            "signature": signature,
        })

        closed = pct >= 100 or position["token_amount"] <= 1e-12
        if closed:
            pnl_sol = position["sol_received"] - position["sol_spent"]
            pnl_pct = (pnl_sol / position["sol_spent"] * 100) if position["sol_spent"] else 0.0
            if self.factors is not None and position.get("factor_log_id") is not None:
                try:  # close the factor-analysis loop: entry factors -> real outcome
                    self.factors.update_trade_outcome(
                        position["factor_log_id"], reason, pnl_pct
                    )
                except Exception:
                    pass
            trade = {
                **{k: position[k] for k in (
                    "mint", "symbol", "name", "entry_price_usd", "sol_spent",
                    "sol_received", "opened_at", "dry_run", "source",
                )},
                "exit_price_usd": price_usd,
                "pnl_sol": pnl_sol,
                "pnl_pct": pnl_pct,
                "result": reason,
                "closed_at": time.time(),
            }
            self.store.close_position(mint, trade)
            return {"closed": True, "trade": trade, "sol_out": sol_out,
                    "price_usd": price_usd, "signature": signature}

        self.store.put_position(position)
        return {"closed": False, "position": position, "sol_out": sol_out,
                "price_usd": price_usd, "signature": signature, "pct": pct}

    # ---------- automated exits ----------

    async def check_exits(self) -> list:
        """Called every scanner tick. Updates peaks and fires exits:

        Before take-profit is hit, a position can close on stop_loss or the
        optional from-entry trailing_stop. When price reaches TP, only
        `tp_sell_pct` of the position is sold (100 = classic full close);
        the remainder becomes a "runner" protected by a trailing stop of
        `runner_trailing_pct` off the peak that never drops below the entry
        price. That floor means the runner exit can't TRIGGER below entry —
        though a sharp one-tick crash can still fill below it, since these
        are monitored stops, not resting orders.

        Returns display events; a failed exit sell produces an 'exit_error'
        event — in live mode that must reach the owners loudly."""
        mints = list(self.store.positions.keys())
        if not mints:
            return []
        try:
            pairs = await self.dex.pairs_for_tokens(constants.CHAIN_ID, mints)
        except Exception:
            # DexScreener down or rate-limited: exits are the one job that
            # must not pause, so fall back to GeckoTerminal prices wrapped
            # as minimal synthetic pairs. If that fails too, re-raise and
            # the scanner reports it.
            if self.gecko is None:
                raise
            prices = await self.gecko.simple_token_prices(mints)
            sol_price = await self.dex.sol_price_usd()
            pairs = [
                {
                    "baseToken": {"address": mint},
                    "quoteToken": {"symbol": "SOL"},
                    "priceUsd": str(price),
                    "priceNative": str(price / sol_price if sol_price > 0 else 0),
                    "liquidity": {"usd": 0},
                }
                for mint, price in prices.items() if price > 0
            ]
        best_by_mint = {}
        for pair in pairs:
            mint = (pair.get("baseToken") or {}).get("address")
            current = best_by_mint.get(mint)
            liq = (pair.get("liquidity") or {}).get("usd") or 0
            if current is None or liq > ((current.get("liquidity") or {}).get("usd") or 0):
                best_by_mint[mint] = pair

        settings = self.store.settings
        tp_sell_pct = settings.get("tp_sell_pct") or 100
        runner_trail = settings.get("runner_trailing_pct") or 20
        events = []
        dirty = False
        for mint in mints:
            position = self.store.get_position(mint)
            pair = best_by_mint.get(mint)
            if position is None or pair is None:
                continue
            if bool(position.get("dry_run")) != self.dry_run:
                continue  # opened under the other mode; sell() explains if the user tries
            price_usd = float(pair.get("priceUsd") or 0)
            if price_usd <= 0:
                continue

            if price_usd > (position.get("peak_price_usd") or 0):
                position["peak_price_usd"] = price_usd
                dirty = True

            reason, sell_pct = None, 100.0
            if position.get("tp_taken"):
                # Runner phase: trail off the peak, floored at breakeven.
                runner_stop = max(
                    position["entry_price_usd"],
                    position["peak_price_usd"] * (1 - runner_trail / 100),
                )
                if price_usd <= runner_stop:
                    reason = "runner_stop"
            else:
                stop_price = position["sl_price_usd"]
                stop_reason = "stop_loss"
                trailing_pct = settings.get("trailing_stop_pct") or 0
                if trailing_pct > 0:
                    trail_price = position["peak_price_usd"] * (1 - trailing_pct / 100)
                    if trail_price > stop_price:
                        stop_price, stop_reason = trail_price, "trailing_stop"

                if price_usd >= position["tp_price_usd"]:
                    reason = "take_profit"
                    sell_pct = min(tp_sell_pct, 100.0)
                elif price_usd <= stop_price:
                    reason = stop_reason

            if reason is None:
                continue
            try:
                result = await self.sell(mint, sell_pct, reason=reason, pair=pair)
                if result["closed"]:
                    events.append({"type": "exit", "reason": reason, **result["trade"]})
                else:
                    remaining = result["position"]
                    remaining["tp_taken"] = True
                    self.store.put_position(remaining)
                    events.append({
                        "type": "exit_partial", "reason": reason,
                        "symbol": remaining.get("symbol", "?"),
                        "dry_run": remaining.get("dry_run", self.dry_run),
                        "pct": sell_pct, "sol_out": result["sol_out"],
                        "price_usd": result["price_usd"],
                        "remaining_tokens": remaining["token_amount"],
                        "runner_trail_pct": runner_trail,
                        "signature": result.get("signature"),
                    })
                dirty = False  # the sell path persisted the store
            except TradeError as exc:
                events.append({
                    "type": "exit_error", "mint": mint,
                    "symbol": position.get("symbol", "?"),
                    "reason": reason, "error": str(exc),
                })
        if dirty:
            self.store.save()
        return events

    async def panic_sell_all(self) -> list:
        """Market-sell every open position immediately. Returns per-position
        results; errors are captured, not raised, so one stuck token can't
        block the rest of the exit."""
        results = []
        for mint in list(self.store.positions.keys()):
            symbol = (self.store.get_position(mint) or {}).get("symbol", "?")
            try:
                result = await self.sell(mint, 100, reason="panic")
                results.append({"mint": mint, "symbol": symbol, "ok": True,
                                "trade": result["trade"]})
            except Exception as exc:
                results.append({"mint": mint, "symbol": symbol, "ok": False,
                                "error": str(exc)})
        return results
