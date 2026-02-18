"""
SOL Hyperliquid Trading Simulation
───────────────────────────────────
Fetches the latest SOL/USD 15m data from Hyperliquid and runs a
multi-strategy backtest simulation covering 13 strategies:

  LONG-ONLY:
   1. SMA Cross 20/50             6. Bollinger Band Reversion
   2. RSI Reversion               7. Donchian Breakout
   3. Momentum ROC10              8. EMA Triple Cross
   4. MACD Signal Cross           9. VWAP Reversion
   5. HMM Adaptive               10. Stochastic Oversold

  SHORT-SIDE:
  11. SMA Cross Short            12. RSI Overbought Short
  13. Momentum Short

All strategies apply realistic fees (0.08%) and slippage (0.03%).
Positions: +1 = long, -1 = short, 0 = flat.
Results are printed as a comparison table and saved as JSON.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests, time

# ── project paths ──────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
OHLCV_DIR = PROJECT_ROOT / "src" / "data" / "ohlcv" / "hyperliquid"
RESULTS_DIR = PROJECT_ROOT / "src" / "data" / "execution_results"
OHLCV_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

FEE = 0.0008
SLIP = 0.0003
INIT_CAPITAL = 10_000.0
BARS_PER_YEAR = (60 // 15) * 24 * 365  # 15m bars


# ═══════════════════════════════════════════════════════════
#  DATA FETCHING
# ═══════════════════════════════════════════════════════════

def fetch_hyperliquid_ohlcv(symbol: str = "SOL", interval: str = "15m",
                            bars: int = 5000) -> pd.DataFrame:
    """Fetch OHLCV candles from Hyperliquid candleSnapshot API."""
    url = "https://api.hyperliquid.xyz/info"
    end_time = datetime.utcnow()
    start_time = end_time - timedelta(days=30)  # 1 month window

    print(f"📡 Fetching {symbol} {interval} from Hyperliquid ({bars} bars)...")
    resp = requests.post(url, json={
        "type": "candleSnapshot",
        "req": {
            "coin": symbol,
            "interval": interval,
            "startTime": int(start_time.timestamp() * 1000),
            "endTime": int(end_time.timestamp() * 1000),
            "limit": min(bars, 5000),
        }
    }, timeout=15)

    if resp.status_code != 200:
        raise RuntimeError(f"API error {resp.status_code}: {resp.text}")

    raw = resp.json()
    if not raw:
        raise RuntimeError("Empty response from Hyperliquid")

    rows = []
    for c in raw:
        rows.append({
            "timestamp": pd.Timestamp(c["t"], unit="ms", tz="UTC"),
            "open": float(c["o"]),
            "high": float(c["h"]),
            "low":  float(c["l"]),
            "close": float(c["c"]),
            "volume": float(c["v"]),
        })
    df = pd.DataFrame(rows).sort_values("timestamp").tail(bars).reset_index(drop=True)
    print(f"   ✅ {len(df)} bars  |  {df['timestamp'].iloc[0]} → {df['timestamp'].iloc[-1]}")
    return df


# ═══════════════════════════════════════════════════════════
#  INDICATORS
# ═══════════════════════════════════════════════════════════

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add all technical indicators needed by the strategies."""
    c = df["close"]
    h = df["high"]
    l = df["low"]
    v = df["volume"]

    # SMAs
    df["sma20"] = c.rolling(20).mean()
    df["sma50"] = c.rolling(50).mean()

    # EMAs (for triple cross)
    df["ema8"] = c.ewm(span=8, adjust=False).mean()
    df["ema21"] = c.ewm(span=21, adjust=False).mean()
    df["ema55"] = c.ewm(span=55, adjust=False).mean()

    # RSI
    delta = c.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    ag = gain.ewm(alpha=1/14, min_periods=14).mean()
    al = loss.ewm(alpha=1/14, min_periods=14).mean()
    rs = ag / al.replace(0.0, np.nan)
    df["rsi"] = 100.0 - 100.0 / (1.0 + rs)

    # Stochastic %K / %D
    low14 = l.rolling(14).min()
    high14 = h.rolling(14).max()
    df["stoch_k"] = ((c - low14) / (high14 - low14).replace(0, np.nan)) * 100.0
    df["stoch_d"] = df["stoch_k"].rolling(3).mean()

    # MACD
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()

    # Rate of change (momentum)
    df["roc10"] = c.pct_change(10) * 100.0

    # Bollinger Bands
    df["bb_mid"] = c.rolling(20).mean()
    bb_std = c.rolling(20).std()
    df["bb_upper"] = df["bb_mid"] + 2.0 * bb_std
    df["bb_lower"] = df["bb_mid"] - 2.0 * bb_std

    # Donchian Channel (20-bar)
    df["don_high"] = h.rolling(20).max()
    df["don_low"] = l.rolling(20).min()

    # VWAP (cumulative intraday proxy — rolling 96 bars = 1 day of 15m)
    tp = (h + l + c) / 3.0
    df["vwap"] = (tp * v).rolling(96).sum() / v.rolling(96).sum().replace(0, np.nan)

    # ATR
    tr = pd.concat([
        h - l,
        (h - c.shift(1)).abs(),
        (l - c.shift(1)).abs(),
    ], axis=1).max(axis=1)
    df["atr14"] = tr.rolling(14).mean()
    df["atr_pct"] = (df["atr14"] / c) * 100.0

    # Volume ratio
    df["vol_ratio"] = v / v.rolling(20).mean()

    return df


# ═══════════════════════════════════════════════════════════
#  HMM (lightweight 2-state Gaussian)
# ═══════════════════════════════════════════════════════════

class GaussianHMM2:
    """Minimal 2-state Gaussian HMM with EM fitting."""

    def __init__(self, max_iter=80, tol=1e-5):
        self.max_iter = max_iter
        self.tol = tol
        self.pi = np.array([0.5, 0.5])
        self.A = np.array([[0.95, 0.05], [0.05, 0.95]])
        self.mu = np.array([0.0, 0.0])
        self.var = np.array([1e-4, 1e-4])

    @staticmethod
    def _gauss(x, mu, var):
        return np.exp(-0.5 * ((x[:, None] - mu[None, :]) ** 2) / var[None, :]) / \
               np.sqrt(2 * np.pi * var[None, :])

    def _forward_backward(self, x):
        T = len(x)
        B = self._gauss(x, self.mu, self.var)
        alpha = np.zeros((T, 2))
        c = np.zeros(T)
        alpha[0] = self.pi * B[0]
        c[0] = max(alpha[0].sum(), 1e-300)
        alpha[0] /= c[0]
        for t in range(1, T):
            alpha[t] = (alpha[t-1] @ self.A) * B[t]
            c[t] = max(alpha[t].sum(), 1e-300)
            alpha[t] /= c[t]
        beta = np.zeros((T, 2))
        beta[-1] = 1.0
        for t in range(T-2, -1, -1):
            beta[t] = (self.A @ (B[t+1] * beta[t+1])) / max(c[t+1], 1e-300)
        gamma = alpha * beta
        gamma /= np.maximum(gamma.sum(axis=1, keepdims=True), 1e-300)
        xi = np.zeros((T-1, 2, 2))
        for t in range(T-1):
            d = max((alpha[t][:, None] * self.A * (B[t+1] * beta[t+1])[None, :]).sum(), 1e-300)
            xi[t] = alpha[t][:, None] * self.A * (B[t+1] * beta[t+1])[None, :] / d
        return gamma, xi

    def fit(self, x):
        x = np.asarray(x, dtype=float)
        x = x[np.isfinite(x)]
        if len(x) < 200:
            raise ValueError("Need ≥200 samples")
        q25, q75 = np.quantile(x, [0.25, 0.75])
        self.mu = np.array([q25, q75])
        bv = max(np.var(x), 1e-8)
        self.var = np.array([bv, bv])
        prev = -np.inf
        for _ in range(self.max_iter):
            gamma, xi = self._forward_backward(x)
            self.pi = gamma[0] / max(gamma[0].sum(), 1e-300)
            xs = xi.sum(0)
            gs = np.maximum(gamma[:-1].sum(0), 1e-300)
            self.A = xs / gs[:, None]
            self.A /= np.maximum(self.A.sum(1, keepdims=True), 1e-300)
            w = np.maximum(gamma.sum(0), 1e-300)
            self.mu = (gamma * x[:, None]).sum(0) / w
            self.var = ((gamma * (x[:, None] - self.mu) ** 2).sum(0) / w).clip(1e-8)
            ll = np.log(np.maximum(np.array([alpha_sum for alpha_sum in [1.0]]), 1e-300)).sum()
            # crude convergence via parameter change
            break  # we'll just do one full pass for speed
        return gamma


# ═══════════════════════════════════════════════════════════
#  METRICS
# ═══════════════════════════════════════════════════════════

def compute_metrics(pos: pd.Series, close: pd.Series, label: str) -> dict:
    """Compute standardized metrics for a position series.
    pos can be +1 (long), -1 (short), or 0 (flat)."""
    raw_ret = close.pct_change().fillna(0.0)
    strat_ret = pos.shift(1).fillna(0.0) * raw_ret
    # transaction cost on every position change
    tc = pos.diff().abs().fillna(pos.iloc[0].item() if hasattr(pos.iloc[0], 'item') else abs(pos.iloc[0]))
    strat_ret -= tc * (FEE + SLIP)

    equity = INIT_CAPITAL * (1.0 + strat_ret).cumprod()
    total_ret = equity.iloc[-1] / INIT_CAPITAL - 1.0

    # Sharpe
    sr_std = strat_ret.std()
    sharpe = (strat_ret.mean() / sr_std * np.sqrt(BARS_PER_YEAR)) if sr_std > 0 else 0.0

    # Max drawdown
    peak = equity.cummax()
    dd = (equity - peak) / peak * 100.0
    mdd = abs(dd.min())

    # Trades & win rate  (a trade = entry → exit or position flip)
    trade_rets = []
    in_trade = False
    entry_ret = 0.0
    prev_pos = 0.0
    for i in range(len(pos)):
        cur = pos.iloc[i]
        if cur != 0.0 and prev_pos == 0.0:  # entry
            in_trade = True
            entry_ret = 0.0
        if in_trade:
            entry_ret += strat_ret.iloc[i]
        if in_trade and (cur == 0.0 or i == len(pos) - 1):
            trade_rets.append(entry_ret)
            in_trade = False
        if cur != prev_pos and cur != 0.0 and prev_pos != 0.0:
            # position flip (e.g. long→short): close old, open new
            trade_rets.append(entry_ret)
            entry_ret = 0.0
        prev_pos = cur

    n_trades = len(trade_rets)
    win_rate = (sum(1 for r in trade_rets if r > 0) / n_trades * 100.0) if n_trades > 0 else 0.0

    # Time in market
    pct_in_market = (pos.abs() > 0).mean() * 100.0

    go = total_ret > 0 and win_rate > 50 and mdd < 10 and n_trades >= 5
    return {
        "strategy": label,
        "total_return_pct": round(total_ret * 100, 2),
        "sharpe": round(sharpe, 2),
        "max_dd_pct": round(mdd, 2),
        "trades": n_trades,
        "win_rate_pct": round(win_rate, 1),
        "pct_in_market": round(pct_in_market, 1),
        "verdict": "✅ GO" if go else "❌ NO-GO",
    }


# ═══════════════════════════════════════════════════════════
#  STRATEGIES
# ═══════════════════════════════════════════════════════════

def strat_sma_cross(df: pd.DataFrame) -> pd.Series:
    """Long when SMA20 > SMA50."""
    return ((df["sma20"] > df["sma50"]).astype(float)).fillna(0.0)


def strat_rsi_reversion(df: pd.DataFrame) -> pd.Series:
    """Long when RSI < 35, exit when RSI > 65."""
    pos = pd.Series(0.0, index=df.index)
    in_pos = False
    for i in range(1, len(df)):
        rsi_val = df["rsi"].iloc[i]
        if pd.isna(rsi_val):
            continue
        if not in_pos and rsi_val < 35:
            in_pos = True
        elif in_pos and rsi_val > 65:
            in_pos = False
        pos.iloc[i] = 1.0 if in_pos else 0.0
    return pos


def strat_momentum(df: pd.DataFrame) -> pd.Series:
    """Long when ROC(10) > 1% and RSI < 70."""
    cond = (df["roc10"] > 1.0) & (df["rsi"] < 70) & (df["rsi"] > 30)
    return cond.astype(float).fillna(0.0)


def strat_macd_cross(df: pd.DataFrame) -> pd.Series:
    """Long when MACD > signal line and price > SMA50."""
    cond = (df["macd"] > df["macd_signal"]) & (df["close"] > df["sma50"])
    return cond.astype(float).fillna(0.0)


def strat_hmm_adaptive(df: pd.DataFrame) -> pd.Series:
    """HMM regime + SMA with rolling refit every 500 bars."""
    close = df["close"].astype(float)
    logret = np.log(close / close.shift(1)).fillna(0.0).values
    n = len(df)
    pos = pd.Series(0.0, index=df.index)

    hmm = None
    bull_state = 0
    refit_every = 500
    min_train = 500

    for i in range(min_train, n):
        # refit periodically
        if (i - min_train) % refit_every == 0:
            window_start = max(0, i - 2000)
            train_data = logret[window_start:i]
            if len(train_data) >= 200:
                hmm = GaussianHMM2(max_iter=60)
                try:
                    hmm.fit(train_data)
                    bull_state = int(np.argmax(hmm.mu))
                except:
                    hmm = None

        if hmm is None:
            continue

        # forward inference on recent chunk
        chunk_start = max(0, i - 100)
        chunk = logret[chunk_start:i + 1]
        try:
            gamma, _ = hmm._forward_backward(chunk)
            prob_bull = gamma[-1, bull_state]
        except:
            continue

        # gates
        sma20 = df["sma20"].iloc[i]
        sma50 = df["sma50"].iloc[i]
        rsi_val = df["rsi"].iloc[i]

        if (prob_bull > 0.55
            and not pd.isna(sma20) and not pd.isna(sma50) and sma20 > sma50
            and not pd.isna(rsi_val) and 40 < rsi_val < 60):
            pos.iloc[i] = 1.0

    return pos


# ── NEW LONG STRATEGIES ────────────────────────────────────

def strat_bollinger_reversion(df: pd.DataFrame) -> pd.Series:
    """Long when price touches lower BB, exit at mid BB."""
    pos = pd.Series(0.0, index=df.index)
    in_pos = False
    for i in range(1, len(df)):
        c = df["close"].iloc[i]
        bbl = df["bb_lower"].iloc[i]
        bbm = df["bb_mid"].iloc[i]
        if pd.isna(bbl) or pd.isna(bbm):
            continue
        if not in_pos and c <= bbl:
            in_pos = True
        elif in_pos and c >= bbm:
            in_pos = False
        pos.iloc[i] = 1.0 if in_pos else 0.0
    return pos


def strat_donchian_breakout(df: pd.DataFrame) -> pd.Series:
    """Long on new 20-bar high breakout, exit on new 20-bar low."""
    pos = pd.Series(0.0, index=df.index)
    in_pos = False
    for i in range(1, len(df)):
        c = df["close"].iloc[i]
        dh = df["don_high"].iloc[i - 1] if i > 0 else np.nan
        dl = df["don_low"].iloc[i - 1] if i > 0 else np.nan
        if pd.isna(dh) or pd.isna(dl):
            continue
        if not in_pos and c > dh:
            in_pos = True
        elif in_pos and c < dl:
            in_pos = False
        pos.iloc[i] = 1.0 if in_pos else 0.0
    return pos


def strat_ema_triple_cross(df: pd.DataFrame) -> pd.Series:
    """Long when EMA8 > EMA21 > EMA55 (full bullish alignment)."""
    cond = (df["ema8"] > df["ema21"]) & (df["ema21"] > df["ema55"])
    return cond.astype(float).fillna(0.0)


def strat_vwap_reversion(df: pd.DataFrame) -> pd.Series:
    """Long when price dips >1% below VWAP, exit when price crosses above VWAP."""
    pos = pd.Series(0.0, index=df.index)
    in_pos = False
    for i in range(1, len(df)):
        c = df["close"].iloc[i]
        vw = df["vwap"].iloc[i]
        if pd.isna(vw):
            continue
        gap_pct = (c - vw) / vw * 100.0
        if not in_pos and gap_pct < -1.0:
            in_pos = True
        elif in_pos and c >= vw:
            in_pos = False
        pos.iloc[i] = 1.0 if in_pos else 0.0
    return pos


def strat_stochastic_oversold(df: pd.DataFrame) -> pd.Series:
    """Long when Stochastic %K crosses above %D below 20, exit above 80."""
    pos = pd.Series(0.0, index=df.index)
    in_pos = False
    for i in range(1, len(df)):
        k = df["stoch_k"].iloc[i]
        d = df["stoch_d"].iloc[i]
        k_prev = df["stoch_k"].iloc[i - 1]
        if pd.isna(k) or pd.isna(d) or pd.isna(k_prev):
            continue
        if not in_pos and k_prev < d and k > d and k < 25:
            in_pos = True
        elif in_pos and k > 80:
            in_pos = False
        pos.iloc[i] = 1.0 if in_pos else 0.0
    return pos


# ── SHORT STRATEGIES ───────────────────────────────────────

def strat_sma_cross_short(df: pd.DataFrame) -> pd.Series:
    """Short when SMA20 < SMA50."""
    return -((df["sma20"] < df["sma50"]).astype(float)).fillna(0.0)


def strat_rsi_overbought_short(df: pd.DataFrame) -> pd.Series:
    """Short when RSI > 70, cover when RSI < 40."""
    pos = pd.Series(0.0, index=df.index)
    in_pos = False
    for i in range(1, len(df)):
        rsi_val = df["rsi"].iloc[i]
        if pd.isna(rsi_val):
            continue
        if not in_pos and rsi_val > 70:
            in_pos = True
        elif in_pos and rsi_val < 40:
            in_pos = False
        pos.iloc[i] = -1.0 if in_pos else 0.0
    return pos


def strat_momentum_short(df: pd.DataFrame) -> pd.Series:
    """Short when ROC(10) < -1% and RSI > 30."""
    cond = (df["roc10"] < -1.0) & (df["rsi"] > 30) & (df["rsi"] < 70)
    return -(cond.astype(float).fillna(0.0))


# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════

def run_simulation(coin: str = "SOL"):
    print("=" * 65)
    print(f"  {coin}/USD Hyperliquid Trading Simulation")
    print("=" * 65)

    # Fetch fresh data
    df = fetch_hyperliquid_ohlcv(coin, "15m", 5000)

    # Save to disk
    save_path = OHLCV_DIR / f"{coin}_USD_15m_hyperliquid_latest.csv"
    df_save = df.copy()
    df_save["symbol"] = coin
    df_save["interval"] = "15m"
    df_save.to_csv(save_path, index=False)
    print(f"   💾 Saved to {save_path}")

    # Add indicators
    df = add_indicators(df)
    close = df["close"]

    # Buy-and-hold baseline
    bh_ret = (close.iloc[-1] / close.iloc[0] - 1.0) * 100.0
    print(f"\n   📊 Buy & Hold return: {bh_ret:+.2f}%")
    print(f"   📅 Period: {df['timestamp'].iloc[0]} → {df['timestamp'].iloc[-1]}")
    print(f"   💰 {coin} price: ${close.iloc[0]:.4f} → ${close.iloc[-1]:.4f}")

    # Run strategies
    strategies = [
        # ── long-only ──
        ("SMA Cross 20/50", strat_sma_cross),
        ("RSI Reversion", strat_rsi_reversion),
        ("Momentum ROC10", strat_momentum),
        ("MACD Signal Cross", strat_macd_cross),
        ("HMM Adaptive", strat_hmm_adaptive),
        ("Bollinger Reversion", strat_bollinger_reversion),
        ("Donchian Breakout", strat_donchian_breakout),
        ("EMA Triple 8/21/55", strat_ema_triple_cross),
        ("VWAP Reversion", strat_vwap_reversion),
        ("Stochastic Oversold", strat_stochastic_oversold),
        # ── short-side ──
        ("SMA Cross SHORT", strat_sma_cross_short),
        ("RSI Overbought SHORT", strat_rsi_overbought_short),
        ("Momentum SHORT", strat_momentum_short),
    ]

    results = []
    for name, fn in strategies:
        print(f"\n   ▶ Running: {name}...", end="", flush=True)
        pos = fn(df)
        m = compute_metrics(pos, close, name)
        results.append(m)
        print(f"  {m['total_return_pct']:+.2f}% | WR {m['win_rate_pct']:.0f}% | {m['trades']} trades")

    # Comparison table
    print("\n" + "═" * 90)
    print(f"  STRATEGY COMPARISON — {coin}/USD 15m Hyperliquid")
    print("═" * 90)
    hdr = f"  {'Strategy':<22} {'Return':>10} {'Sharpe':>8} {'MaxDD':>8} {'Trades':>7} {'WinRate':>8} {'InMkt':>7} {'Verdict':>10}"
    print(hdr)
    print("  " + "─" * 86)
    for r in results:
        print(f"  {r['strategy']:<22} {r['total_return_pct']:>9.2f}% {r['sharpe']:>8.2f} "
              f"{r['max_dd_pct']:>7.2f}% {r['trades']:>7} {r['win_rate_pct']:>7.1f}% "
              f"{r['pct_in_market']:>6.1f}% {r['verdict']:>10}")
    print("  " + "─" * 86)
    print(f"  {'Buy & Hold':<22} {bh_ret:>9.2f}%")
    print("═" * 90)

    # Save results
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    result_json = {
        "test": f"{coin.lower()}_hyperliquid_simulation",
        "coin": coin,
        "timestamp": ts,
        "data_bars": len(df),
        "data_period": [str(df["timestamp"].iloc[0]), str(df["timestamp"].iloc[-1])],
        "buy_and_hold_pct": round(bh_ret, 2),
        "strategies": results,
    }
    out_path = RESULTS_DIR / f"{coin.lower()}_simulation_{ts}.json"
    out_path.write_text(json.dumps(result_json, indent=2))
    print(f"\n  💾 Results saved → {out_path}\n")

    return result_json


if __name__ == "__main__":
    import sys
    coin = sys.argv[1].upper() if len(sys.argv) > 1 else "SOL"
    run_simulation(coin)

