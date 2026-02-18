"""
Hedged RSI Short Strategy — Extended Backtest
──────────────────────────────────────────────
Compares 4 variants of RSI-based short strategies on 1h data (~7 months):

  1. Naked RSI Short        — baseline, no protection
  2. RSI Short + Stop-Loss  — hard 3% stop on each trade
  3. RSI Short + Sized       — 10% of capital per trade + stop
  4. Hedged RSI Short       — sized + partial BTC long hedge + stop

All use Hyperliquid 1h data (5000 bars ≈ 208 days).
Accepts a coin argument: python hedged_short_backtest.py <COIN>

Protection mechanisms:
  - Hard stop-loss: exit at +3% adverse move
  - Position sizing: risk only 10% of capital per trade
  - BTC hedge: for every $1 short, go $0.30 long BTC (lower-beta hedge)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

# ── paths ──────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
RESULTS_DIR = PROJECT_ROOT / "src" / "data" / "execution_results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

FEE = 0.0008
SLIP = 0.0003
INIT_CAPITAL = 10_000.0
BARS_PER_YEAR_1H = 24 * 365


# ═══════════════════════════════════════════════════════════
#  DATA
# ═══════════════════════════════════════════════════════════

def fetch_ohlcv(symbol: str, interval: str = "1h", bars: int = 5000) -> pd.DataFrame:
    url = "https://api.hyperliquid.xyz/info"
    end = datetime.utcnow()
    start = end - timedelta(days=250)  # wide window to get ~5000 1h bars
    resp = requests.post(url, json={
        "type": "candleSnapshot",
        "req": {"coin": symbol, "interval": interval,
                "startTime": int(start.timestamp() * 1000),
                "endTime": int(end.timestamp() * 1000),
                "limit": min(bars, 5000)}
    }, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"API {resp.status_code}")
    raw = resp.json()
    if not raw:
        raise RuntimeError("Empty data")
    rows = [{"timestamp": pd.Timestamp(c["t"], unit="ms", tz="UTC"),
             "open": float(c["o"]), "high": float(c["h"]),
             "low": float(c["l"]), "close": float(c["c"]),
             "volume": float(c["v"])} for c in raw]
    df = pd.DataFrame(rows).sort_values("timestamp").tail(bars).reset_index(drop=True)
    print(f"   {symbol} {interval}: {len(df)} bars | {df['timestamp'].iloc[0].date()} → {df['timestamp'].iloc[-1].date()}")
    return df


def add_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    c = df["close"]
    delta = c.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    ag = gain.ewm(alpha=1/period, min_periods=period).mean()
    al = loss.ewm(alpha=1/period, min_periods=period).mean()
    rs = ag / al.replace(0.0, np.nan)
    df["rsi"] = 100.0 - 100.0 / (1.0 + rs)
    return df


# ═══════════════════════════════════════════════════════════
#  STRATEGY VARIANTS
# ═══════════════════════════════════════════════════════════

def run_naked_short(close: pd.Series, rsi: pd.Series) -> pd.Series:
    """Variant 1: Naked RSI Short — no protection."""
    n = len(close)
    pos = pd.Series(0.0, index=range(n))
    in_short = False
    for i in range(1, n):
        r = rsi.iloc[i]
        if pd.isna(r):
            continue
        if not in_short and r > 70:
            in_short = True
        elif in_short and r < 40:
            in_short = False
        pos.iloc[i] = -1.0 if in_short else 0.0
    return pos


def run_short_with_stop(close: pd.Series, rsi: pd.Series,
                        stop_pct: float = 3.0) -> pd.Series:
    """Variant 2: RSI Short + hard stop-loss at +stop_pct% adverse move."""
    n = len(close)
    pos = pd.Series(0.0, index=range(n))
    in_short = False
    entry_price = 0.0
    for i in range(1, n):
        r = rsi.iloc[i]
        p = close.iloc[i]
        if pd.isna(r):
            continue

        if in_short:
            # check stop-loss (price rose too much above entry)
            loss_pct = (p - entry_price) / entry_price * 100.0
            if loss_pct >= stop_pct:
                in_short = False
                pos.iloc[i] = 0.0
                continue
            # check RSI exit
            if r < 40:
                in_short = False
                pos.iloc[i] = 0.0
                continue
            pos.iloc[i] = -1.0
        else:
            if r > 70:
                in_short = True
                entry_price = p
                pos.iloc[i] = -1.0
            else:
                pos.iloc[i] = 0.0
    return pos


def compute_sized_equity(pos: pd.Series, close: pd.Series,
                         alloc_frac: float = 1.0,
                         hedge_close: pd.Series | None = None,
                         hedge_ratio: float = 0.0) -> tuple:
    """
    Compute equity curve with position sizing and optional hedge.

    alloc_frac: fraction of capital allocated per trade (1.0 = full, 0.1 = 10%)
    hedge_close: BTC close series for hedge leg
    hedge_ratio: e.g. 0.30 means for every $1 short, go $0.30 long BTC
    """
    n = len(close)
    raw_ret = close.pct_change().fillna(0.0)

    # Short leg returns (position * return, with fees)
    short_ret = pos.shift(1).fillna(0.0) * raw_ret
    tc = pos.diff().abs().fillna(abs(pos.iloc[0]))
    short_ret -= tc * (FEE + SLIP)

    # Hedge leg returns (long BTC when short is on)
    hedge_ret = pd.Series(0.0, index=range(n))
    if hedge_close is not None and hedge_ratio > 0:
        btc_ret = hedge_close.pct_change().fillna(0.0)
        # hedge is ON when short is ON (pos < 0)
        hedge_pos = (pos < 0).astype(float)
        hedge_ret = hedge_pos.shift(1).fillna(0.0) * btc_ret * hedge_ratio
        htc = hedge_pos.diff().abs().fillna(hedge_pos.iloc[0])
        hedge_ret -= htc * hedge_ratio * (FEE + SLIP)

    # Combined returns, scaled by allocation
    total_ret = (short_ret + hedge_ret) * alloc_frac

    equity = INIT_CAPITAL * (1.0 + total_ret).cumprod()
    return equity, total_ret


def compute_metrics(equity: pd.Series, strat_ret: pd.Series,
                    pos: pd.Series, label: str) -> dict:
    total_ret = equity.iloc[-1] / INIT_CAPITAL - 1.0
    sr_std = strat_ret.std()
    sharpe = (strat_ret.mean() / sr_std * np.sqrt(BARS_PER_YEAR_1H)) if sr_std > 0 else 0.0
    peak = equity.cummax()
    dd = (equity - peak) / peak * 100.0
    mdd = abs(dd.min())

    # Trades
    trade_rets = []
    in_trade = False
    entry_ret = 0.0
    prev = 0.0
    for i in range(len(pos)):
        cur = pos.iloc[i]
        if cur != 0.0 and prev == 0.0:
            in_trade = True
            entry_ret = 0.0
        if in_trade:
            entry_ret += strat_ret.iloc[i]
        if in_trade and (cur == 0.0 or i == len(pos) - 1):
            trade_rets.append(entry_ret)
            in_trade = False
        prev = cur

    n_trades = len(trade_rets)
    win_rate = (sum(1 for r in trade_rets if r > 0) / n_trades * 100.0) if n_trades > 0 else 0.0
    avg_win = np.mean([r for r in trade_rets if r > 0]) * 100 if any(r > 0 for r in trade_rets) else 0
    avg_loss = np.mean([r for r in trade_rets if r <= 0]) * 100 if any(r <= 0 for r in trade_rets) else 0
    pct_in = (pos.abs() > 0).mean() * 100.0

    # Worst single trade
    worst_trade = min(trade_rets) * 100 if trade_rets else 0

    return {
        "strategy": label,
        "total_return_pct": round(total_ret * 100, 2),
        "sharpe": round(sharpe, 2),
        "max_dd_pct": round(mdd, 2),
        "trades": n_trades,
        "win_rate_pct": round(win_rate, 1),
        "avg_win_pct": round(avg_win, 3),
        "avg_loss_pct": round(avg_loss, 3),
        "worst_trade_pct": round(worst_trade, 3),
        "pct_in_market": round(pct_in, 1),
    }


# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════

def run_backtest(coin: str = "SOL"):
    print("=" * 80)
    print(f"  HEDGED SHORT STRATEGY — {coin}/USD 1h (~7 months)")
    print("=" * 80)

    # Fetch data
    print("\n📡 Fetching data...")
    df = fetch_ohlcv(coin, "1h", 5000)
    btc_df = fetch_ohlcv("BTC", "1h", 5000)

    # Align timestamps (inner join)
    df = df.set_index("timestamp")
    btc_df = btc_df.set_index("timestamp")
    common = df.index.intersection(btc_df.index)
    df = df.loc[common].reset_index()
    btc_df = btc_df.loc[common].reset_index()
    print(f"   Aligned: {len(df)} bars | {df['timestamp'].iloc[0].date()} → {df['timestamp'].iloc[-1].date()}")

    months = (df['timestamp'].iloc[-1] - df['timestamp'].iloc[0]).days / 30.44
    print(f"   Period: ~{months:.1f} months")

    # Add RSI
    df = add_rsi(df)

    close = df["close"].astype(float).reset_index(drop=True)
    rsi = df["rsi"].reset_index(drop=True)
    btc_close = btc_df["close"].astype(float).reset_index(drop=True)

    # Buy and hold baseline
    bh_ret = (close.iloc[-1] / close.iloc[0] - 1.0) * 100.0
    btc_bh = (btc_close.iloc[-1] / btc_close.iloc[0] - 1.0) * 100.0
    print(f"\n   {coin} Buy & Hold: {bh_ret:+.1f}%  |  BTC Buy & Hold: {btc_bh:+.1f}%")
    print(f"   {coin}: ${close.iloc[0]:.4f} → ${close.iloc[-1]:.4f}")

    # ── Variant 1: Naked Short ─────────────────────────────
    pos1 = run_naked_short(close, rsi)
    eq1, ret1 = compute_sized_equity(pos1, close, alloc_frac=1.0)
    m1 = compute_metrics(eq1, ret1, pos1, "1. Naked Short")

    # ── Variant 2: Short + Stop ────────────────────────────
    pos2 = run_short_with_stop(close, rsi, stop_pct=3.0)
    eq2, ret2 = compute_sized_equity(pos2, close, alloc_frac=1.0)
    m2 = compute_metrics(eq2, ret2, pos2, "2. Short + 3% Stop")

    # ── Variant 3: Sized + Stop ────────────────────────────
    pos3 = run_short_with_stop(close, rsi, stop_pct=3.0)
    eq3, ret3 = compute_sized_equity(pos3, close, alloc_frac=0.10)
    m3 = compute_metrics(eq3, ret3, pos3, "3. Sized 10% + Stop")

    # ── Variant 4: Hedged (Sized + Stop + BTC Long) ───────
    pos4 = run_short_with_stop(close, rsi, stop_pct=3.0)
    eq4, ret4 = compute_sized_equity(pos4, close, alloc_frac=0.10,
                                      hedge_close=btc_close, hedge_ratio=0.30)
    m4 = compute_metrics(eq4, ret4, pos4, "4. Hedged + Sized")

    results = [m1, m2, m3, m4]

    # ── Print comparison ───────────────────────────────────
    print("\n" + "═" * 100)
    print(f"  RSI SHORT VARIANTS — {coin}/USD 1h | {df['timestamp'].iloc[0].date()} → {df['timestamp'].iloc[-1].date()} ({months:.0f}mo)")
    print("═" * 100)
    print(f"  {'Strategy':<22} {'Return':>9} {'Sharpe':>8} {'MaxDD':>8} {'Trades':>7} {'WR':>7} {'AvgWin':>8} {'AvgLoss':>9} {'Worst':>8} {'InMkt':>7}")
    print("  " + "─" * 96)
    for r in results:
        print(f"  {r['strategy']:<22} {r['total_return_pct']:>+8.2f}% {r['sharpe']:>8.2f} "
              f"{r['max_dd_pct']:>7.2f}% {r['trades']:>7} {r['win_rate_pct']:>6.1f}% "
              f"{r['avg_win_pct']:>+7.3f}% {r['avg_loss_pct']:>+8.3f}% "
              f"{r['worst_trade_pct']:>+7.3f}% {r['pct_in_market']:>6.1f}%")
    print("  " + "─" * 96)
    print(f"  {'Buy & Hold':<22} {bh_ret:>+8.2f}%")
    print(f"  {'BTC B&H (hedge ref)':<22} {btc_bh:>+8.2f}%")
    print("═" * 100)

    # ── Protection analysis ────────────────────────────────
    print("\n  📊 PROTECTION ANALYSIS:")
    if m1['max_dd_pct'] > 0:
        dd_reduction = (1 - m4['max_dd_pct'] / m1['max_dd_pct']) * 100
        print(f"     Max drawdown reduction (naked→hedged): {m1['max_dd_pct']:.1f}% → {m4['max_dd_pct']:.1f}% ({dd_reduction:+.0f}%)")
    if m1['worst_trade_pct'] < 0:
        worst_reduction = (1 - abs(m4['worst_trade_pct']) / abs(m1['worst_trade_pct'])) * 100
        print(f"     Worst trade reduction: {m1['worst_trade_pct']:+.2f}% → {m4['worst_trade_pct']:+.2f}% ({worst_reduction:+.0f}%)")
    print(f"     Stop-loss saved {m1['trades'] - m2['trades']} early exits (stopped out before RSI recovery)")

    # ── Save ───────────────────────────────────────────────
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    result_json = {
        "test": f"{coin.lower()}_hedged_short_backtest",
        "coin": coin,
        "timestamp": ts,
        "data_bars": len(df),
        "data_period": [str(df["timestamp"].iloc[0]), str(df["timestamp"].iloc[-1])],
        "months": round(months, 1),
        "buy_and_hold_pct": round(bh_ret, 2),
        "btc_buy_and_hold_pct": round(btc_bh, 2),
        "variants": results,
    }
    out_path = RESULTS_DIR / f"{coin.lower()}_hedged_short_{ts}.json"
    out_path.write_text(json.dumps(result_json, indent=2))
    print(f"\n  💾 Results → {out_path}\n")
    return result_json


if __name__ == "__main__":
    import sys
    coins = sys.argv[1:] if len(sys.argv) > 1 else ["SOL"]
    coins = [c.upper() for c in coins]
    for coin in coins:
        run_backtest(coin)
