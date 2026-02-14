from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import termios
import threading
import time
import tty
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.backtest_runner import (
    parse_backtest_output,
    run_backtest_in_conda,
    save_results,
)


def run_backtest_with_fallback(file_path: str, conda_env: str) -> dict:
    output = run_backtest_in_conda(file_path, conda_env)
    if output.get("success"):
        return output
    stderr = str(output.get("stderr", ""))
    if "EnvironmentLocationNotFound" not in stderr:
        return output

    start = time.time()
    proc = subprocess.run(
        [sys.executable, file_path],
        capture_output=True,
        text=True,
        timeout=300,
    )
    return {
        "success": proc.returncode == 0,
        "return_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "execution_time": time.time() - start,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "runner": "python-fallback",
    }


ANSI = {
    "reset": "\x1b[0m",
    "clear": "\x1b[2J",
    "home": "\x1b[H",
    "alt_on": "\x1b[?1049h",
    "alt_off": "\x1b[?1049l",
    "hide": "\x1b[?25l",
    "show": "\x1b[?25h",
    "green": "\x1b[38;2;0;220;140m",
    "red": "\x1b[38;2;255;90;120m",
    "yellow": "\x1b[38;2;255;200;0m",
    "gray": "\x1b[38;2;140;140;160m",
    "white": "\x1b[38;2;245;245;255m",
    "cyan": "\x1b[38;2;0;212;170m",
    "bold": "\x1b[1m",
}


@dataclass
class BacktestRow:
    path: Path
    strategy: str
    bucket: str


@dataclass
class ResultRow:
    path: Path
    success: bool
    return_code: Optional[int]
    execution_time: Optional[float]
    timestamp: str


def discover_backtests(root: Path) -> List[BacktestRow]:
    candidates = list(
        (root / "src" / "data" / "rbi").glob("**/backtests_final/*_BTFinal.py")
    )
    if not candidates:
        candidates = list((root / "src" / "data" / "rbi").glob("**/backtests/*.py"))
    rows: List[BacktestRow] = []
    for path in sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True):
        strategy = path.stem.replace("_BTFinal", "")
        bucket = path.parent.parent.name
        rows.append(BacktestRow(path=path, strategy=strategy, bucket=bucket))
    return rows


def load_results(root: Path) -> List[ResultRow]:
    rows: List[ResultRow] = []
    results_dir = root / "src" / "data" / "execution_results"
    results_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(
        results_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
    ):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            rows.append(
                ResultRow(
                    path=path,
                    success=bool(payload.get("success", False)),
                    return_code=payload.get("return_code"),
                    execution_time=payload.get("execution_time"),
                    timestamp=str(payload.get("timestamp", "")),
                )
            )
        except Exception:
            rows.append(
                ResultRow(
                    path=path,
                    success=False,
                    return_code=None,
                    execution_time=None,
                    timestamp="",
                )
            )
    return rows


class BacktestsAdminTui:
    def __init__(self, root: Path, conda_env: str) -> None:
        self.root = root
        self.conda_env = conda_env
        self.backtests: List[BacktestRow] = []
        self.results: List[ResultRow] = []
        self.selected = 0
        self.message = ""
        self.running = False
        self.last_run_output: Optional[dict] = None
        self.thread: Optional[threading.Thread] = None

    def refresh(self, update_message: bool = True) -> None:
        self.backtests = discover_backtests(self.root)
        self.results = load_results(self.root)
        if self.selected >= len(self.backtests):
            self.selected = max(len(self.backtests) - 1, 0)
        if update_message:
            self.message = f"updated {time.strftime('%H:%M:%S')}"
        self.render()

    def run_selected(self) -> None:
        if self.running:
            self.message = "a run is already active"
            self.render()
            return
        row = self.backtests[self.selected] if self.backtests else None
        if not row:
            self.message = "no backtest selected"
            self.render()
            return

        self.running = True
        self.message = f"running {row.path.name}..."
        self.render()

        def worker() -> None:
            try:
                output = run_backtest_with_fallback(str(row.path), self.conda_env)
                self.last_run_output = output
                save_results(output, str(row.path))
                parsed = parse_backtest_output(output)
                if output.get("success"):
                    self.message = f"completed {row.path.name} in {output.get('execution_time', 0):.2f}s"
                else:
                    err = parsed.get("error_type") if isinstance(parsed, dict) else None
                    self.message = f"failed {row.path.name}" + (
                        f" ({err})" if err else ""
                    )
            except Exception as exc:
                self.message = f"run error: {exc}"
            finally:
                self.running = False
                self.refresh(update_message=False)

        self.thread = threading.Thread(target=worker, daemon=True)
        self.thread.start()

    def latest_result_preview(self) -> List[str]:
        if not self.results:
            return ["no saved results"]
        latest = self.results[0]
        try:
            payload = json.loads(latest.path.read_text(encoding="utf-8"))
            stdout = str(payload.get("stdout", "")).splitlines()
            stderr = str(payload.get("stderr", "")).splitlines()
            lines: List[str] = []
            lines.append(f"file: {latest.path.name}")
            lines.append(
                f"success: {payload.get('success')} return_code: {payload.get('return_code')}"
            )
            lines.append(f"execution_time: {payload.get('execution_time')}")
            lines.append("stdout:")
            lines.extend(stdout[:4] if stdout else ["(empty)"])
            lines.append("stderr:")
            lines.extend(stderr[:4] if stderr else ["(empty)"])
            return lines
        except Exception as exc:
            return [f"failed reading latest result: {exc}"]

    def render(self) -> None:
        width = max(int(os.get_terminal_size().columns), 100)
        lines: List[str] = []
        lines.append(f"{ANSI['bold']}{ANSI['cyan']}Backtests Admin{ANSI['reset']}")
        lines.append(
            f"{ANSI['gray']}j/k move  r refresh  x run selected  p preview latest result  q quit{ANSI['reset']}"
        )
        lines.append("-")
        lines.append("IDX  STRATEGY                   DATE_BUCKET   FILE")
        lines.append("-" * min(width, 110))
        if not self.backtests:
            lines.append("No backtests found under src/data/rbi/**/backtests_final")
        for idx, row in enumerate(
            self.backtests[: max((os.get_terminal_size().lines - 18), 5)]
        ):
            marker = ">" if idx == self.selected else " "
            lines.append(
                f"{marker} {str(idx + 1).rjust(2)}  {row.strategy[:24].ljust(24)} {row.bucket[:12].ljust(12)} {row.path.name}"
            )
        lines.append("-" * min(width, 110))
        run_status = f"running={self.running} env={self.conda_env}"
        lines.append(run_status)
        lines.append(self.message)
        lines.append("")
        lines.append(
            f"{ANSI['bold']}{ANSI['white']}Latest Result Preview{ANSI['reset']}"
        )
        for preview_line in self.latest_result_preview()[:8]:
            lines.append(preview_line)
        clipped = lines[: max(os.get_terminal_size().lines - 1, 10)]
        sys.stdout.write(f"{ANSI['home']}{ANSI['clear']}" + "\n".join(clipped))
        sys.stdout.flush()

    def start(self) -> None:
        if not sys.stdin.isatty():
            raise RuntimeError("backtests-admin-tui requires interactive TTY")

        sys.stdout.write(f"{ANSI['alt_on']}{ANSI['hide']}{ANSI['clear']}{ANSI['home']}")
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        tty.setraw(fd)
        try:
            self.refresh()
            while True:
                key = os.read(fd, 8).decode("utf-8", errors="ignore")
                if key in ("q", "Q", "\x03"):
                    return
                if key in ("j", "\x1b[B"):
                    self.selected = min(
                        self.selected + 1, max(len(self.backtests) - 1, 0)
                    )
                    self.render()
                    continue
                if key in ("k", "\x1b[A"):
                    self.selected = max(self.selected - 1, 0)
                    self.render()
                    continue
                if key == "r":
                    self.refresh()
                    continue
                if key == "x":
                    self.run_selected()
                    continue
                if key == "p":
                    self.message = "preview refreshed"
                    self.render()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
            sys.stdout.write(f"{ANSI['reset']}{ANSI['show']}{ANSI['alt_off']}")
            sys.stdout.flush()


def run_non_interactive(
    root: Path, conda_env: str, file_path: Optional[str], run_latest: bool
) -> int:
    backtests = discover_backtests(root)
    selected: Optional[Path] = None
    if file_path:
        candidate = Path(file_path).expanduser().resolve()
        if candidate.exists():
            selected = candidate
    elif run_latest and backtests:
        selected = backtests[0].path

    if not selected:
        print("No backtest file selected. Use --file or --run-latest.")
        return 1

    print(f"Running backtest: {selected}")
    output = run_backtest_with_fallback(str(selected), conda_env)
    results_file = save_results(output, str(selected))
    parsed = parse_backtest_output(output)
    print(
        json.dumps(
            {
                "results_file": str(results_file),
                "success": output.get("success"),
                "parsed": parsed,
            },
            indent=2,
        )
    )
    return 0 if output.get("success") else 2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtests admin TUI")
    parser.add_argument("--conda-env", default="tflow")
    parser.add_argument("--run-latest", action="store_true")
    parser.add_argument("--file", default=None)
    parser.add_argument("--no-tui", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.no_tui:
        raise SystemExit(
            run_non_interactive(ROOT, args.conda_env, args.file, args.run_latest)
        )
    app = BacktestsAdminTui(ROOT, args.conda_env)
    app.start()


if __name__ == "__main__":
    main()
