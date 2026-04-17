"""Smoke tests for backtest.py and optimizer.py on synthetic data."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date

import pandas as pd

from backtest import (
    BacktestEngine,
    BacktestParams,
    DEFAULT_BLACKLIST,
    filter_markets,
    generate_synthetic_markets,
)
from optimizer import (
    Optimizer,
    OptimizerConfig,
    perturb_params,
    sample_random_params,
    SEARCH_SPACE,
    _combine_results,
)


def _baseline_params(**overrides) -> BacktestParams:
    payload = asdict(BacktestParams())
    payload.update(overrides)
    payload["categories_blacklist"] = tuple(payload["categories_blacklist"])
    return BacktestParams(**payload)


def test_generate_synthetic_markets_is_deterministic() -> None:
    a = generate_synthetic_markets(n=50, seed=1)
    b = generate_synthetic_markets(n=50, seed=1)
    pd.testing.assert_frame_equal(a, b)
    assert len(a) == 50
    assert set(a.columns) >= {
        "market_id",
        "title",
        "category",
        "start_date",
        "end_date",
        "resolved",
        "outcome",
        "no_entry_price",
        "no_peak_price",
        "binary",
    }


def test_filter_markets_drops_sports_and_short_duration() -> None:
    df = generate_synthetic_markets(n=300, seed=3)
    out = filter_markets(df, min_market_days=30, categories_blacklist=DEFAULT_BLACKLIST)
    assert not out.empty
    # No blacklisted categories remain (simple substring match mirrors loader).
    for _, row in out.iterrows():
        blob = f"{row['title']} {row['category']}".lower()
        assert not any(b in blob for b in DEFAULT_BLACKLIST)
    duration = (
        pd.to_datetime(out["end_date"]) - pd.to_datetime(out["start_date"])
    ).dt.days
    assert (duration >= 30).all()


def test_backtest_engine_run_produces_metrics() -> None:
    df = generate_synthetic_markets(n=400, seed=5)
    engine = BacktestEngine.from_dataframe(df)
    result = engine.run(_baseline_params())
    summary = result.summary_dict()

    assert summary["trade_count"] > 0
    # The synthetic universe has a ~72% NO-resolution edge for non-sports,
    # so the default strategy should earn money in aggregate.
    assert summary["total_pnl_usd"] > 0
    assert 0.0 <= summary["win_rate_pct"] <= 100.0
    assert summary["max_drawdown_pct"] >= 0.0
    assert summary["final_bankroll_usd"] > result.starting_bankroll_usd


def test_backtest_engine_no_duplicate_per_market() -> None:
    df = generate_synthetic_markets(n=200, seed=9)
    engine = BacktestEngine.from_dataframe(df)
    result = engine.run(_baseline_params())
    ids = [t.market_id for t in result.trades]
    assert len(ids) == len(set(ids))


def test_backtest_respects_daily_trade_cap() -> None:
    # Force many markets closing on the same day and verify we don't exceed
    # max_trade_count_per_day.
    n = 50
    same_day = date(2024, 6, 1)
    rows = []
    for i in range(n):
        rows.append(
            {
                "market_id": f"m-{i}",
                "title": "Will event X happen?",
                "category": "politics",
                "start_date": same_day.replace(day=1),
                "end_date": same_day.replace(day=1).replace(month=10),
                "resolved": True,
                "outcome": "no",
                "no_entry_price": 0.55,
                "no_peak_price": 0.95,
                "binary": True,
            }
        )
    df = pd.DataFrame(rows)
    engine = BacktestEngine.from_dataframe(df)
    result = engine.run(_baseline_params(max_trade_count_per_day=3))
    by_entry_day: dict = {}
    for t in result.trades:
        by_entry_day.setdefault(t.entry_date, 0)
        by_entry_day[t.entry_date] += 1
    assert all(count <= 3 for count in by_entry_day.values())


def test_walk_forward_produces_multiple_folds() -> None:
    df = generate_synthetic_markets(n=300, seed=11)
    engine = BacktestEngine.from_dataframe(df)
    folds = engine.walk_forward(_baseline_params(), splits=4)
    assert len(folds) == 4
    # Combined OOS result is a valid BacktestResult with non-negative counts.
    combined = _combine_results(folds[1:])
    assert combined.trade_count >= 0


def test_optimizer_loop_writes_artifacts(tmp_path) -> None:
    df = generate_synthetic_markets(n=250, seed=13)
    engine = BacktestEngine.from_dataframe(df)
    baseline = _baseline_params()
    cfg = OptimizerConfig(
        iters=5,
        walk_forward_splits=3,
        seed=21,
        top_n=3,
        work_dir=tmp_path / "out",
    )
    opt = Optimizer(engine, baseline, cfg=cfg)
    leaderboard = opt.run()

    assert len(leaderboard) > 0
    assert (tmp_path / "out" / "trials.jsonl").exists()
    assert (tmp_path / "out" / "best_configs.json").exists()
    assert (tmp_path / "out" / "baseline.json").exists()
    assert (tmp_path / "out" / "latest.json").exists()

    trials = (tmp_path / "out" / "trials.jsonl").read_text().splitlines()
    # Baseline + iters
    assert len(trials) == cfg.iters + 1


def test_sample_and_perturb_stay_in_search_space() -> None:
    import random

    rng = random.Random(0)
    base = _baseline_params()
    for _ in range(50):
        p = sample_random_params(rng, base)
        for r in SEARCH_SPACE:
            v = getattr(p, r.name)
            assert r.low - 1e-9 <= v <= r.high + 1e-9

    for _ in range(50):
        p = perturb_params(rng, base, scale=0.25)
        for r in SEARCH_SPACE:
            v = getattr(p, r.name)
            assert r.low - 1e-9 <= v <= r.high + 1e-9


def test_effective_price_cap_and_size_via_config_dict() -> None:
    cfg = {
        "strategies": {
            "nothing_happens": {
                "no_price_cap": 0.55,
                "position_size_pct": 3.5,
                "min_market_days": 45,
                "max_open_positions_pct": 50.0,
                "exit_threshold": 0.9,
                "categories_blacklist": ["sports", "baseball"],
                "max_trade_count_per_day": 2,
            }
        },
        "backtest": {
            "taker_fee_pct": 0.6,
            "slippage_pct": 0.3,
            "starting_bankroll_usd": 5000.0,
            "min_hold_days": 2,
        },
    }
    params = BacktestParams.from_config_dict(cfg)
    assert params.no_price_cap == 0.55
    assert params.position_size_pct == 3.5
    assert params.min_market_days == 45
    assert params.max_open_positions_pct == 50.0
    assert params.exit_threshold == 0.9
    assert params.max_trade_count_per_day == 2
    assert params.categories_blacklist == ("sports", "baseball")
    assert params.taker_fee_pct == 0.6
    assert params.slippage_pct == 0.3
    assert params.starting_bankroll_usd == 5000.0
    assert params.min_hold_days == 2
