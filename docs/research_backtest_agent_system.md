# Research Backtest Agent System

## Quick Sources For Current Tasks

- `src/agents/research_backtest_agent.py`: new research/backtest/investigation loop agent
- `src/main.py`: current orchestrator loop and agent enable/disable map
- `src/config.py`: shared runtime settings (exchange, risk, cadence, AI model)
- `src/exchange_manager.py`: Solana/Hyperliquid abstraction boundary
- `src/nice_funcs_hyperliquid.py`: order execution and position utilities
- `src/nice_funcs_hl.py`: OHLCV and market/funding data retrieval
- `src/agents/research_agent.py`: idea generation patterns and content loop
- `src/agents/rbi_batch_backtester.py`: batch backtest generation and auto-debug flow
- `src/agents/backtest_runner.py`: isolated execution runner and result capture
- `src/strategies/base_strategy.py`: reusable strategy contract

## New Agent Entry Commands

```bash
python src/agents/research_backtest_agent.py --mode research --symbols BTC ETH SOL --timeframes 15m 1h --max-candidates 24
python src/agents/research_backtest_agent.py --mode full --symbols BTC ETH SOL --timeframes 15m 1h --bars 1500
```

Outputs:

- `src/data/research_backtest_agent/run_registry.json`
- `src/data/research_backtest_agent/latest_ideas.jsonl`
- `src/data/research_backtest_agent/runs/<timestamp>/ideas.jsonl`
- `src/data/research_backtest_agent/runs/<timestamp>/results.jsonl`
- `src/data/research_backtest_agent/runs/<timestamp>/investigation.md`

## Prioritized Future Task Queue

### High Impact / Low-Medium Effort

1. Add walk-forward splits (in-sample/out-of-sample) to `research_backtest_agent.py`
2. Add per-trade logs and equity curve export CSV for investigations
3. Add funding-rate penalty path (perp realism) using Hyperliquid funding endpoints
4. Add configurable maker/taker mode and fee model profiles
5. Add run resume mode using latest run directory and explicit run ID
6. Add top-N promoter mode to auto-rerun best candidates with tighter assumptions

### High Impact / Medium Effort

7. Add signal registry in `src/strategies/` and move strategy logic out of agent file
8. Add robust slippage model using `l2Book` depth snapshots
9. Add parameter sweep engine and compare surface metrics (Sharpe, return, drawdown)
10. Add experiment manifest (`json`) for reproducibility
11. Add result ranking API endpoint or CLI query command
12. Add failure taxonomy file for invalid/noisy strategies

### Medium Impact / Low Effort

13. Wire `research_backtest_agent.py` into `src/main.py` toggle map
14. Add config bridge from `src/config.py` to agent CLI defaults
15. Add unified symbols source via `config.get_active_tokens()`
16. Add markdown summary table generation for each run
17. Add "quick sanity" mode (`bars=400`, small candidate set)
18. Add command wrappers in README/docs for common workflows

### Medium Impact / Medium-High Effort

19. Refactor data fetch path to shared adapter (`exchange_manager.py` + hyperliquid data)
20. Add persistent data cache to avoid repeated OHLCV pulls across runs
21. Add robust error/retry policy for API throttling and transient failures
22. Add Monte Carlo perturbation checks for strategy robustness
23. Add "investigation agent" style textual diagnostics with model factory integration
24. Add scheduled run support (daily/4h windows) with run registry controls

## Suggested Execution Order (1-2 Weeks)

- Days 1-2: tasks 1, 2, 4, 13, 14
- Days 3-4: tasks 3, 8, 9
- Days 5-6: tasks 10, 11, 16, 17
- Days 7-10: tasks 7, 19, 20, 24
- Days 11-14: tasks 12, 21, 22, 23
