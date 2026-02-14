"""
SOL 15m Holdout — Rolling & Incremental HMM Retraining
-------------------------------------------------------
Compares three HMM modes on the same 80/20 holdout split:
  1) frozen   — train once, apply to holdout (baseline)
  2) rolling  — retrain from scratch every R bars on a window of W bars
  3) incremental — light update (2 EM iters) every R bars on recent W bars

Uses the same frozen strategy config as sol_15m_holdout_test.py.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from src.scripts.hmm_regime_test import (
    GaussianHMM2,
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

# Retraining parameters
RETRAIN_EVERY = 200   # bars between retraining
TRAIN_WINDOW = 2000   # rolling window size for retraining

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


def fit_hmm_on_returns(log_returns: np.ndarray, max_iter: int = 120) -> GaussianHMM2:
    """Fit a fresh HMM on the given log returns."""
    hmm = GaussianHMM2(max_iter=max_iter, tol=1e-6)
    hmm.fit(log_returns)
    return hmm


def incremental_update(hmm: GaussianHMM2, log_returns: np.ndarray, em_iters: int = 2) -> GaussianHMM2:
    """Run a few EM iterations on new data starting from existing parameters."""
    hmm_new = copy.deepcopy(hmm)
    x = np.asarray(log_returns, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) < 200:
        return hmm_new  # not enough data, keep existing

    prev = -np.inf
    for _ in range(em_iters):
        gamma, xi, _, ll = hmm_new._forward_backward(x)

        hmm_new.pi = gamma[0]
        hmm_new.pi /= np.maximum(hmm_new.pi.sum(), 1e-300)

        xi_sum = xi.sum(axis=0)
        gamma_sum_t = np.maximum(gamma[:-1].sum(axis=0), 1e-300)
        hmm_new.A = xi_sum / gamma_sum_t[:, None]
        hmm_new.A /= np.maximum(hmm_new.A.sum(axis=1, keepdims=True), 1e-300)

        w = np.maximum(gamma.sum(axis=0), 1e-300)
        hmm_new.mu = (gamma * x[:, None]).sum(axis=0) / w
        hmm_new.var = (
            (gamma * (x[:, None] - hmm_new.mu[None, :]) ** 2).sum(axis=0) / w
        ).clip(1e-8)

        if abs(ll - prev) < hmm_new.tol:
            break
        prev = ll

    return hmm_new


def compute_indicators(train_df: pd.DataFrame, hold_df: pd.DataFrame):
    """Compute SMA, RSI, ATR, vol_ratio on holdout with proper lookback from train."""
    lookback = max(CFG["sma_slow"], CFG["rsi_period"], CFG["atr_period"]) + 10
    hc = hold_df["close"].astype(float).reset_index(drop=True)

    ext_c = pd.concat(
        [train_df["close"].iloc[-lookback:], hc], ignore_index=True
    ).astype(float)
    ext_h = pd.concat(
        [train_df["high"].iloc[-lookback:], hold_df["high"].reset_index(drop=True)], ignore_index=True
    ).astype(float)
    ext_l = pd.concat(
        [train_df["low"].iloc[-lookback:], hold_df["low"].reset_index(drop=True)], ignore_index=True
    ).astype(float)
    ext_v = pd.concat(
        [train_df["volume"].iloc[-lookback:], hold_df["volume"].reset_index(drop=True)], ignore_index=True
    ).astype(float)

    sma_fast = ext_c.rolling(CFG["sma_fast"]).mean().iloc[lookback:].reset_index(drop=True)
    sma_slow = ext_c.rolling(CFG["sma_slow"]).mean().iloc[lookback:].reset_index(drop=True)
    rsi_vals = rsi(ext_c, CFG["rsi_period"]).iloc[lookback:].reset_index(drop=True)

    tr = pd.concat([
        ext_h - ext_l,
        (ext_h - ext_c.shift(1)).abs(),
        (ext_l - ext_c.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr_pct = ((tr.rolling(CFG["atr_period"]).mean() / ext_c) * 100.0).iloc[lookback:].reset_index(drop=True)
    vol_ratio = (ext_v / ext_v.rolling(20).mean()).iloc[lookback:].reset_index(drop=True)

    return hc, sma_fast, sma_slow, rsi_vals, atr_pct, vol_ratio


def apply_strategy(
    close: pd.Series,
    prob_bull: np.ndarray,
    sma_fast: pd.Series,
    sma_slow: pd.Series,
    rsi_vals: pd.Series,
    atr_pct: pd.Series,
    vol_ratio: pd.Series,
) -> dict:
    """Apply the frozen strategy config and return metrics dict."""
    n = len(close)
    pos = pd.Series(np.zeros(n), dtype=float)

    for i in range(1, n):
        if prob_bull[i] < CFG["prob"]:
            continue
        if pd.isna(sma_fast.iloc[i]) or pd.isna(sma_slow.iloc[i]):
            continue
        if sma_fast.iloc[i] <= sma_slow.iloc[i]:
            continue
        rsi_val = rsi_vals.iloc[i]
        if pd.isna(rsi_val) or rsi_val > CFG["rsi_entry"] or rsi_val < CFG["rsi_exit"]:
            continue
        if not pd.isna(vol_ratio.iloc[i]) and vol_ratio.iloc[i] > CFG["vol_thr"]:
            continue
        if not pd.isna(atr_pct.iloc[i]) and atr_pct.iloc[i] > CFG["atr_max_pct"]:
            continue
        pos.iloc[i] = 1.0

    # stop-loss / take-profit
    entry_price = None
    for i in range(1, n):
        if pos.iloc[i] == 1.0:
            if pos.iloc[i - 1] == 0.0:
                entry_price = close.iloc[i]
            elif entry_price is not None:
                pnl_pct = (close.iloc[i] - entry_price) / entry_price * 100.0
                if pnl_pct <= -CFG["stop_pct"] or pnl_pct >= CFG["tp_pct"]:
                    pos.iloc[i] = 0.0
                    entry_price = None
        else:
            entry_price = None

    raw_ret = close.pct_change().fillna(0.0)
    strategy_ret = pos.shift(1).fillna(0.0) * raw_ret
    trade_change = pos.diff().abs().fillna(pos.iloc[0])
    strategy_ret = strategy_ret - trade_change * (CFG["fee"] + CFG["slip"])

    equity = 10_000.0 * (1.0 + strategy_ret).cumprod()
    total_return = float(equity.iloc[-1] / 10_000.0 - 1.0)
    bars_per_year = (60 // 15) * 24 * 365
    trades, win_rate = estimate_trade_stats(pos, strategy_ret)
    sharpe = annualized_sharpe(strategy_ret, bars_per_year)
    mdd = max_drawdown_pct(equity)
    ann_ret = annualize_return(total_return, n, bars_per_year) * 100.0

    go = total_return > 0.0 and win_rate > 50.0 and mdd < 10.0 and trades >= 5
    return {
        "total_return_pct": round(total_return * 100.0, 4),
        "annualized_return_pct": round(ann_ret, 4),
        "sharpe": round(sharpe, 4),
        "max_drawdown_pct": round(mdd, 4),
        "trades": trades,
        "win_rate_pct": round(win_rate, 2),
        "verdict": "GO" if go else "NO-GO",
        "bars_in_position": int(pos.sum()),
    }


def run_comparison():
    # ── load & split ───────────────────────────────────────
    df = pd.read_csv(DATA_PATH)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values("timestamp").reset_index(drop=True)
    split_idx = int(len(df) * 0.80)
    train_df = df.iloc[:split_idx].reset_index(drop=True)
    hold_df = df.iloc[split_idx:].reset_index(drop=True)
    print(f"Total: {len(df)}  Train: {len(train_df)}  Holdout: {len(hold_df)}")
    print(f"Holdout: {hold_df['timestamp'].iloc[0]} → {hold_df['timestamp'].iloc[-1]}")

    # All log returns (train + holdout concatenated for windowing)
    all_close = df["close"].astype(float)
    all_logret = np.log(all_close / all_close.shift(1)).fillna(0.0).values

    # Indicators (computed once — they don't depend on HMM)
    close_h, sma_fast, sma_slow, rsi_vals, atr_pct, vol_ratio = compute_indicators(train_df, hold_df)
    n_hold = len(close_h)

    # ── MODE 1: FROZEN ─────────────────────────────────────
    print("\n▶ Mode: FROZEN")
    hmm_frozen = fit_hmm_on_returns(all_logret[:split_idx])
    bull_frozen = int(np.argmax(hmm_frozen.mu))
    hold_lr = all_logret[split_idx:]
    gamma_f, _, _, _ = hmm_frozen._forward_backward(hold_lr)
    prob_bull_frozen = gamma_f[:, bull_frozen]
    print(f"  Bull bars: {(prob_bull_frozen >= CFG['prob']).sum()} / {n_hold}")
    res_frozen = apply_strategy(close_h, prob_bull_frozen, sma_fast, sma_slow, rsi_vals, atr_pct, vol_ratio)

    # ── MODE 2: ROLLING ────────────────────────────────────
    print("\n▶ Mode: ROLLING (retrain every {}, window {})".format(RETRAIN_EVERY, TRAIN_WINDOW))
    prob_bull_rolling = np.zeros(n_hold)
    current_hmm = copy.deepcopy(hmm_frozen)
    current_bull = bull_frozen

    for i in range(n_hold):
        global_idx = split_idx + i
        # Retrain at every RETRAIN_EVERY bars
        if i % RETRAIN_EVERY == 0:
            window_start = max(0, global_idx - TRAIN_WINDOW)
            window_data = all_logret[window_start:global_idx]
            if len(window_data) >= 200:
                current_hmm = fit_hmm_on_returns(window_data)
                current_bull = int(np.argmax(current_hmm.mu))
                if i == 0:
                    print(f"  Retrain at bar {i}: window [{window_start}:{global_idx}], bull={current_bull}")

        # Forward inference for this single bar using accumulated context
        # For efficiency, do forward-backward on small chunks
        chunk_start = max(0, i - 100)
        chunk_lr = hold_lr[chunk_start:i + 1]
        if len(chunk_lr) >= 1:
            gamma_r, _, _, _ = current_hmm._forward_backward(chunk_lr)
            prob_bull_rolling[i] = gamma_r[-1, current_bull]

    print(f"  Bull bars: {(prob_bull_rolling >= CFG['prob']).sum()} / {n_hold}")
    res_rolling = apply_strategy(close_h, prob_bull_rolling, sma_fast, sma_slow, rsi_vals, atr_pct, vol_ratio)

    # ── MODE 3: INCREMENTAL ────────────────────────────────
    print("\n▶ Mode: INCREMENTAL (update every {}, window {}, 2 EM iters)".format(RETRAIN_EVERY, TRAIN_WINDOW))
    prob_bull_incr = np.zeros(n_hold)
    incr_hmm = copy.deepcopy(hmm_frozen)
    incr_bull = bull_frozen

    for i in range(n_hold):
        global_idx = split_idx + i
        if i % RETRAIN_EVERY == 0 and i > 0:
            window_start = max(0, global_idx - TRAIN_WINDOW)
            window_data = all_logret[window_start:global_idx]
            if len(window_data) >= 200:
                incr_hmm = incremental_update(incr_hmm, window_data, em_iters=2)
                incr_bull = int(np.argmax(incr_hmm.mu))

        chunk_start = max(0, i - 100)
        chunk_lr = hold_lr[chunk_start:i + 1]
        if len(chunk_lr) >= 1:
            gamma_i, _, _, _ = incr_hmm._forward_backward(chunk_lr)
            prob_bull_incr[i] = gamma_i[-1, incr_bull]

    print(f"  Bull bars: {(prob_bull_incr >= CFG['prob']).sum()} / {n_hold}")
    res_incr = apply_strategy(close_h, prob_bull_incr, sma_fast, sma_slow, rsi_vals, atr_pct, vol_ratio)

    # ── comparison table ───────────────────────────────────
    print("\n" + "═" * 72)
    print("  SOL 15m HOLDOUT — HMM MODE COMPARISON")
    print("═" * 72)
    header = f"  {'metric':<22} {'frozen':>12} {'rolling':>12} {'incremental':>12}"
    print(header)
    print("  " + "─" * 68)
    for key in ["total_return_pct", "win_rate_pct", "sharpe", "max_drawdown_pct", "trades", "bars_in_position", "verdict"]:
        print(f"  {key:<22} {str(res_frozen[key]):>12} {str(res_rolling[key]):>12} {str(res_incr[key]):>12}")
    print("═" * 72)

    # ── save ───────────────────────────────────────────────
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    result = {
        "test": "sol_15m_holdout_rolling_comparison",
        "timestamp": ts,
        "config": CFG,
        "retrain_every": RETRAIN_EVERY,
        "train_window": TRAIN_WINDOW,
        "holdout_bars": n_hold,
        "holdout_period": [str(hold_df["timestamp"].iloc[0]), str(hold_df["timestamp"].iloc[-1])],
        "frozen": res_frozen,
        "rolling": res_rolling,
        "incremental": res_incr,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"sol_15m_holdout_rolling_{ts}.json"
    out_path.write_text(json.dumps(result, indent=2))
    print(f"\n  Results saved → {out_path}\n")
    return result


if __name__ == "__main__":
    run_comparison()
