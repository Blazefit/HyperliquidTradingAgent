"""Manual trade setup executor for Hyperliquid.

Usage:
    python manual_setup.py

Then follow the interactive prompts, or pass everything via CLI:
    python manual_setup.py --asset BTC --direction long --entry 75507 \
        --sl 73997 --tp1 76615,25 --tp2 78384,35 --tp3 79644,20 \
        --trail-sl 78384 --size 0.01

The watcher loop monitors fills, adjusts stop-loss after targets hit,
and exits when the position is fully closed.
"""

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv
from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("manual-setup")

SZ_DECIMALS = {"BTC": 5, "ETH": 4, "SOL": 2, "HYPE": 1}


@dataclass
class Target:
    price: float
    pct: float


@dataclass
class Setup:
    asset: str
    direction: str
    entry_price: float
    stop_loss: float
    targets: list[Target] = field(default_factory=list)
    trail_sl_price: Optional[float] = None
    trail_sl_after_target: int = 2
    size: Optional[float] = None
    leverage: int = 3
    max_equity_pct: float = 0.10

    @property
    def remaining_pct(self) -> float:
        used = sum(t.pct for t in self.targets)
        return round(100 - used, 2)

    def calc_size(self, equity: float) -> float:
        if self.size is not None:
            return round(self.size, SZ_DECIMALS.get(self.asset, 4))
        raw = (equity * self.max_equity_pct * self.leverage) / self.entry_price
        return round(raw, SZ_DECIMALS.get(self.asset, 4))


class ManualExecutor:
    def __init__(self):
        private_key = os.getenv("HL_PRIVATE_KEY", "")
        wallet = os.getenv("HL_WALLET_ADDRESS", "")
        if not private_key or not wallet:
            log.error("HL_PRIVATE_KEY and HL_WALLET_ADDRESS must be set in .env")
            sys.exit(1)

        self.wallet = wallet
        self.info = Info(constants.MAINNET_API_URL, skip_ws=True)
        account = Account.from_key(private_key)
        self.exchange = Exchange(account, constants.MAINNET_API_URL, account_address=wallet)
        self.filled_targets: set[int] = set()
        self.sl_order_oid: Optional[str] = None

    def get_account_value(self) -> float:
        return float(
            self.info.user_state(self.wallet)["marginSummary"]["accountValue"]
        )

    def get_mid_price(self, asset: str) -> float:
        return float(self.info.all_mids().get(asset, 0))

    def get_position(self, asset: str) -> Optional[dict]:
        for p in self.info.user_state(self.wallet).get("assetPositions", []):
            pos = p["position"]
            if pos["coin"] == asset and float(pos["szi"]) != 0:
                return pos
        return None

    def get_open_orders(self, asset: str) -> list[dict]:
        for attempt in range(3):
            try:
                return [o for o in self.info.open_orders(self.wallet) if o.get("coin") == asset]
            except Exception as e:
                if attempt < 2:
                    time.sleep(1 * (attempt + 1))
                else:
                    log.warning("get_open_orders failed after 3 attempts: %s", e)
                    return []

    def cancel_order(self, asset: str, oid) -> bool:
        try:
            result = self.exchange.cancel(asset, oid)
            ok = result and result.get("status") == "ok"
            if ok:
                log.info("Cancelled order %s for %s", oid, asset)
            else:
                log.warning("Cancel result for %s: %s", oid, result)
            return ok
        except Exception as e:
            log.error("Cancel %s failed: %s", oid, e)
            return False

    def cancel_all_orders(self, asset: str):
        for o in self.get_open_orders(asset):
            self.cancel_order(asset, o.get("oid"))

    def place_entry_limit(self, setup: Setup, size: float) -> Optional[str]:
        is_buy = setup.direction == "long"
        log.info(
            "Placing %s limit entry: %s %.5f @ $%.2f",
            setup.direction.upper(), setup.asset, size, setup.entry_price,
        )
        result = self.exchange.order(
            setup.asset, is_buy, size, setup.entry_price,
            {"limit": {"tif": "Gtc"}},
        )
        log.info("Entry result: %s", result)
        if result and result.get("status") == "ok":
            statuses = result.get("response", {}).get("data", {}).get("statuses", [])
            if statuses:
                if "resting" in statuses[0]:
                    oid = str(statuses[0]["resting"]["oid"])
                    log.info("Entry order resting: %s", oid)
                    return oid
                if "filled" in statuses[0]:
                    fill = statuses[0]["filled"]
                    log.info(
                        "Entry FILLED immediately: %.5f @ $%.2f",
                        float(fill.get("totalSz", size)),
                        float(fill.get("avgPx", setup.entry_price)),
                    )
                    return str(fill.get("oid", ""))
        log.error("Entry order failed: %s", result)
        return None

    def place_stop_loss(self, setup: Setup, size: float) -> Optional[str]:
        is_buy = setup.direction == "short"
        order_type = {
            "trigger": {
                "triggerPx": str(setup.stop_loss),
                "isMarket": True,
                "tpsl": "sl",
            }
        }
        sz = round(size, SZ_DECIMALS.get(setup.asset, 4))
        log.info(
            "Placing stop-loss: %s %s %.5f @ trigger $%.2f",
            "BUY" if is_buy else "SELL", setup.asset, sz, setup.stop_loss,
        )
        result = self.exchange.order(
            setup.asset, is_buy, sz, setup.stop_loss, order_type, reduce_only=True,
        )
        log.info("SL result: %s", result)
        if result and result.get("status") == "ok":
            statuses = result.get("response", {}).get("data", {}).get("statuses", [])
            if statuses and "resting" in statuses[0]:
                oid = str(statuses[0]["resting"]["oid"])
                log.info("Stop-loss order resting: %s", oid)
                return oid
        log.error("Stop-loss failed: %s", result)
        return None

    def place_tp_limit(self, setup: Setup, price: float, size: float, label: str) -> Optional[str]:
        is_buy = setup.direction == "short"
        sz = round(size, SZ_DECIMALS.get(setup.asset, 4))
        log.info(
            "Placing %s TP limit: %s %.5f @ $%.2f",
            label, setup.asset, sz, price,
        )
        result = self.exchange.order(
            setup.asset, is_buy, sz, price,
            {"limit": {"tif": "Gtc"}},
            reduce_only=True,
        )
        log.info("TP %s result: %s", label, result)
        if result and result.get("status") == "ok":
            statuses = result.get("response", {}).get("data", {}).get("statuses", [])
            if statuses:
                if "resting" in statuses[0]:
                    oid = str(statuses[0]["resting"]["oid"])
                    log.info("TP %s resting: %s", label, oid)
                    return oid
                if "filled" in statuses[0]:
                    log.info("TP %s FILLED immediately!", label)
                    return str(statuses[0]["filled"].get("oid", ""))
        log.error("TP %s failed: %s", label, result)
        return None

    def move_stop_loss(self, setup: Setup, new_price: float, remaining_size: float) -> Optional[str]:
        if self.sl_order_oid:
            log.info("Cancelling old SL %s to move to $%.2f", self.sl_order_oid, new_price)
            self.cancel_order(setup.asset, self.sl_order_oid)
            time.sleep(0.3)

        is_short = setup.direction == "short"
        sz = round(remaining_size, SZ_DECIMALS.get(setup.asset, 4))
        order_requests = [
            {
                "coin": setup.asset,
                "is_buy": is_short,
                "sz": sz,
                "limit_px": float(new_price),
                "order_type": {
                    "trigger": {
                        "triggerPx": float(new_price),
                        "isMarket": True,
                        "tpsl": "sl",
                    }
                },
                "reduce_only": True,
            }
        ]
        log.info(
            "Moving SL to $%.2f for remaining %.5f %s",
            new_price, sz, setup.asset,
        )
        result = self.exchange.bulk_orders(order_requests)
        log.info("New SL result: %s", result)
        if result and result.get("status") == "ok":
            statuses = result.get("response", {}).get("data", {}).get("statuses", [])
            if statuses and "resting" in statuses[0]:
                oid = str(statuses[0]["resting"]["oid"])
                log.info("New SL resting: %s @ $%.2f", oid, new_price)
                self.sl_order_oid = oid
                return oid
        log.error("Failed to move SL: %s", result)
        return None

    def check_fills(self, setup: Setup) -> list[dict]:
        open_orders = self.get_open_orders(setup.asset)
        oids = {o.get("oid") for o in open_orders}
        fills = []
        for i, tp in enumerate(setup.targets):
            if i in self.filled_targets:
                continue
            tp_oid = getattr(tp, "_oid", None)
            if tp_oid and int(tp_oid) not in oids:
                log.info("TP%d @ $%.2f FILLED!", i + 1, tp.price)
                self.filled_targets.add(i)
                fills.append({"target_idx": i, "price": tp.price, "pct": tp.pct})
        return fills

    def get_remaining_position_size(self, setup: Setup, total_size: float) -> float:
        pos = self.get_position(setup.asset)
        if pos is None:
            return 0.0
        return abs(float(pos["szi"]))

    def _find_existing_entry(self, setup: Setup):
        open_orders = self.get_open_orders(setup.asset)
        for o in open_orders:
            is_buy = o.get("side") == "B"
            matches_dir = (is_buy and setup.direction == "long") or (not is_buy and setup.direction == "short")
            px = float(o.get("limitPx", 0))
            if matches_dir and abs(px - setup.entry_price) < 1:
                log.info("Found existing entry order oid=%s @ $%.2f", o["oid"], px)
                return str(o["oid"]), float(o.get("sz", 0))
        return None, None

    def _count_resting_tps(self, setup: Setup):
        open_orders = self.get_open_orders(setup.asset)
        resting = {}
        for o in open_orders:
            is_buy = o.get("side") == "B"
            is_reduce = o.get("reduceOnly", False)
            if not is_reduce:
                continue
            px = float(o.get("limitPx", 0))
            for i, tp in enumerate(setup.targets):
                if i not in resting and abs(px - tp.price) < 1:
                    resting[i] = str(o["oid"])
                    tp._oid = str(o["oid"])
        return resting

    def _find_resting_sl(self, setup: Setup):
        open_orders = self.get_open_orders(setup.asset)
        for o in open_orders:
            trigger = o.get("trigger", {})
            if trigger and trigger.get("isMarket") and trigger.get("tpsl") == "sl":
                log.info("Found existing SL oid=%s @ $%s", o["oid"], trigger.get("triggerPx"))
                return str(o["oid"])
        return None

    def execute(self, setup: Setup):
        equity = self.get_account_value()
        size = setup.calc_size(equity)
        remaining_pct = setup.remaining_pct

        print("\n" + "=" * 60)
        print("  TRADE SETUP")
        print("=" * 60)
        print(f"  Asset:      {setup.asset}")
        print(f"  Direction:  {setup.direction.upper()}")
        print(f"  Entry:      ${setup.entry_price:,.2f}")
        print(f"  Stop Loss:  ${setup.stop_loss:,.2f}")
        for i, tp in enumerate(setup.targets):
            print(f"  TP{i+1}:       ${tp.price:,.2f}  ({tp.pct}%)")
        print(f"  Remaining:  {remaining_pct}% (runner)")
        if setup.trail_sl_price:
            print(f"  Trail SL:   ${setup.trail_sl_price:,.2f} (after TP{setup.trail_sl_after_target})")
        print(f"  Size:       {size:.5f} ({setup.leverage}x, {setup.max_equity_pct*100:.0f}% equity)")
        print(f"  Equity:     ${equity:,.2f}")
        print("=" * 60)

        self.exchange.update_leverage(setup.leverage, setup.asset)
        time.sleep(0.3)

        pos = self.get_position(setup.asset)
        existing_entry_oid, existing_sz = self._find_existing_entry(setup)
        resting_tps = self._count_resting_tps(setup)
        resting_sl = self._find_resting_sl(setup)

        if pos is not None:
            pos_side = "long" if float(pos["szi"]) > 0 else "short"
            pos_sz = abs(float(pos["szi"]))
            if pos_side == setup.direction and pos_sz > 0:
                log.info(
                    "RESUMING: Existing %s position %.5f %s @ $%.2f",
                    pos_side.upper(), pos_sz, setup.asset, float(pos["entryPx"]),
                )
                size = pos_sz
                if existing_entry_oid:
                    log.info("Cancelling filled entry order %s", existing_entry_oid)
                    self.cancel_order(setup.asset, existing_entry_oid)
                    time.sleep(0.2)
                self._place_bracket(setup, size, remaining_pct)
                return

        if existing_entry_oid:
            existing_sz = float(existing_sz) if existing_sz else size
            log.info(
                "RESUMING: Watching existing entry %s (%.5f @ $%.2f)",
                existing_entry_oid, existing_sz, setup.entry_price,
            )
            self._watch_entry_fill(setup, existing_entry_oid, existing_sz, remaining_pct)
            return

        confirm = input("\n  Execute this setup? (yes/no): ").strip().lower()
        if confirm != "yes":
            log.info("Aborted.")
            return

        entry_oid = self.place_entry_limit(setup, size)
        if not entry_oid:
            log.error("Failed to place entry order. Aborting.")
            return
        self._watch_entry_fill(setup, entry_oid, size, remaining_pct)

    def _watch_entry_fill(self, setup: Setup, entry_oid: str, size: float, remaining_pct: float):
        print(f"\n  Entry watching (oid: {entry_oid})")
        print("  Waiting for entry fill...\n")
        entry_oid_int = int(entry_oid)

        while True:
            try:
                pos = self.get_position(setup.asset)
                if pos is not None:
                    entry_px = float(pos["entryPx"])
                    pos_sz = abs(float(pos["szi"]))
                    side = "long" if float(pos["szi"]) > 0 else "short"
                    if side == setup.direction and pos_sz > 0:
                        log.info(
                            "FILLED: %s %.5f %s @ $%.2f",
                            side.upper(), pos_sz, setup.asset, entry_px,
                        )
                        size = pos_sz
                        break
                open_oids = {o.get("oid") for o in self.get_open_orders(setup.asset)}
                if entry_oid_int not in open_oids:
                    log.info("Entry order no longer open - checking position...")
                    if pos:
                        size = abs(float(pos["szi"]))
                        break
                    log.error("Entry order gone but no position. May have failed.")
                    return
            except Exception as e:
                log.warning("Poll error (retrying in 10s): %s", e)
                time.sleep(10)
                continue
            time.sleep(3)

        self._place_bracket(setup, size, remaining_pct)

    def _place_bracket(self, setup: Setup, size: float, remaining_pct: float):
        resting_tps = self._count_resting_tps(setup)
        resting_sl = self._find_resting_sl(setup)

        if len(resting_tps) == len(setup.targets) and resting_sl:
            log.info("Bracket already fully placed (%d TPs + SL). Resuming watcher.", len(resting_tps))
            self.filled_targets = set()
            self.sl_order_oid = resting_sl
        else:
            log.info("Placing bracket orders via bulk_orders...")
            self.cancel_all_orders(setup.asset)
            time.sleep(0.3)

            is_short = setup.direction == "short"
            order_requests = [
                {
                    "coin": setup.asset,
                    "is_buy": is_short,
                    "sz": round(size, SZ_DECIMALS.get(setup.asset, 4)),
                    "limit_px": float(setup.stop_loss),
                    "order_type": {
                        "trigger": {
                            "triggerPx": float(setup.stop_loss),
                            "isMarket": True,
                            "tpsl": "sl",
                        }
                    },
                    "reduce_only": True,
                }
            ]

            for tp in setup.targets:
                tp_size = round(size * (tp.pct / 100), SZ_DECIMALS.get(setup.asset, 4))
                order_requests.append({
                    "coin": setup.asset,
                    "is_buy": is_short,
                    "sz": tp_size,
                    "limit_px": float(tp.price),
                    "order_type": {"limit": {"tif": "Gtc"}},
                    "reduce_only": True,
                })

            result = self.exchange.bulk_orders(order_requests)
            log.info("bulk_orders result: %s", result)

            if result and result.get("status") == "ok":
                statuses = result.get("response", {}).get("data", {}).get("statuses", [])
                if statuses and "resting" in statuses[0]:
                    self.sl_order_oid = str(statuses[0]["resting"]["oid"])
                    log.info("SL order resting: %s", self.sl_order_oid)
                for i, tp in enumerate(setup.targets):
                    if i + 1 < len(statuses) and "resting" in statuses[i + 1]:
                        tp._oid = str(statuses[i + 1]["resting"]["oid"])
                        log.info("TP%d resting: %s", i + 1, tp._oid)
            else:
                log.error("bulk_orders failed: %s", result)
                return

        print("\n  Bracket active. Watching fills... (Ctrl+C to exit watcher)\n")
        self._watch_loop(setup, size, remaining_pct)

    def _watch_loop(self, setup: Setup, size: float, remaining_pct: float):
        trail_sl_applied = False
        remaining_sz = size
        last_health_check = time.time()
        last_health_log = 0.0

        try:
            while True:
                try:
                    pos = self.get_position(setup.asset)
                    if pos is None:
                        log.info("Position closed (no position found). All done.")
                        self.cancel_all_orders(setup.asset)
                        break

                    remaining_sz = abs(float(pos["szi"]))
                    fills = self.check_fills(setup)

                    for fill in fills:
                        idx = fill["target_idx"]
                        log.info(
                            "TP%d filled @ $%.2f (%.0f%%). Remaining: %.5f %s",
                            idx + 1, fill["price"], fill["pct"], remaining_sz, setup.asset,
                        )

                        if self.sl_order_oid and remaining_sz > 0:
                            self.cancel_order(setup.asset, self.sl_order_oid)
                            time.sleep(0.2)
                            self.sl_order_oid = self.place_stop_loss_with_size(
                                setup, remaining_sz
                            )

                        if (
                            not trail_sl_applied
                            and setup.trail_sl_price
                            and idx + 1 >= setup.trail_sl_after_target
                            and len(self.filled_targets) >= setup.trail_sl_after_target
                        ):
                            log.info(
                                "TP%d threshold reached. Moving SL to $%.2f",
                                setup.trail_sl_after_target, setup.trail_sl_price,
                            )
                            self.move_stop_loss(setup, setup.trail_sl_price, remaining_sz)
                            trail_sl_applied = True

                    all_tps_filled = len(self.filled_targets) == len(setup.targets)
                    if all_tps_filled and remaining_pct <= 0:
                        log.info("All targets filled and no runner. Done.")
                        self.cancel_all_orders(setup.asset)
                        break

                    now = time.time()
                    if now - last_health_check >= 3600:
                        last_health_check = now
                        trail_sl_applied = self._health_check(
                            setup, remaining_sz, remaining_pct, trail_sl_applied,
                        )

                    if now - last_health_log >= 1800:
                        last_health_log = now
                        mid = self.get_mid_price(setup.asset)
                        filled_str = ",".join(f"TP{i+1}" for i in sorted(self.filled_targets)) or "none"
                        log.info(
                            "STATUS: pos=%.5f %s @ mid=$%,.2f | filled=[%s] | SL=%s | equity=$%,.2f",
                            remaining_sz, setup.asset, mid, filled_str,
                            self.sl_order_oid or "none", self.get_account_value(),
                        )

                except Exception as e:
                    log.warning("Watch error (retrying in 10s): %s", e)
                    time.sleep(10)
                    continue

                time.sleep(3)

        except KeyboardInterrupt:
            print("\n")
            log.info("Watcher stopped. Orders remain active on Hyperliquid.")
            log.info("Position: %.5f %s", remaining_sz if pos else 0, setup.asset)
            log.info("SL order: %s", self.sl_order_oid or "none")

    def _health_check(self, setup: Setup, remaining_sz: float, remaining_pct: float, trail_sl_applied: bool) -> bool:
        log.info("=" * 40 + " HOURLY HEALTH CHECK " + "=" * 40)
        issues = []

        pos = self.get_position(setup.asset)
        if pos is None:
            log.info("[HC] No position found - trade may have closed.")
            return trail_sl_applied

        pos_sz = abs(float(pos["szi"]))
        mid = self.get_mid_price(setup.asset)

        open_orders = self.get_open_orders(setup.asset)
        sl_found = False
        tp_oids_found = set()

        for o in open_orders:
            trigger = o.get("trigger", {})
            if trigger and trigger.get("isMarket") and trigger.get("tpsl") == "sl":
                sl_found = True
                sl_px = float(trigger.get("triggerPx", 0))
                expected_sl = setup.stop_loss
                if trail_sl_applied and setup.trail_sl_price:
                    expected_sl = setup.trail_sl_price
                if abs(sl_px - expected_sl) > 1:
                    issues.append(f"SL at ${sl_px:,.2f} but expected ${expected_sl:,.2f}")
                sl_sz = float(o.get("sz", 0))
                if abs(sl_sz - pos_sz) > 0.0001:
                    issues.append(f"SL size {sl_sz:.5f} != position {pos_sz:.5f}")

            is_reduce = o.get("reduceOnly", False)
            if is_reduce and not trigger:
                px = float(o.get("limitPx", 0))
                for i, tp in enumerate(setup.targets):
                    if i not in self.filled_targets and i not in tp_oids_found:
                        if abs(px - tp.price) < 1:
                            tp_oids_found.add(i)
                            tp._oid = str(o["oid"])

        if not sl_found and remaining_sz > 0:
            issues.append("No stop-loss order found!")
            log.warning("[HC] Replacing missing stop-loss...")
            self.sl_order_oid = self.place_stop_loss_with_size(setup, pos_sz)

        if self.sl_order_oid and not sl_found:
            if self.sl_order_oid:
                self.cancel_order(setup.asset, self.sl_order_oid)
            self.sl_order_oid = self.place_stop_loss_with_size(setup, pos_sz)

        unfilled_tps = set()
        for i, tp in enumerate(setup.targets):
            if i not in self.filled_targets and i not in tp_oids_found:
                unfilled_tps.add(i)

        if unfilled_tps and pos_sz > 0:
            issues.append(f"Missing TP orders: {[f'TP{i+1}' for i in unfilled_tps]}")
            log.warning("[HC] Replacing missing TP orders...")
            for i in sorted(unfilled_tps):
                tp = setup.targets[i]
                tp_sz = round(pos_sz * (tp.pct / 100), SZ_DECIMALS.get(setup.asset, 4))
                tp_oid = self.place_tp_limit(setup, tp.price, tp_sz, f"TP{i+1}")
                if tp_oid:
                    tp._oid = tp_oid
                time.sleep(0.2)

        tp_sizes_ok = True
        for o in open_orders:
            is_reduce = o.get("reduceOnly", False)
            trigger = o.get("trigger", {})
            if is_reduce and not trigger:
                o_sz = float(o.get("sz", 0))
                px = float(o.get("limitPx", 0))
                for i, tp in enumerate(setup.targets):
                    if abs(px - tp.price) < 1 and i not in self.filled_targets:
                        expected_sz = round(pos_sz * (tp.pct / 100), SZ_DECIMALS.get(setup.asset, 4))
                        if abs(o_sz - expected_sz) > 0.0001:
                            issues.append(f"TP{i+1} size {o_sz:.5f} != expected {expected_sz:.5f}")

        if not issues:
            log.info(
                "[HC] ALL GOOD: pos=%.5f %s @ mid=$%,.2f | SL active | %d TPs active | equity=$%,.2f",
                pos_sz, setup.asset, mid, len(setup.targets) - len(self.filled_targets),
                self.get_account_value(),
            )
        else:
            for issue in issues:
                log.warning("[HC] ISSUE: %s", issue)
            log.info("[HC] Self-healing applied for %d issue(s).", len(issues))

        log.info("=" * 100)
        return trail_sl_applied

    def place_stop_loss_with_size(self, setup: Setup, size: float) -> Optional[str]:
        is_short = setup.direction == "short"
        current_sl = setup.stop_loss
        if hasattr(self, '_active_trail_sl') and self._active_trail_sl:
            current_sl = self._active_trail_sl
        sz = round(size, SZ_DECIMALS.get(setup.asset, 4))
        if sz <= 0:
            return None
        order_requests = [
            {
                "coin": setup.asset,
                "is_buy": is_short,
                "sz": sz,
                "limit_px": float(current_sl),
                "order_type": {
                    "trigger": {
                        "triggerPx": float(current_sl),
                        "isMarket": True,
                        "tpsl": "sl",
                    }
                },
                "reduce_only": True,
            }
        ]
        log.info("Replacing SL for remaining %.5f @ $%.2f", sz, current_sl)
        result = self.exchange.bulk_orders(order_requests)
        if result and result.get("status") == "ok":
            statuses = result.get("response", {}).get("data", {}).get("statuses", [])
            if statuses and "resting" in statuses[0]:
                oid = str(statuses[0]["resting"]["oid"])
                log.info("New SL resting: %s", oid)
                return oid
        log.error("Replace SL failed: %s", result)
        return None


def parse_args():
    parser = argparse.ArgumentParser(description="Manual trade setup executor")
    parser.add_argument("--asset", default="BTC")
    parser.add_argument("--direction", choices=["long", "short"], default="long")
    parser.add_argument("--entry", type=float, help="Entry limit price")
    parser.add_argument("--sl", type=float, help="Stop-loss price")
    parser.add_argument("--tp1", help="price,pct (e.g. 76615,25)")
    parser.add_argument("--tp2", help="price,pct")
    parser.add_argument("--tp3", help="price,pct")
    parser.add_argument("--tp4", help="price,pct")
    parser.add_argument("--trail-sl", type=float, help="Move SL to this price after trail-after-target")
    parser.add_argument("--trail-after", type=int, default=2, help="Move trail SL after this many TPs fill")
    parser.add_argument("--size", type=float, help="Override position size")
    parser.add_argument("--leverage", type=int, default=3)
    parser.add_argument("--equity-pct", type=float, default=0.10, help="Max equity % per trade")
    return parser.parse_args()


def interactive_setup() -> Setup:
    print("\n  MANUAL TRADE SETUP")
    print("  " + "-" * 40)
    asset = input("  Asset (BTC): ").strip().upper() or "BTC"
    direction = input("  Direction (long/short): ").strip().lower() or "long"
    entry = float(input("  Entry price: ").strip())
    sl = float(input("  Stop-loss price: ").strip())

    targets = []
    print("\n  Targets (enter empty to finish):")
    for i in range(1, 5):
        raw = input(f"  TP{i} price: ").strip()
        if not raw:
            break
        price = float(raw)
        pct = float(input(f"  TP{i} % to close: ").strip())
        targets.append(Target(price=price, pct=pct))

    remaining = round(100 - sum(t.pct for t in targets), 2)
    print(f"\n  Remaining runner: {remaining}%")

    trail_sl = None
    trail_after = 2
    trail_raw = input("  Trail SL price (after TPs fill, empty=none): ").strip()
    if trail_raw:
        trail_sl = float(trail_raw)
        trail_after = int(input(f"  Move SL after how many TPs (default 2): ").strip() or "2")

    size_raw = input("  Position size (empty=auto): ").strip()
    size = float(size_raw) if size_raw else None
    leverage = int(input("  Leverage (default 3): ").strip() or "3")

    return Setup(
        asset=asset,
        direction=direction,
        entry_price=entry,
        stop_loss=sl,
        targets=targets,
        trail_sl_price=trail_sl,
        trail_sl_after_target=trail_after,
        size=size,
        leverage=leverage,
    )


def main():
    args = parse_args()

    if args.entry and args.sl:
        targets = []
        for tp_arg in [args.tp1, args.tp2, args.tp3, args.tp4]:
            if not tp_arg:
                continue
            parts = tp_arg.split(",")
            targets.append(Target(price=float(parts[0]), pct=float(parts[1])))

        setup = Setup(
            asset=args.asset,
            direction=args.direction,
            entry_price=args.entry,
            stop_loss=args.sl,
            targets=targets,
            trail_sl_price=args.trail_sl,
            trail_sl_after_target=args.trail_after,
            size=args.size,
            leverage=args.leverage,
            max_equity_pct=args.equity_pct,
        )
    else:
        setup = interactive_setup()

    executor = ManualExecutor()
    executor.execute(setup)


if __name__ == "__main__":
    main()
