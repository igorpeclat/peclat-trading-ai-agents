from __future__ import annotations

import argparse
import json
import math
import traceback
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np

from src.agents.base_agent import BaseAgent
from src.nice_funcs_hl import get_data


TIMEFRAME_BARS_PER_YEAR: Dict[str, int] = {
    "1m": 60 * 24 * 365,
    "3m": (60 // 3) * 24 * 365,
    "5m": (60 // 5) * 24 * 365,
    "15m": (60 // 15) * 24 * 365,
    "30m": (60 // 30) * 24 * 365,
    "1h": 24 * 365,
    "2h": (24 // 2) * 365,
    "4h": (24 // 4) * 365,
    "8h": (24 // 8) * 365,
    "12h": (24 // 12) * 365,
    "1d": 365,
}


@dataclass
class StrategyIdea:
    idea_id: str
    symbol: str
    timeframe: str
    strategy: str
    params: Dict[str, Any]
    hypothesis: str
    created_at: str


@dataclass
class BacktestResult:
    idea_id: str
    symbol: str
    timeframe: str
    strategy: str
    params: Dict[str, Any]
    bars: int
    trades: int
    win_rate: float
    total_return_pct: float
    annualized_return_pct: float
    sharpe: float
    max_drawdown_pct: float
    ending_equity: float
    created_at: str


class RunRegistry:
    def __init__(self, registry_path: Path) -> None:
        self.registry_path = registry_path
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> Dict[str, Any]:
        if not self.registry_path.exists():
            return {"runs": []}
        try:
            return json.loads(self.registry_path.read_text(encoding="utf-8"))
        except Exception:
            return {"runs": []}

    def _write(self, payload: Dict[str, Any]) -> None:
        self.registry_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def start_run(self, mode: str, config: Dict[str, Any], run_dir: Path) -> str:
        run_id = f"run_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
        payload = self._read()
        payload["runs"].append(
            {
                "run_id": run_id,
                "status": "running",
                "mode": mode,
                "started_at": datetime.utcnow().isoformat(),
                "ended_at": None,
                "run_dir": str(run_dir),
                "config": config,
                "summary": None,
            }
        )
        self._write(payload)
        return run_id

    def finish_run(self, run_id: str, ok: bool, summary: Dict[str, Any]) -> None:
        payload = self._read()
        for run in payload.get("runs", []):
            if run.get("run_id") == run_id:
                run["status"] = "completed" if ok else "failed"
                run["ended_at"] = datetime.utcnow().isoformat()
                run["summary"] = summary
                break
        self._write(payload)


class ResearchBacktestAgent(BaseAgent):
    def __init__(
        self,
        symbols: List[str],
        timeframes: List[str],
        bars: int,
        starting_capital: float,
        fee_bps: float,
        slippage_bps: float,
        max_candidates: int,
        mode: str = "full",
    ) -> None:
        super().__init__("research_backtest", use_exchange_manager=False)
        self.symbols = symbols
        self.timeframes = timeframes
        self.bars = bars
        self.starting_capital = starting_capital
        self.fee_bps = fee_bps
        self.slippage_bps = slippage_bps
        self.max_candidates = max_candidates
        self.mode = mode

        root = Path(__file__).resolve().parents[1]
        self.data_root = root / "data" / "research_backtest_agent"
        self.runs_dir = self.data_root / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

        self.latest_ideas_file = self.data_root / "latest_ideas.jsonl"
        self.registry = RunRegistry(self.data_root / "run_registry.json")

    def run(self) -> Dict[str, Any]:
        mode = self.mode
        run_dir = self.runs_dir / datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        run_dir.mkdir(parents=True, exist_ok=True)

        cfg = {
            "symbols": self.symbols,
            "timeframes": self.timeframes,
            "bars": self.bars,
            "starting_capital": self.starting_capital,
            "fee_bps": self.fee_bps,
            "slippage_bps": self.slippage_bps,
            "max_candidates": self.max_candidates,
        }
        run_id = self.registry.start_run(mode=mode, config=cfg, run_dir=run_dir)
        print(f"[research-backtest] started {run_id} mode={mode}")

        ideas: List[StrategyIdea] = []
        results: List[BacktestResult] = []
        report_path: Optional[Path] = None

        try:
            if mode in ("research", "full"):
                ideas = self.research(run_dir)
            elif self.latest_ideas_file.exists():
                ideas = self._load_ideas(self.latest_ideas_file)

            if mode in ("backtest", "full"):
                if not ideas:
                    raise RuntimeError("No ideas available. Run research stage first.")
                results = self.backtest(ideas=ideas, run_dir=run_dir)
            else:
                results = self._load_results(run_dir)

            if mode in ("investigate", "full"):
                if not results:
                    if ideas:
                        results = self.backtest(ideas=ideas, run_dir=run_dir)
                    else:
                        raise RuntimeError("No results available for investigation.")
                report_path = self.investigate(results=results, run_dir=run_dir)

            summary = {
                "ideas": len(ideas),
                "results": len(results),
                "report": str(report_path) if report_path else None,
                "run_dir": str(run_dir),
            }
            self.registry.finish_run(run_id, ok=True, summary=summary)
            print(f"[research-backtest] completed {run_id}")
            return summary
        except Exception as exc:
            summary = {
                "error": str(exc),
                "traceback": traceback.format_exc(),
                "run_dir": str(run_dir),
            }
            self.registry.finish_run(run_id, ok=False, summary=summary)
            print(f"[research-backtest] failed {run_id}: {exc}")
            raise

    def research(self, run_dir: Path) -> List[StrategyIdea]:
        ideas: List[StrategyIdea] = []
        counter = 0

        for symbol in self.symbols:
            for timeframe in self.timeframes:
                rsi_variants = [
                    {"lower": 28, "upper": 65, "confirm_sma": 20},
                    {"lower": 30, "upper": 70, "confirm_sma": 20},
                    {"lower": 25, "upper": 68, "confirm_sma": 50},
                ]
                trend_variants = [
                    {"fast": 20, "slow": 50},
                    {"fast": 30, "slow": 100},
                    {"fast": 50, "slow": 200},
                ]

                for params in rsi_variants:
                    if len(ideas) >= self.max_candidates:
                        break
                    counter += 1
                    ideas.append(
                        StrategyIdea(
                            idea_id=f"idea_{counter:04d}",
                            symbol=symbol,
                            timeframe=timeframe,
                            strategy="rsi_mean_reversion",
                            params=params,
                            hypothesis=(
                                "Mean-reversion entries after oversold RSI with trend confirmation "
                                "can capture short-term rebounds while avoiding weak regimes."
                            ),
                            created_at=datetime.utcnow().isoformat(),
                        )
                    )

                for params in trend_variants:
                    if len(ideas) >= self.max_candidates:
                        break
                    counter += 1
                    ideas.append(
                        StrategyIdea(
                            idea_id=f"idea_{counter:04d}",
                            symbol=symbol,
                            timeframe=timeframe,
                            strategy="sma_trend_follow",
                            params=params,
                            hypothesis=(
                                "Trend-following with fast/slow SMA alignment can reduce false entries "
                                "and improve time-in-trend capture."
                            ),
                            created_at=datetime.utcnow().isoformat(),
                        )
                    )

        ideas_file = run_dir / "ideas.jsonl"
        self._write_ideas(ideas, ideas_file)
        self._write_ideas(ideas, self.latest_ideas_file)
        print(f"[research-backtest] generated {len(ideas)} ideas")
        return ideas

    def backtest(
        self, ideas: List[StrategyIdea], run_dir: Path
    ) -> List[BacktestResult]:
        data_cache: Dict[Tuple[str, str], Any] = {}
        results: List[BacktestResult] = []

        for idea in ideas:
            key = (idea.symbol, idea.timeframe)
            if key not in data_cache:
                print(
                    f"[research-backtest] fetching data {idea.symbol} {idea.timeframe}"
                )
                df = get_data(
                    symbol=idea.symbol,
                    timeframe=idea.timeframe,
                    bars=self.bars,
                    add_indicators=True,
                )
                if df is None or df.empty:
                    print(
                        f"[research-backtest] no data for {idea.symbol} {idea.timeframe}"
                    )
                    continue
                df = df.copy().sort_values("timestamp").reset_index(drop=True)
                data_cache[key] = df

            df = data_cache[key]
            signal = self._build_signal(
                df=df, strategy=idea.strategy, params=idea.params
            )
            result = self._simulate(
                idea=idea,
                df=df,
                signal=signal,
                starting_capital=self.starting_capital,
                fee_bps=self.fee_bps,
                slippage_bps=self.slippage_bps,
            )
            if result:
                results.append(result)

        self._write_results(results, run_dir / "results.jsonl")
        print(f"[research-backtest] produced {len(results)} backtest results")
        return results

    def investigate(self, results: List[BacktestResult], run_dir: Path) -> Path:
        ordered = sorted(
            results,
            key=lambda x: (x.sharpe, x.total_return_pct, -x.max_drawdown_pct),
            reverse=True,
        )

        top = ordered[: min(10, len(ordered))]
        weak = [r for r in ordered if r.total_return_pct < 0][: min(10, len(ordered))]

        lines: List[str] = []
        lines.append("# Research Backtest Investigation")
        lines.append("")
        lines.append(f"Generated at: {datetime.utcnow().isoformat()}")
        lines.append(f"Total runs: {len(results)}")
        lines.append("")
        lines.append("## Top Candidates")
        lines.append("")
        for idx, r in enumerate(top, 1):
            lines.append(
                (
                    f"{idx}. {r.idea_id} | {r.symbol} {r.timeframe} | {r.strategy} "
                    f"| return={r.total_return_pct:.2f}% | sharpe={r.sharpe:.2f} "
                    f"| mdd={r.max_drawdown_pct:.2f}% | trades={r.trades}"
                )
            )

        lines.append("")
        lines.append("## Weak Candidates")
        lines.append("")
        for idx, r in enumerate(weak, 1):
            lines.append(
                (
                    f"{idx}. {r.idea_id} | {r.symbol} {r.timeframe} | {r.strategy} "
                    f"| return={r.total_return_pct:.2f}% | sharpe={r.sharpe:.2f} "
                    f"| mdd={r.max_drawdown_pct:.2f}% | trades={r.trades}"
                )
            )

        lines.append("")
        lines.append("## Observations")
        lines.append("")
        if top:
            avg_sharpe_top = float(np.mean([t.sharpe for t in top]))
            avg_dd_top = float(np.mean([t.max_drawdown_pct for t in top]))
            lines.append(
                f"- Top bucket avg sharpe: {avg_sharpe_top:.2f}, avg max drawdown: {avg_dd_top:.2f}%"
            )
        if weak:
            overtraded = sum(1 for w in weak if w.trades > 80)
            lines.append(f"- Weak bucket over-trading count (>80 trades): {overtraded}")
        lines.append(
            "- Next step: rerun top candidates with tighter slippage assumptions and out-of-sample windows."
        )

        report_path = run_dir / "investigation.md"
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"[research-backtest] wrote investigation report -> {report_path}")
        return report_path

    def _build_signal(self, df: Any, strategy: str, params: Dict[str, Any]) -> Any:
        close = df["close"].astype(float)

        if strategy == "rsi_mean_reversion":
            lower = float(params.get("lower", 30))
            upper = float(params.get("upper", 70))
            sma_len = int(params.get("confirm_sma", 20))

            rsi = df["rsi"] if "rsi" in df.columns else self._compute_rsi(close, 14)
            sma = close.rolling(sma_len).mean()

            long_entry = (rsi < lower) & (close > sma)
            long_exit = rsi > upper

            return self._stateful_position(long_entry=long_entry, long_exit=long_exit)

        if strategy == "sma_trend_follow":
            fast = int(params.get("fast", 20))
            slow = int(params.get("slow", 50))
            fast_ma = close.rolling(fast).mean()
            slow_ma = close.rolling(slow).mean()

            long_entry = (fast_ma > slow_ma) & (close > slow_ma)
            long_exit = fast_ma < slow_ma

            return self._stateful_position(long_entry=long_entry, long_exit=long_exit)

        raise ValueError(f"Unknown strategy: {strategy}")

    def _simulate(
        self,
        idea: StrategyIdea,
        df: Any,
        signal: Any,
        starting_capital: float,
        fee_bps: float,
        slippage_bps: float,
    ) -> Optional[BacktestResult]:
        if len(df) < 100 or signal.empty:
            return None

        close = df["close"].astype(float)
        returns = close.pct_change().fillna(0.0)
        position = signal.astype(float).clip(0.0, 1.0)
        position_prev = position.shift(1).fillna(0.0)

        trade_changes = position.diff().abs().fillna(position.iloc[0])
        cost_rate = (fee_bps + slippage_bps) / 10000.0

        gross_ret = position_prev * returns
        net_ret = gross_ret - (trade_changes * cost_rate)
        equity_curve = starting_capital * (1.0 + net_ret).cumprod()

        total_return = (equity_curve.iloc[-1] / starting_capital) - 1.0
        max_dd = self._max_drawdown_pct(equity_curve)

        bars_per_year = TIMEFRAME_BARS_PER_YEAR.get(idea.timeframe, 24 * 365)
        annualized_return = self._annualize_return(
            total_return, len(net_ret), bars_per_year
        )
        sharpe = self._annualized_sharpe(net_ret, bars_per_year)

        trades = int((trade_changes > 0).sum())
        win_rate = self._estimate_win_rate(net_ret=net_ret, trade_changes=trade_changes)

        return BacktestResult(
            idea_id=idea.idea_id,
            symbol=idea.symbol,
            timeframe=idea.timeframe,
            strategy=idea.strategy,
            params=idea.params,
            bars=int(len(df)),
            trades=trades,
            win_rate=win_rate,
            total_return_pct=total_return * 100.0,
            annualized_return_pct=annualized_return * 100.0,
            sharpe=sharpe,
            max_drawdown_pct=max_dd,
            ending_equity=float(equity_curve.iloc[-1]),
            created_at=datetime.utcnow().isoformat(),
        )

    @staticmethod
    def _stateful_position(long_entry: Any, long_exit: Any) -> Any:
        pos = 0.0
        out: List[float] = []
        for e, x in zip(long_entry.fillna(False), long_exit.fillna(False)):
            if pos == 0.0 and bool(e):
                pos = 1.0
            elif pos == 1.0 and bool(x):
                pos = 0.0
            out.append(pos)
        return long_entry.__class__(out, index=long_entry.index)

    @staticmethod
    def _compute_rsi(close: Any, period: int) -> Any:
        delta = close.diff()
        up = delta.clip(lower=0.0)
        down = -delta.clip(upper=0.0)
        avg_up = up.ewm(alpha=1 / period, adjust=False).mean()
        avg_down = down.ewm(alpha=1 / period, adjust=False).mean()
        rs = avg_up / avg_down.replace(0.0, np.nan)
        return (100 - (100 / (1 + rs))).fillna(50.0)

    @staticmethod
    def _annualized_sharpe(returns: Any, bars_per_year: int) -> float:
        std = float(returns.std())
        if std <= 0:
            return 0.0
        mean = float(returns.mean())
        return (mean / std) * math.sqrt(bars_per_year)

    @staticmethod
    def _max_drawdown_pct(equity_curve: Any) -> float:
        running_max = equity_curve.cummax()
        drawdown = (equity_curve / running_max) - 1.0
        return float(abs(drawdown.min()) * 100.0)

    @staticmethod
    def _annualize_return(total_return: float, bars: int, bars_per_year: int) -> float:
        if bars <= 1:
            return 0.0
        years = bars / bars_per_year
        if years <= 0:
            return 0.0
        return (1.0 + total_return) ** (1.0 / years) - 1.0

    @staticmethod
    def _estimate_win_rate(net_ret: Any, trade_changes: Any) -> float:
        trade_boundaries = list(np.where(trade_changes.values > 0)[0])
        if len(trade_boundaries) < 2:
            return 0.0

        wins = 0
        total = 0
        for i in range(len(trade_boundaries) - 1):
            start = trade_boundaries[i]
            end = trade_boundaries[i + 1]
            pnl = float(net_ret.iloc[start:end].sum())
            total += 1
            if pnl > 0:
                wins += 1
        return (wins / total) * 100.0 if total else 0.0

    @staticmethod
    def _write_ideas(ideas: Iterable[StrategyIdea], path: Path) -> None:
        with path.open("w", encoding="utf-8") as f:
            for idea in ideas:
                f.write(json.dumps(asdict(idea)) + "\n")

    @staticmethod
    def _load_ideas(path: Path) -> List[StrategyIdea]:
        out: List[StrategyIdea] = []
        if not path.exists():
            return out
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            out.append(StrategyIdea(**json.loads(line)))
        return out

    @staticmethod
    def _write_results(results: Iterable[BacktestResult], path: Path) -> None:
        with path.open("w", encoding="utf-8") as f:
            for result in results:
                f.write(json.dumps(asdict(result)) + "\n")

    @staticmethod
    def _load_results(run_dir: Path) -> List[BacktestResult]:
        path = run_dir / "results.jsonl"
        out: List[BacktestResult] = []
        if not path.exists():
            return out
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            out.append(BacktestResult(**json.loads(line)))
        return out


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Research + Backtest + Investigation agent"
    )
    parser.add_argument(
        "--mode",
        choices=["research", "backtest", "investigate", "full"],
        default="full",
        help="Stage to run",
    )
    parser.add_argument("--symbols", nargs="+", default=["BTC", "ETH", "SOL"])
    parser.add_argument("--timeframes", nargs="+", default=["15m", "1h"])
    parser.add_argument("--bars", type=int, default=1500)
    parser.add_argument("--starting-capital", type=float, default=10_000.0)
    parser.add_argument("--fee-bps", type=float, default=4.5)
    parser.add_argument("--slippage-bps", type=float, default=3.0)
    parser.add_argument("--max-candidates", type=int, default=24)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    agent = ResearchBacktestAgent(
        symbols=args.symbols,
        timeframes=args.timeframes,
        bars=args.bars,
        starting_capital=args.starting_capital,
        fee_bps=args.fee_bps,
        slippage_bps=args.slippage_bps,
        max_candidates=args.max_candidates,
        mode=args.mode,
    )
    summary = agent.run()
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
