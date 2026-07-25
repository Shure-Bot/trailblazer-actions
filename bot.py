"""
TrailBlazer v0.3 — Scheduled Check Engine (GitHub Actions edition)
Runs ONCE per invocation: reads previous state, fetches current price,
applies trailing buy/sell logic, logs the decision, saves state for next run.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import ccxt

STATE_FILE = Path("state.json")
LOG_FILE = Path("logs.jsonl")

RISK_PROFILES = {"ultra_safe": 5, "safe": 10, "moderate": 20}

CONFIG = {
    "exchange": os.environ.get("TB_EXCHANGE", "kucoin"),
    "pair": os.environ.get("TB_PAIR", "BTC/USDT"),
    "mode": os.environ.get("TB_MODE", "demo"),
    "capital": float(os.environ.get("TB_CAPITAL", "20")),
    "risk_key": os.environ.get("TB_RISK", "safe"),
    "trail_buy_pct": float(os.environ.get("TB_BUY_PCT", "1.5")),
    "trail_sl_pct": float(os.environ.get("TB_SL_PCT", "2.0")),
    "check_interval_minutes": 15,
}


def default_state():
    return {
        "phase": "watching",
        "low": None,
        "high": None,
        "entry": None,
        "balance": CONFIG["capital"],
        "wins": 0,
        "losses": 0,
        "trades": [],
        "last_action": "No trades yet.",
        "last_checked": None,
        "next_check_due": None,
        "check_count": 0,
    }


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return default_state()


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def log_entry(entry):
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def trade_size(balance, risk_key):
    pct = RISK_PROFILES.get(risk_key, 10)
    return (balance * pct) / 100


def fetch_price(exchange_name, pair):
    exchange_class = getattr(ccxt, exchange_name)
    exchange = exchange_class({"enableRateLimit": True})
    ticker = exchange.fetch_ticker(pair)
    exchange.close()
    return ticker["last"]


def main():
    now = datetime.now(timezone.utc)
    now_str = now.isoformat()

    state = load_state()
    state["check_count"] = state.get("check_count", 0) + 1
    state["last_checked"] = now_str

    from datetime import timedelta
    next_check = now + timedelta(minutes=CONFIG["check_interval_minutes"])
    state["next_check_due"] = next_check.isoformat()

    log = {
        "time": now_str,
        "check_number": state["check_count"],
        "pair": CONFIG["pair"],
        "exchange": CONFIG["exchange"],
    }

    try:
        price = fetch_price(CONFIG["exchange"], CONFIG["pair"])
        state["last_error"] = None
    except Exception as e:
        log["decision"] = "ERROR"
        log["error"] = str(e)
        state["last_error"] = str(e)[:200]
        log_entry(log)
        save_state(state)
        print(f"[{now_str}] ERROR fetching price: {e}")
        return

    log["price"] = price
    log["phase"] = state["phase"]

    buy_pct = CONFIG["trail_buy_pct"]
    sl_pct = CONFIG["trail_sl_pct"]

    if state["phase"] == "watching":
        state["low"] = price if state["low"] is None else min(state["low"], price)
        trigger = state["low"] * (1 + buy_pct / 100)
        log["lowest_seen"] = state["low"]
        log["buy_trigger"] = trigger

        if price >= trigger:
            state["phase"] = "in_position"
            state["entry"] = price
            state["high"] = price
            size = trade_size(state["balance"], CONFIG["risk_key"])
            msg = f"BUY at {price:.2f} with ${size:.2f} after bounce from low of {state['low']:.2f}"
            state["last_action"] = msg
            log["decision"] = "BUY"
            log["reason"] = msg
        else:
            log["decision"] = "NO_TRADE"
            log["reason"] = "Price has not reached trailing buy trigger yet."

    else:
        state["high"] = max(state["high"], price)
        stop = state["high"] * (1 - sl_pct / 100)
        log["peak_seen"] = state["high"]
        log["stop_trigger"] = stop

        if price <= stop:
            size = trade_size(state["balance"], CONFIG["risk_key"])
            pnl_pct = ((price - state["entry"]) / state["entry"]) * 100
            profit = (size * pnl_pct) / 100
            is_win = pnl_pct > 0

            state["balance"] = max(0, state["balance"] + profit)
            if is_win:
                state["wins"] += 1
            else:
                state["losses"] += 1

            trade = {
                "pair": CONFIG["pair"],
                "type": "WIN" if is_win else "LOSS",
                "entry": round(state["entry"], 4),
                "exit": round(price, 4),
                "pnl_pct": round(pnl_pct, 3),
                "pnl_dollar": round(profit, 4),
                "time": now_str,
            }
            state["trades"].append(trade)
            state["trades"] = state["trades"][-50:]

            msg = f"{'SOLD (WIN)' if is_win else 'SOLD (LOSS)'} at {price:.2f} ({pnl_pct:+.2f}%, {profit:+.3f} USD)"
            state["last_action"] = msg
            log["decision"] = "SELL"
            log["reason"] = msg
            log["trade"] = trade

            state["phase"] = "watching"
            state["low"] = price
            state["high"] = None
            state["entry"] = None
        else:
            log["decision"] = "HOLD"
            log["reason"] = "In position, stop-loss not yet triggered."

    save_state(state)
    log_entry(log)
    print(f"[{now_str}] Check #{state['check_count']}: {log['decision']} — price {price:.2f}")


if __name__ == "__main__":
    main()
