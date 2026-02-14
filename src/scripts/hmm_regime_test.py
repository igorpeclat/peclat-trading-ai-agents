from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd


@dataclass
class Metrics:
    symbol: str
    timeframe: str
    bars: int
    total_return_pct: float
    annualized_return_pct: float
    sharpe: float
    max_drawdown_pct: float
    trades: int
    win_rate_pct: float
    bull_state: int
    bear_state: int


class GaussianHMM2:
    def __init__(self, max_iter: int = 80, tol: float = 1e-5) -> None:
        self.max_iter = max_iter
        self.tol = tol
        self.pi = np.array([0.5, 0.5], dtype=float)
        self.A = np.array([[0.95, 0.05], [0.05, 0.95]], dtype=float)
        self.mu = np.array([-0.001, 0.001], dtype=float)
        self.var = np.array([1e-4, 1e-4], dtype=float)

    @staticmethod
    def _gauss_prob(x: np.ndarray, mu: np.ndarray, var: np.ndarray) -> np.ndarray:
        v = np.maximum(var, 1e-10)
        coeff = 1.0 / np.sqrt(2.0 * np.pi * v)
        expv = np.exp(-0.5 * ((x[:, None] - mu[None, :]) ** 2) / v[None, :])
        return np.maximum(coeff[None, :] * expv, 1e-300)

    def _forward_backward(
        self, x: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
        T = len(x)
        B = self._gauss_prob(x, self.mu, self.var)

        alpha = np.zeros((T, 2), dtype=float)
        beta = np.zeros((T, 2), dtype=float)
        c = np.zeros(T, dtype=float)

        alpha[0] = self.pi * B[0]
        c[0] = np.maximum(alpha[0].sum(), 1e-300)
        alpha[0] /= c[0]

        for t in range(1, T):
            alpha[t] = (alpha[t - 1] @ self.A) * B[t]
            c[t] = np.maximum(alpha[t].sum(), 1e-300)
            alpha[t] /= c[t]

        beta[T - 1] = 1.0
        for t in range(T - 2, -1, -1):
            beta[t] = (self.A @ (B[t + 1] * beta[t + 1])) / np.maximum(c[t + 1], 1e-300)

        gamma = alpha * beta
        gamma /= np.maximum(gamma.sum(axis=1, keepdims=True), 1e-300)

        xi = np.zeros((T - 1, 2, 2), dtype=float)
        for t in range(T - 1):
            denom = np.maximum(
                (alpha[t][:, None] * self.A * (B[t + 1] * beta[t + 1])[None, :]).sum(),
                1e-300,
            )
            xi[t] = (
                alpha[t][:, None] * self.A * (B[t + 1] * beta[t + 1])[None, :]
            ) / denom

        loglik = float(np.sum(np.log(np.maximum(c, 1e-300))))
        return gamma, xi, B, loglik

    def fit(self, x: np.ndarray) -> tuple[np.ndarray, float]:
        x = np.asarray(x, dtype=float)
        x = x[np.isfinite(x)]
        if len(x) < 200:
            raise ValueError("Not enough samples for HMM fit")

        q25, q75 = np.quantile(x, [0.25, 0.75])
        self.mu = np.array([q25, q75], dtype=float)
        base_var = np.var(x) if np.var(x) > 1e-8 else 1e-4
        self.var = np.array([base_var, base_var], dtype=float)

        prev = -np.inf
        gamma = np.zeros((len(x), 2), dtype=float)
        for _ in range(self.max_iter):
            gamma, xi, _, ll = self._forward_backward(x)

            self.pi = gamma[0]
            self.pi /= np.maximum(self.pi.sum(), 1e-300)

            xi_sum = xi.sum(axis=0)
            gamma_sum_t = np.maximum(gamma[:-1].sum(axis=0), 1e-300)
            self.A = xi_sum / gamma_sum_t[:, None]
            self.A /= np.maximum(self.A.sum(axis=1, keepdims=True), 1e-300)

            w = np.maximum(gamma.sum(axis=0), 1e-300)
            self.mu = (gamma * x[:, None]).sum(axis=0) / w
            self.var = (
                (gamma * (x[:, None] - self.mu[None, :]) ** 2).sum(axis=0) / w
            ).clip(1e-8)

            if abs(ll - prev) < self.tol:
                break
            prev = ll

        return gamma, prev


def max_drawdown_pct(equity: pd.Series) -> float:
    peak = equity.cummax()
    dd = equity / peak - 1.0
    return float(abs(dd.min()) * 100.0)


def annualized_sharpe(rets: pd.Series, bars_per_year: int) -> float:
    std = float(rets.std())
    if std <= 0:
        return 0.0
    return float((rets.mean() / std) * np.sqrt(bars_per_year))


def annualize_return(total_return: float, bars: int, bars_per_year: int) -> float:
    years = bars / bars_per_year
    if years <= 0:
        return 0.0
    return float((1.0 + total_return) ** (1.0 / years) - 1.0)


def estimate_trade_stats(pos: pd.Series, rets: pd.Series) -> tuple[int, float]:
    changes = pos.diff().abs().fillna(pos.iloc[0])
    cuts = list(np.where(changes.values > 0)[0])
    if len(cuts) < 2:
        return int((changes > 0).sum()), 0.0
    wins = 0
    total = 0
    for i in range(len(cuts) - 1):
        pnl = float(rets.iloc[cuts[i] : cuts[i + 1]].sum())
        total += 1
        if pnl > 0:
            wins += 1
    win_rate = (wins / total) * 100.0 if total else 0.0
    return int((changes > 0).sum()), float(win_rate)


def run_hmm_test(
    path: Path, symbol: str, timeframe: str
) -> tuple[Metrics, pd.DataFrame]:
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values("timestamp").reset_index(drop=True)
    close = df["close"].astype(float)

    logret = np.log(close / close.shift(1)).fillna(0.0)
    hmm = GaussianHMM2(max_iter=120, tol=1e-6)
    gamma, _ = hmm.fit(logret.values)

    bull_state = int(np.argmax(hmm.mu))
    bear_state = 1 - bull_state
    state = np.argmax(gamma, axis=1)

    sma_fast = close.rolling(30).mean()
    sma_slow = close.rolling(50).mean()

    regime_long = pd.Series((state == bull_state).astype(float))
    trend_long = (sma_fast > sma_slow).astype(float)
    pos = (regime_long * trend_long).fillna(0.0)

    raw_ret = close.pct_change().fillna(0.0)
    strategy_ret = pos.shift(1).fillna(0.0) * raw_ret
    fee = 0.0008
    slip = 0.0003
    trade_change = pos.diff().abs().fillna(pos.iloc[0])
    strategy_ret = strategy_ret - trade_change * (fee + slip)

    equity = 10_000.0 * (1.0 + strategy_ret).cumprod()
    total_return = float(equity.iloc[-1] / 10_000.0 - 1.0)

    bars_per_year = 24 * 365 if timeframe == "1h" else (60 // 15) * 24 * 365
    trades, win_rate = estimate_trade_stats(pos, strategy_ret)

    metrics = Metrics(
        symbol=symbol,
        timeframe=timeframe,
        bars=int(len(df)),
        total_return_pct=total_return * 100.0,
        annualized_return_pct=annualize_return(total_return, len(df), bars_per_year)
        * 100.0,
        sharpe=annualized_sharpe(strategy_ret, bars_per_year),
        max_drawdown_pct=max_drawdown_pct(equity),
        trades=trades,
        win_rate_pct=win_rate,
        bull_state=bull_state,
        bear_state=bear_state,
    )

    out = df.copy()
    out["log_return"] = logret
    out["state"] = state
    out["prob_bear"] = gamma[:, bear_state]
    out["prob_bull"] = gamma[:, bull_state]
    out["sma_fast"] = sma_fast
    out["sma_slow"] = sma_slow
    out["position"] = pos
    out["strategy_ret"] = strategy_ret
    out["equity"] = equity
    return metrics, out


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Hidden Markov regime test on Hyperliquid OHLCV"
    )
    p.add_argument("--symbol", default="BTC", choices=["BTC", "SOL"])
    p.add_argument("--timeframe", default="15m", choices=["15m", "1h"])
    return p.parse_args()


def main() -> None:
    args = parse_args()
    data_path = Path(
        f"src/data/ohlcv/hyperliquid/{args.symbol}_USD_{args.timeframe}_hyperliquid.csv"
    )
    if not data_path.exists():
        raise FileNotFoundError(str(data_path))

    metrics, frame = run_hmm_test(data_path, args.symbol, args.timeframe)

    out_dir = Path("src/data/execution_results")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    trace_path = out_dir / f"hmm_trace_{args.symbol}_{args.timeframe}_{stamp}.csv"
    metrics_path = out_dir / f"hmm_metrics_{args.symbol}_{args.timeframe}_{stamp}.json"

    frame.to_csv(trace_path, index=False)
    metrics_path.write_text(json.dumps(metrics.__dict__, indent=2), encoding="utf-8")

    print(json.dumps(metrics.__dict__, indent=2))
    print(f"trace_file={trace_path}")
    print(f"metrics_file={metrics_path}")


if __name__ == "__main__":
    main()
