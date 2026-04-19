"""Karpathy-style autoresearch optimizer for the nothing-ever-happens strategy.

This orchestrates the loop described in ``program.md``:

* Define a search space for the tunable strategy knobs.
* Sample candidate configs (random seed + perturbation around the current best).
* For every candidate, run the :mod:`backtest` engine with **walk-forward
  validation** — the markets are chronologically split into K folds and we
  evaluate on each fold independently. The first fold is in-sample; we judge a
  config by the average of folds 2..K so in-sample-only wins are ignored.
* Keep a leaderboard of the top N configs ranked by a composite score
  (``total_pnl_usd * sharpe_ratio`` by default; configurable).
* Persist every trial to JSONL under ``artifacts/optimizer/trials.jsonl`` and
  keep the best configs in ``artifacts/optimizer/best_configs.json`` so the
  dashboard can display them.

Usage:

    uv run optimizer.py --iters 200 --data-dir data/polymarket
    uv run optimizer.py --synthetic --iters 50  # offline smoke test

The optimizer is intentionally decoupled from any live code paths — it only
reads data and writes JSON. You can run it on the same box as the bot (it is
cheap) or on a separate machine.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from backtest import (
    BacktestEngine,
    BacktestParams,
    BacktestResult,
    DEFAULT_BLACKLIST,
    generate_synthetic_markets,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Search space (matches the ranges documented in program.md)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParamRange:
    name: str
    low: float
    high: float
    step: float
    is_int: bool = False


SEARCH_SPACE: tuple[ParamRange, ...] = (
    ParamRange("no_price_cap", 0.50, 0.70, 0.01),
    ParamRange("min_market_days", 20, 90, 1, is_int=True),
    ParamRange("position_size_pct", 1.0, 5.0, 0.1),
    ParamRange("exit_threshold", 0.80, 0.95, 0.01),
    ParamRange("max_open_positions_pct", 20.0, 60.0, 1.0),
)


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _round_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return round(round(value / step) * step, 6)


def sample_random_params(rng: random.Random, base: BacktestParams) -> BacktestParams:
    overrides: dict[str, Any] = {}
    for r in SEARCH_SPACE:
        v = rng.uniform(r.low, r.high)
        v = _round_to_step(v, r.step)
        overrides[r.name] = int(round(v)) if r.is_int else float(v)
    return _replace(base, **overrides)


def perturb_params(
    rng: random.Random,
    base: BacktestParams,
    *,
    scale: float = 0.15,
) -> BacktestParams:
    """Gaussian-ish perturbation of each tunable around ``base`` — the
    autoresearch "stay close to what works, but explore" step."""

    overrides: dict[str, Any] = {}
    for r in SEARCH_SPACE:
        center = float(getattr(base, r.name))
        span = (r.high - r.low) * scale
        v = rng.gauss(center, max(span, r.step))
        v = _round_to_step(_clip(v, r.low, r.high), r.step)
        overrides[r.name] = int(round(v)) if r.is_int else float(v)
    return _replace(base, **overrides)


def _replace(params: BacktestParams, **overrides: Any) -> BacktestParams:
    d = asdict(params)
    d.update(overrides)
    # asdict collapses tuples to lists, restore blacklist as tuple of str.
    bl = d.get("categories_blacklist") or DEFAULT_BLACKLIST
    d["categories_blacklist"] = tuple(str(x).lower() for x in bl)
    return BacktestParams(**d)


# ---------------------------------------------------------------------------
# Scoring + trial bookkeeping
# ---------------------------------------------------------------------------


@dataclass
class TrialMetrics:
    total_pnl_usd: float
    sharpe_ratio: float
    win_rate_pct: float
    max_drawdown_pct: float
    trade_count: int
    avg_hold_days: float
    return_pct: float

    @classmethod
    def from_result(cls, result: BacktestResult) -> "TrialMetrics":
        s = result.summary_dict()
        return cls(
            total_pnl_usd=float(s["total_pnl_usd"]),
            sharpe_ratio=float(s["sharpe_ratio"]),
            win_rate_pct=float(s["win_rate_pct"]),
            max_drawdown_pct=float(s["max_drawdown_pct"]),
            trade_count=int(s["trade_count"]),
            avg_hold_days=float(s["avg_hold_days"]),
            return_pct=float(s["return_pct"]),
        )

    def composite_score(self) -> float:
        # Reward PnL and Sharpe jointly while penalising drawdown. Low trade
        # counts get deflated so the search doesn't reward a strategy that
        # fired once and got lucky.
        if self.trade_count < 5:
            return -1e9
        pnl_term = self.total_pnl_usd
        sharpe_term = max(self.sharpe_ratio, 0.0) * 1000.0
        dd_penalty = self.max_drawdown_pct * 25.0
        return pnl_term + sharpe_term - dd_penalty


@dataclass
class Trial:
    trial_id: int
    timestamp_utc: str
    params: dict[str, Any]
    folds: list[dict[str, Any]]
    oos: TrialMetrics
    score: float
    mode: str  # "random" | "perturb"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


def run_trial(
    engine: BacktestEngine,
    params: BacktestParams,
    *,
    walk_forward_splits: int,
) -> tuple[list[BacktestResult], TrialMetrics]:
    folds = engine.walk_forward(params, splits=max(2, walk_forward_splits))
    if len(folds) <= 1:
        # Not enough data to split; fall back to single-run metrics.
        full = engine.run(params)
        return folds or [full], TrialMetrics.from_result(full)

    # In-sample = folds[0], evaluate on remaining folds (out-of-sample).
    oos = folds[1:]
    combined = _combine_results(oos)
    return folds, TrialMetrics.from_result(combined)


def _combine_results(results: Sequence[BacktestResult]) -> BacktestResult:
    if not results:
        return BacktestResult(
            total_pnl_usd=0.0,
            win_rate_pct=0.0,
            sharpe_ratio=0.0,
            max_drawdown_pct=0.0,
            trade_count=0,
            avg_hold_days=0.0,
            final_bankroll_usd=0.0,
            starting_bankroll_usd=0.0,
        )
    trades = [t for r in results for t in r.trades]
    equity = [pt for r in results for pt in r.equity_curve]
    start_bank = results[0].starting_bankroll_usd
    total_pnl = sum(r.total_pnl_usd for r in results)
    wins = sum(1 for t in trades if t.won)
    tc = len(trades)
    avg_hold = (sum(t.hold_days for t in trades) / tc) if tc else 0.0
    win_rate = (wins / tc * 100.0) if tc else 0.0
    # Average per-fold Sharpe is a less-biased OOS estimate than recomputing
    # across concatenated fold returns (which would understate vol).
    sharpe = sum(r.sharpe_ratio for r in results) / len(results)
    max_dd = max((r.max_drawdown_pct for r in results), default=0.0)
    return BacktestResult(
        total_pnl_usd=total_pnl,
        win_rate_pct=win_rate,
        sharpe_ratio=sharpe,
        max_drawdown_pct=max_dd,
        trade_count=tc,
        avg_hold_days=avg_hold,
        final_bankroll_usd=start_bank + total_pnl,
        starting_bankroll_usd=start_bank,
        trades=trades,
        equity_curve=equity,
    )


# ---------------------------------------------------------------------------
# Optimizer loop
# ---------------------------------------------------------------------------


@dataclass
class OptimizerConfig:
    iters: int = 100
    walk_forward_splits: int = 4
    seed: int = 0
    top_n: int = 10
    work_dir: Path = field(default_factory=lambda: Path("artifacts/optimizer"))
    perturb_probability: float = 0.5

    def paths(self) -> dict[str, Path]:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        return {
            "trials": self.work_dir / "trials.jsonl",
            "best": self.work_dir / "best_configs.json",
            "baseline": self.work_dir / "baseline.json",
            "latest": self.work_dir / "latest.json",
        }


class Optimizer:
    def __init__(
        self,
        engine: BacktestEngine,
        baseline: BacktestParams,
        *,
        cfg: OptimizerConfig | None = None,
    ) -> None:
        self.engine = engine
        self.baseline = baseline
        self.cfg = cfg or OptimizerConfig()
        self.rng = random.Random(self.cfg.seed)
        self.leaderboard: list[Trial] = []
        self._paths = self.cfg.paths()

    def run(self) -> list[Trial]:
        logger.info(
            "optimizer_starting",
            extra={"iters": self.cfg.iters, "splits": self.cfg.walk_forward_splits},
        )
        baseline_folds, baseline_metrics = run_trial(
            self.engine,
            self.baseline,
            walk_forward_splits=self.cfg.walk_forward_splits,
        )
        baseline_trial = Trial(
            trial_id=0,
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            params=asdict(self.baseline),
            folds=[r.summary_dict() for r in baseline_folds],
            oos=baseline_metrics,
            score=baseline_metrics.composite_score(),
            mode="baseline",
        )
        self._write_baseline(baseline_trial)
        self._append_trial(baseline_trial)
        self.leaderboard.append(baseline_trial)

        best = baseline_trial
        for i in range(1, self.cfg.iters + 1):
            use_perturb = (
                bool(self.leaderboard)
                and self.rng.random() < self.cfg.perturb_probability
            )
            seed_params = self._params_from_dict(best.params)
            candidate = (
                perturb_params(self.rng, seed_params)
                if use_perturb
                else sample_random_params(self.rng, self.baseline)
            )

            folds, oos = run_trial(
                self.engine,
                candidate,
                walk_forward_splits=self.cfg.walk_forward_splits,
            )
            trial = Trial(
                trial_id=i,
                timestamp_utc=datetime.now(timezone.utc).isoformat(),
                params=asdict(candidate),
                folds=[r.summary_dict() for r in folds],
                oos=oos,
                score=oos.composite_score(),
                mode="perturb" if use_perturb else "random",
            )
            self._append_trial(trial)
            self.leaderboard.append(trial)
            self.leaderboard.sort(key=lambda t: t.score, reverse=True)
            self.leaderboard = self.leaderboard[: self.cfg.top_n]

            if trial.score > best.score and self._is_oos_improvement(trial, baseline_trial):
                best = trial
                logger.info(
                    "optimizer_new_best",
                    extra={
                        "trial": i,
                        "score": round(trial.score, 3),
                        "pnl": trial.oos.total_pnl_usd,
                        "sharpe": trial.oos.sharpe_ratio,
                    },
                )

            if i % 10 == 0:
                logger.info(
                    "optimizer_progress",
                    extra={
                        "trial": i,
                        "iters": self.cfg.iters,
                        "best_score": round(best.score, 3),
                    },
                )

        self._write_best()
        self._write_latest(best)
        return self.leaderboard

    @staticmethod
    def _is_oos_improvement(candidate: Trial, baseline: Trial) -> bool:
        """Enforce program.md's "never commit in-sample-only wins" rule: OOS
        PnL AND OOS Sharpe must both beat baseline."""

        return (
            candidate.oos.total_pnl_usd > baseline.oos.total_pnl_usd
            and candidate.oos.sharpe_ratio >= baseline.oos.sharpe_ratio
        )

    def _params_from_dict(self, d: dict[str, Any]) -> BacktestParams:
        bl = d.get("categories_blacklist") or DEFAULT_BLACKLIST
        payload = dict(d)
        payload["categories_blacklist"] = tuple(str(x).lower() for x in bl)
        return BacktestParams(**payload)

    def _append_trial(self, trial: Trial) -> None:
        with self._paths["trials"].open("a") as f:
            f.write(json.dumps(trial.to_dict(), default=str) + "\n")

    def _write_best(self) -> None:
        payload = [t.to_dict() for t in self.leaderboard]
        with self._paths["best"].open("w") as f:
            json.dump(payload, f, indent=2, default=str)

    def _write_baseline(self, trial: Trial) -> None:
        with self._paths["baseline"].open("w") as f:
            json.dump(trial.to_dict(), f, indent=2, default=str)

    def _write_latest(self, best: Trial) -> None:
        with self._paths["latest"].open("w") as f:
            json.dump(best.to_dict(), f, indent=2, default=str)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_config(path: Path | str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    with p.open() as f:
        return json.load(f)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--data-dir",
        default=os.getenv("POLYMARKET_DATA_DIR", "data/polymarket"),
    )
    parser.add_argument(
        "--config", default="config.json",
        help="Path to config.json (falls back to config.example.json).",
    )
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--synthetic-n", type=int, default=800)
    parser.add_argument("--iters", type=int, default=100)
    parser.add_argument("--walk-forward-splits", type=int, default=4)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument(
        "--work-dir",
        default=os.getenv("OPTIMIZER_WORK_DIR", "artifacts/optimizer"),
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args(argv)
    cfg_dict = _load_config(args.config) or _load_config("config.example.json")
    baseline = BacktestParams.from_config_dict(cfg_dict)

    if args.synthetic:
        engine = BacktestEngine.from_dataframe(
            generate_synthetic_markets(n=args.synthetic_n)
        )
    else:
        engine = BacktestEngine.from_directory(args.data_dir)

    opt_cfg = OptimizerConfig(
        iters=args.iters,
        walk_forward_splits=args.walk_forward_splits,
        seed=args.seed,
        top_n=args.top_n,
        work_dir=Path(args.work_dir),
    )
    opt = Optimizer(engine, baseline, cfg=opt_cfg)
    started = time.time()
    leaderboard = opt.run()
    elapsed = time.time() - started

    summary = {
        "elapsed_sec": round(elapsed, 2),
        "baseline": opt.leaderboard[-1].params if opt.leaderboard else {},
        "top": [
            {
                "trial_id": t.trial_id,
                "score": round(t.score, 3),
                "oos": asdict(t.oos),
                "params": t.params,
                "mode": t.mode,
            }
            for t in leaderboard[:5]
        ],
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
