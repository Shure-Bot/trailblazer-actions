"""
TrailBlazer v0.4 — Portfolio Edition (GitHub Actions)
Trades MULTIPLE coins simultaneously, each with its own capital allocation.
Unallocated capital stays as a safe USDT reserve.
"""

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

import ccxt

STATE_FILE = Path("state.json")
LOG_FILE = Path("logs.jsonl")

PORTFOLIO = [
    {"pair": "BTC/USDT", "allocation_pct": 10},
    {"pair": "ETH/USDT", "allocation_pct": 15},
    {"pair": "TRX/USDT", "allocation_pct": 12},
    {"pair": "SOL/USDT", "allocation_pct": 10},
    {"pair": "XRP/USDT", "allocation_pct": 8},
]

TOTAL_CAPITAL = float(os.environ.get("TB_CAPITAL", "100"))
EXCHANGE = os.environ.get("TB_EXCHANGE", "kucoin")
TRAIL_BUY_PCT = float(os.environ.get("TB_BUY_PCT", "1.5"))
TRAIL_SL_PCT = float(os.environ.get("TB_SL_PCT", "2.0"))
CHECK_INTERVAL_MINUTES = 15


def default_pair_state(allocation_pct):
    return {
        "allocation_pct": allocation_pct,
        "balance": round(TOTAL_CAPITAL * allocation_pct / 100, 4),
        "phase": "watching",
        "low": None,
        "high": None,
        "entry": None,
        "wins": 0,
        "losses": 0,
        "trades": [],
        "last_action": "No trades yet.",
    }


def default_state():
    allocated_pct = sum(p["allocation_pct"] for p in PORTFOLIO)
    reserve_pct = max(0, 100 - allocated_pct)
    return {
        "total_capital": TOTAL_CAPITAL,
        "reserve_balance": round(TOTAL_CAPITAL * reserve_pct / 100, 4),
        "reserve_pct": reserve_pct,
        "portfolio": {p["pair"]: default_pair_state(p["allocation_pct"]) for p in PORTFOLIO},
        "last_checked": None,
        "next_check_due": None,
        "check_count": 0,
        "last_error": None,
    }


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            state = json.load(f)
        for p in PORTFOLIO:
            if p["pair"] not in state["portfolio"]:
                state["portfolio"][p["pair"]] = default_pair_state(p["allocation_pct"])
        return state
    return default_state()


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def log_entry(entry):
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def fetch_price(exchange_obj, pair):
    ticker = exchange_obj.fetch_ticker(pair)
    return ticker["last"]


def process_pair(pair_state, pair_name, price, now_str, log):
    log["pair"] = pair_name
    log["price"] = price
    log["phase"] = pair_state["phase"]
    balance = pair_state["balance"]

    if pair_state["phase"] == "watching":
        pair_state["low"] = price if pair_state["low"] is None else min(pair_state["low"], price)
        trigger = pair_state["low"] * (1 + TRAIL_BUY_PCT / 100)
        log["lowest_seen"] = pair_state["low"]
        log["buy_trigger"] = trigger

        if price >= trigger:
            pair_state["phase"] = "in_position"
            pair_state["entry"] = price
            pair_state["high"] = price
            msg = f"BUY {pair_name} at {price:.4f} with ${balance:.2f} after bounce from low of {pair_state['low']:.4f}"
            pair_state["last_action"] = msg
            log["decision"] = "BUY"
            log["reason"] = msg
        else:
            log["decision"] = "NO_TRADE"
            log["reason"] = "Price has not reached trailing buy trigger yet."

    else:
        pair_state["high"] = max(pair_state["high"], price)
        stop = pair_state["high"] * (1 - TRAIL_SL_PCT / 100)
        log["peak_seen"] = pair_state["high"]
        log["stop_trigger"] = stop

        if price <= stop:
            pnl_pct = ((price - pair_state["entry"]) / pair_state["entry"]) * 100
            profit = (balance * pnl_pct) / 100
            is_win = pnl_pct > 0

            pair_state["balance"] = round(max(0, balance + profit), 4)
            if is_win:
                pair_state["wins"] += 1
            else:
                pair_state["losses"] += 1

            trade = {
                "pair": pair_name,
                "type": "WIN" if is_win else "LOSS",
                "entry": round(pair_state["entry"], 4),
                "exit": round(price, 4),
                "pnl_pct": round(pnl_pct, 3),
                "pnl_dollar": round(profit, 4),
                "time": now_str,
            }
            pair_state["trades"].append(trade)
            pair_state["trades"] = pair_state["trades"][-30:]

            msg = f"{'SOLD (WIN)' if is_win else 'SOLD (LOSS)'} {pair_name} at {price:.4f} ({pnl_pct:+.2f}%, {profit:+.3f} USD)"
            pair_state["last_action"] = msg
            log["decision"] = "SELL"
            log["reason"] = msg
            log["trade"] = trade

            pair_state["phase"] = "watching"
            pair_state["low"] = price
            pair_state["high"] = None
            pair_state["entry"] = None
        else:
            log["decision"] = "HOLD"
            log["reason"] = "In position, stop-loss not yet triggered."

    return pair_state


def main():
    now = datetime.now(timezone.utc)
    now_str = now.isoformat()

    state = load_state()
    state["check_count"] = state.get("check_count", 0) + 1
    state["last_checked"] = now_str
    next_check = now + timedelta(minutes=CHECK_INTERVAL_MINUTES)
    state["next_check_due"] = next_check.isoformat()

    exchange_class = getattr(ccxt, EXCHANGE)
    exchange_obj = exchange_class({"enableRateLimit": True})

    any_error = None

    for p in PORTFOLIO:
        pair_name = p["pair"]
        log = {
            "time": now_str,
            "check_number": state["check_count"],
            "exchange": EXCHANGE,
        }
        try:
            price = fetch_price(exchange_obj, pair_name)
        except Exception as e:
            log["pair"] = pair_name
            log["decision"] = "ERROR"
            log["error"] = str(e)
            log_entry(log)
            any_error = str(e)[:200]
            continue

        pair_state = state["portfolio"][pair_name]
        updated = process_pair(pair_state, pair_name, price, now_str, log)
        state["portfolio"][pair_name] = updated
        log_entry(log)

    state["last_error"] = any_error
    save_state(state)

    total_now = state["reserve_balance"] + sum(s["balance"] for s in state["portfolio"].values())
    print(f"[{now_str}] Check #{state['check_count']} complete. Portfolio total: ${total_now:.2f}")


if __name__ == "__main__":
    main()
