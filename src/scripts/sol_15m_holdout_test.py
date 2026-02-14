"""
SOL 15m Final Holdout Test
--------------------------
Trains HMM on the first 80% (4000 bars) of SOL_USD_15m data,
then evaluates the frozen best walk-forward config on the unseen 20% (1000 bars).

Best config from walk-forward:
  prob=0.55, persist=1, rsi_entry=55, rsi_exit=45,
  stop=2.0, tp=2.5, vol_thr=1.0, atr_max=0.90
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# Re-use core HMM & metric helpers from hmm_regime_test
from src.scripts.hmm_regime_test import (
    GaussianHMM2,
    Metrics,
    annualize_return,
    annualized_sharpe,
    estimate_trade_stats,
    max_drawdown_pct,
)

# ── frozen config ──────────────────────────────────────────
CFG = dict(
    prob=0.55,
    persist=1,
    rsi_entry=55,
    rsi_exit=45,
    stop_pct=2.0,
    tp_pct=2.5,
    vol_thr=1.0,
    atr_max_pct=0.90,
    sma_fast=30,
    sma_slow=50,
    rsi_period=14,
    atr_period=14,
    fee=0.0008,
    slip=0.0003,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_PATH = PROJECT_ROOT / "src" / "data" / "ohlcv" / "hyperliquid" / "SOL_USD_15m_hyperliquid.csv"
RESULTS_DIR = PROJECT_ROOT / "src" / "data" / "execution_results"


# ── RSI helper ─────────────────────────────────────────────
def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100.0 - 100.0 / (1.0 + rs)


def run_holdout() -> dict:
    # ── load ───────────────────────────────────────────────
    df = pd.read_csv(DATA_PATH)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values("timestamp").reset_index(drop=True)
    total_bars = len(df)

    # ── split ──────────────────────────────────────────────
    split_idx = int(total_bars * 0.80)
    train_df = df.iloc[:split_idx].reset_index(drop=True)
    hold_df = df.iloc[split_idx:].reset_index(drop=True)
    print(f"Total bars: {total_bars}  |  Train: {len(train_df)}  |  Holdout: {len(hold_df)}")
    print(f"Train period: {train_df['timestamp'].iloc[0]} → {train_df['timestamp'].iloc[-1]}")
    print(f"Holdout period: {hold_df['timestamp'].iloc[0]} → {hold_df['timestamp'].iloc[-1]}")

    # ── HMM fit on train only ──────────────────────────────
    train_close = train_df["close"].astype(float)
    train_logret = np.log(train_close / train_close.shift(1)).fillna(0.0)

    hmm = GaussianHMM2(max_iter=120, tol=1e-6)
    gamma_train, _ = hmm.fit(train_logret.values)
    bull_state = int(np.argmax(hmm.mu))
    bear_state = 1 - bull_state
    print(f"\nHMM fitted on train  |  bull μ={hmm.mu[bull_state]:.6f}  bear μ={hmm.mu[bear_state]:.6f}")

    # ── apply trained HMM to holdout (forward inference only) ──
    hold_close = hold_df["close"].astype(float)
    hold_logret = np.log(hold_close / hold_close.shift(1)).fillna(0.0)

    # Run forward-backward using the *trained* parameters on holdout data
    gamma_hold, _, _, _ = hmm._forward_backward(hold_logret.values)
    state_hold = np.argmax(gamma_hold, axis=1)
    prob_bull_hold = gamma_hold[:, bull_state]

    # ── indicators on holdout ──────────────────────────────
    # To compute SMA/RSI with proper look-back, prepend last
    # CFG['sma_slow'] bars from training set
    lookback = max(CFG["sma_slow"], CFG["rsi_period"], CFG["atr_period"]) + 10
    extended_close = pd.concat(
        [train_df["close"].iloc[-lookback:], hold_close], ignore_index=True
    ).astype(float)
    extended_high = pd.concat(
        [train_df["high"].iloc[-lookback:], hold_df["high"]], ignore_index=True
    ).astype(float)
    extended_low = pd.concat(
        [train_df["low"].iloc[-lookback:], hold_df["low"]], ignore_index=True
    ).astype(float)
    extended_vol = pd.concat(
        [train_df["volume"].iloc[-lookback:], hold_df["volume"]], ignore_index=True
    ).astype(float)

    sma_fast = extended_close.rolling(CFG["sma_fast"]).mean()
    sma_slow = extended_close.rolling(CFG["sma_slow"]).mean()
    rsi_vals = rsi(extended_close, CFG["rsi_period"])
    tr = pd.concat([
        extended_high - extended_low,
        (extended_high - extended_close.shift(1)).abs(),
        (extended_low - extended_close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(CFG["atr_period"]).mean()
    atr_pct = (atr / extended_close) * 100.0
    vol_ratio = extended_vol / extended_vol.rolling(20).mean()

    # Trim back to holdout length
    offset = lookback
    sma_fast_h = sma_fast.iloc[offset:].reset_index(drop=True)
    sma_slow_h = sma_slow.iloc[offset:].reset_index(drop=True)
    rsi_h = rsi_vals.iloc[offset:].reset_index(drop=True)
    atr_pct_h = atr_pct.iloc[offset:].reset_index(drop=True)
    vol_ratio_h = vol_ratio.iloc[offset:].reset_index(drop=True)
    close_h = hold_close.reset_index(drop=True)

    # ── generate positions ─────────────────────────────────
    n = len(close_h)
    pos = pd.Series(np.zeros(n), dtype=float)

    for i in range(1, n):
        # regime gate
        if prob_bull_hold[i] < CFG["prob"]:
            pos.iloc[i] = 0.0
            continue
        # trend gate
        if pd.isna(sma_fast_h.iloc[i]) or pd.isna(sma_slow_h.iloc[i]):
            pos.iloc[i] = 0.0
            continue
        if sma_fast_h.iloc[i] <= sma_slow_h.iloc[i]:
            pos.iloc[i] = 0.0
            continue
        # RSI gate
        rsi_val = rsi_h.iloc[i]
        if pd.isna(rsi_val):
            pos.iloc[i] = 0.0
            continue
        if rsi_val > CFG["rsi_entry"]:
            pos.iloc[i] = 0.0
            continue
        if rsi_val < CFG["rsi_exit"]:
            pos.iloc[i] = 0.0
            continue
        # volatility gate
        if not pd.isna(vol_ratio_h.iloc[i]) and vol_ratio_h.iloc[i] > CFG["vol_thr"]:
            pos.iloc[i] = 0.0
            continue
        # ATR gate
        if not pd.isna(atr_pct_h.iloc[i]) and atr_pct_h.iloc[i] > CFG["atr_max_pct"]:
            pos.iloc[i] = 0.0
            continue

        pos.iloc[i] = 1.0

    # ── apply stop-loss / take-profit ──────────────────────
    entry_price = None
    for i in range(1, n):
        if pos.iloc[i] == 1.0:
            if pos.iloc[i - 1] == 0.0:
                entry_price = close_h.iloc[i]
            elif entry_price is not None:
                pnl_pct = (close_h.iloc[i] - entry_price) / entry_price * 100.0
                if pnl_pct <= -CFG["stop_pct"] or pnl_pct >= CFG["tp_pct"]:
                    pos.iloc[i] = 0.0
                    entry_price = None
        else:
            entry_price = None

    # ── compute returns ────────────────────────────────────
    raw_ret = close_h.pct_change().fillna(0.0)
    strategy_ret = pos.shift(1).fillna(0.0) * raw_ret
    trade_change = pos.diff().abs().fillna(pos.iloc[0])
    strategy_ret = strategy_ret - trade_change * (CFG["fee"] + CFG["slip"])

    equity = 10_000.0 * (1.0 + strategy_ret).cumprod()
    total_return = float(equity.iloc[-1] / 10_000.0 - 1.0)

    bars_per_year = (60 // 15) * 24 * 365
    trades, win_rate = estimate_trade_stats(pos, strategy_ret)
    sharpe = annualized_sharpe(strategy_ret, bars_per_year)
    mdd = max_drawdown_pct(equity)
    ann_ret = annualize_return(total_return, len(close_h), bars_per_year) * 100.0

    # ── report ─────────────────────────────────────────────
    print("\n" + "═" * 60)
    print("  SOL 15m — FINAL HOLDOUT RESULTS")
    print("═" * 60)
    print(f"  Holdout bars      : {len(close_h)}")
    print(f"  Total return      : {total_return * 100.0:+.2f}%")
    print(f"  Annualized return : {ann_ret:+.2f}%")
    print(f"  Sharpe ratio      : {sharpe:.2f}")
    print(f"  Max drawdown      : {mdd:.2f}%")
    print(f"  # Trades          : {trades}")
    print(f"  Win rate          : {win_rate:.1f}%")
    print("═" * 60)

    # go / no-go
    go = (
        total_return > 0.0
        and win_rate > 50.0
        and mdd < 10.0
        and trades >= 5
    )
    verdict = "✅ GO" if go else "❌ NO-GO"
    print(f"\n  Verdict: {verdict}\n")

    # ── save results ───────────────────────────────────────
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    result = {
        "test": "sol_15m_holdout",
        "timestamp": ts,
        "config": CFG,
        "train_bars": len(train_df),
        "holdout_bars": len(hold_df),
        "train_period": [str(train_df["timestamp"].iloc[0]), str(train_df["timestamp"].iloc[-1])],
        "holdout_period": [str(hold_df["timestamp"].iloc[0]), str(hold_df["timestamp"].iloc[-1])],
        "total_return_pct": round(total_return * 100.0, 4),
        "annualized_return_pct": round(ann_ret, 4),
        "sharpe": round(sharpe, 4),
        "max_drawdown_pct": round(mdd, 4),
        "trades": trades,
        "win_rate_pct": round(win_rate, 2),
        "verdict": verdict,
    }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"sol_15m_holdout_{ts}.json"
    out_path.write_text(json.dumps(result, indent=2))
    print(f"  Results saved → {out_path}\n")
    return result


if __name__ == "__main__":
    run_holdout()
