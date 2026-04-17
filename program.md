Goal: Maximize Total PnL and Sharpe Ratio of the Polymarket "No" bias strategy using only non-sports historical markets.
Dataset: Use jon-becker/prediction-market-analysis Parquet files (largest clean Polymarket dataset).
Constraints:

Only non-sports markets
Only resolved markets
Minimum market duration as defined in config
Simulate realistic fees and slippage

Tunable parameters to optimize:

no_price_cap (range 0.50 - 0.70)
min_market_days (range 20 - 90)
position_size_pct (range 1.0 - 5.0)
exit_threshold (range 0.80 - 0.95)
max_open_positions_pct (range 20 - 60)

Success criteria: Improve both Total PnL and Sharpe Ratio compared to baseline using walk-forward validation. Never commit changes that only improve in-sample performance.
After every code change, run the full backtest and evaluate metrics.
