"""
Execution & Trade Lifecycle Engine
Manages active holdings, daily trailing ratchets, disaster stop losses, and trend exits.
"""
from datetime import datetime
import pandas as pd
import pytz

from portfolio import (
    can_take_trade, open_position, close_position,
    add_discrepancy, add_order
)
from config import HARD_STOP_LOSS_PCT


IST = pytz.timezone("Asia/Kolkata")


def manage_active_holdings(portfolio: dict, market_data_map: dict, today_str: str) -> list:
    """
    Evaluates every currently open holding against today's bar:
    1. Skip if position was entered today (cannot evaluate against entry day's past low)
    2. Ratchet Trailing Stop: lock profits 4.5% below highest high
    3. Check Stop Loss / Trailing Stop: Low <= current_sl
    4. Check Trend Reversal: EMA9 < EMA21
    5. Update Unrealized P&L and Days Held
    Returns: list of closed trade dictionaries
    """
    closed_this_run = []
    active_tickers = list(portfolio.get("positions", {}).keys())

    for ticker in active_tickers:
        pos = portfolio["positions"].get(ticker)
        if not pos:
            continue

        df = market_data_map.get(ticker)
        if df is None or len(df) == 0:
            add_discrepancy(portfolio, "DATA_MISSING", ticker, f"No bar data available for today {today_str}")
            continue

        bar_date = df.index[-1].strftime("%Y-%m-%d")

        # ── CRITICAL SAFEGUARD: Do not evaluate exit on the entry day's bar ──
        # Entry happens at Close of entry_date; Day T's low happened before entry!
        if pos.get("entry_date") == bar_date or pos.get("entry_date") == today_str:
            continue

        latest_bar = df.iloc[-1]
        hi = float(latest_bar["High"])
        lo = float(latest_bar["Low"])
        cl = float(latest_bar["Close"])
        ema9 = float(latest_bar["EMA9"]) if "EMA9" in latest_bar else cl
        ema21 = float(latest_bar["EMA21"]) if "EMA21" in latest_bar else cl

        # Update position tracking
        pos["days_held"] = pos.get("days_held", 0) + 1
        pos["highest_high"] = max(pos.get("highest_high", pos["entry_price"]), hi)
        pos["last_price"] = round(cl, 2)
        pos["unrealized_pnl_pct"] = round((cl - pos["entry_price"]) / pos["entry_price"] * 100.0, 2)

        entry = pos["entry_price"]

        # ── TRAILING STOP RATCHET: Lock in profits at 4.5% below highest high ──
        ratchet_sl = round(pos["highest_high"] * (1.0 - HARD_STOP_LOSS_PCT / 100.0), 2)
        if ratchet_sl > pos["current_sl"]:
            pos["current_sl"] = ratchet_sl

        sl = pos["current_sl"]

        # ── EXIT CONDITION 1: Hard Stop Loss or Trailing Stop Hit ──
        if lo <= sl:
            actual_exit = sl
            # Check for gap-down opening below SL
            op = float(latest_bar["Open"])
            if op < sl:
                actual_exit = op  # Slipped beyond SL due to opening gap
                add_discrepancy(portfolio, "GAP_DOWN_SLIP", ticker,
                                f"Opened at {op:.2f} below SL of {sl:.2f}. Filled at Open.")

            exit_reason = "TRAILING_STOP" if sl > entry else "HARD_STOP_LOSS"
            trade = close_position(portfolio, ticker, exit_price=actual_exit,
                                   exit_date=today_str, exit_reason=exit_reason)
            if trade:
                closed_this_run.append(trade)
            continue

        # ── EXIT CONDITION 2: Trend Exhaustion (EMA 9 crosses below EMA 21) ──
        if ema9 < ema21 and pos["days_held"] >= 1:
            trade = close_position(portfolio, ticker, exit_price=cl,
                                   exit_date=today_str, exit_reason="TREND_EXIT (EMA9 < EMA21)")
            if trade:
                closed_this_run.append(trade)
            continue

    return closed_this_run



def execute_new_signals(portfolio: dict, qualified_signals: list, today_str: str) -> list:
    """
    Executes newly qualified breakout signals if slots and capital allow.
    Returns: list of opened position dictionaries.
    """
    opened_this_run = []

    # Rank signals by relative volume spike (highest institutional demand first)
    ranked_signals = sorted(qualified_signals, key=lambda x: x.get("vol_ratio", 1.0), reverse=True)

    for sig in ranked_signals:
        ticker = sig["ticker"]

        # Already holding?
        if ticker in portfolio.get("positions", {}):
            continue

        if can_take_trade(portfolio):
            entry_price = sig["close"]
            pos = open_position(portfolio, ticker, entry_price=entry_price, entry_date=today_str)
            if pos:
                opened_this_run.append(pos)
        else:
            add_discrepancy(portfolio, "SKIPPED_CAPITAL_EXHAUSTED", ticker,
                            f"Qualified breakout on {ticker} skipped: all slots full or insufficient cash.",
                            severity="INFO")

    return opened_this_run
