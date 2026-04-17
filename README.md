# Nothing Ever Happens Polymarket Bot

Focused async Python bot for Polymarket that buys No on standalone non-sports yes/no markets.

*FOR ENTERTAINMENT ONLY. PROVIDED AS IS, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED. USE AT YOUR OWN RISK. THE AUTHORS ARE NOT LIABLE FOR ANY CLAIMS, LOSSES, OR DAMAGES.*

![Dashboard screenshot](docs/dashboard.jpg)

- `bot/`: runtime, exchange clients, dashboard, recovery, and the `nothing_happens` strategy
- `scripts/`: operational helpers for deployed instances and local inspection
- `tests/`: focused unit and regression coverage

## Runtime

The bot scans standalone markets, looks for NO entries below a configured price cap, tracks open positions, exposes a dashboard, and persists live recovery state when order transmission is enabled.

The runtime is `nothing_happens`.

## Safety Model

Real order transmission requires all three environment variables:

- `BOT_MODE=live`
- `LIVE_TRADING_ENABLED=true`
- `DRY_RUN=false`

If any of those are missing, the bot uses `PaperExchangeClient`.

Additional live-mode requirements:

- `PRIVATE_KEY`
- `FUNDER_ADDRESS` for signature types `1` and `2`
- `DATABASE_URL`
- `POLYGON_RPC_URL` for proxy-wallet approvals and redemption

## Setup

The project is packaged with `pyproject.toml` and managed with
[uv](https://docs.astral.sh/uv/). Install uv once on your machine:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then clone + bootstrap:

```bash
git clone https://github.com/sterlingcrispin/nothing-ever-happens.git
cd nothing-ever-happens
uv venv --python 3.12
uv pip install -e ".[dev]"
cp config.example.json config.json
cp .env.example .env
```

`config.json` is intentionally local and ignored by git.

Legacy `pip install -r requirements.txt` still works for container
environments that don't have uv.

## Configuration

The runtime reads:

- `config.json` for non-secret runtime settings
- `.env` for secrets and runtime flags

The runtime config lives under `strategies.nothing_happens`. See [config.example.json](config.example.json) and [.env.example](.env.example).

You can point the runtime at a different config file with `CONFIG_PATH=/path/to/config.json`.

## Running Locally

```bash
uv run python -m bot.main          # paper mode (default)
```

The dashboard binds `$PORT` or `DASHBOARD_PORT` when one is set.
The default mode (no `BOT_MODE` + `LIVE_TRADING_ENABLED` + `DRY_RUN=false` all
set) is **paper trading**: the bot still scans markets, sizes positions, and
writes to `trades.jsonl`, but `PaperExchangeClient` intercepts all orders and
nothing hits Polymarket.

## Backtesting

The backtest engine (`backtest.py`) replays historical Polymarket markets
through the exact filters + sizing the live bot uses, with realistic fees and
slippage.

```bash
# 1. Get data — jon-becker/prediction-market-analysis (2026 clean Polymarket dataset)
./scripts/fetch_prediction_market_analysis.sh

# 2. Run a single backtest against config.json
uv run python backtest.py --data-dir data/polymarket --walk-forward-splits 4

# 3. Or quick smoke test on deterministic synthetic data
uv run python backtest.py --synthetic --synthetic-n 800 --walk-forward-splits 4
```

Output metrics: Total PnL (USD), Win Rate %, Sharpe Ratio, Max Drawdown %,
Trade Count, Avg Hold Days, plus per-fold walk-forward numbers.

## Autoresearch Optimizer (Karpathy-style)

`optimizer.py` runs the autonomous optimization loop described in
[`program.md`](program.md). It samples candidate configs (random + Gaussian
perturbation around the current best), scores each under walk-forward
validation, and writes leaderboard artifacts to
`artifacts/optimizer/` which the dashboard serves read-only.

```bash
# Quick offline smoke test (synthetic data)
uv run python optimizer.py --synthetic --iters 50

# Overnight run against real data
uv run python optimizer.py --data-dir data/polymarket --iters 500 --walk-forward-splits 4
```

Outputs:

- `artifacts/optimizer/trials.jsonl` — every trial with params + metrics
- `artifacts/optimizer/best_configs.json` — top-N leaderboard
- `artifacts/optimizer/baseline.json` — seed config from `config.json`
- `artifacts/optimizer/latest.json` — current best

The optimizer enforces program.md's "never commit in-sample-only wins" rule:
candidates must beat baseline on **both** OOS Total PnL and OOS Sharpe to be
accepted as the new best.

## Paper Trading Mode

Paper mode is the default — no special flags needed beyond the defaults in
`.env.example`:

```bash
BOT_MODE=paper
DRY_RUN=true
LIVE_TRADING_ENABLED=false

export PORT=8080
uv run python -m bot.main
```

In paper mode every entry / exit is routed through `PaperExchangeClient`,
logged to `trades.jsonl`, and surfaced in the dashboard.

## Dashboard

The dashboard serves:

- Live / paper mode status (`/api/status`)
- Current strategy config (`/api/config`)
- Backtest latest / best / baseline (`/api/backtest/{latest,best,baseline}`)
- Open positions, realized/unrealized PnL, recent trades, session PnL (`/ws`)

Open `http://<host>:<port>/` in your browser.

## Oracle Cloud Deployment (4 OCPU / 24 GB)

1. **Open port 22 and your dashboard port (e.g. 8080)** in the VCN Security
   List / NSG for the source CIDR you trust. At minimum allow TCP/22 from your
   workstation and TCP/8080 from `0.0.0.0/0` if you want public access.
2. **SSH in** with the instance key:

   ```bash
   chmod 600 instance_key.pem
   ssh -i instance_key.pem ubuntu@<instance-public-ip>
   ```

3. **Bootstrap the VM:**

   ```bash
   sudo apt-get update && sudo apt-get install -y git curl build-essential
   curl -LsSf https://astral.sh/uv/install.sh | sh
   source ~/.bashrc

   git clone https://github.com/sterlingcrispin/nothing-ever-happens.git
   cd nothing-ever-happens
   git checkout devin-autoresearch-optimization
   uv venv --python 3.12
   uv pip install -e ".[dev]"
   cp config.example.json config.json
   cp .env.example .env
   ```

4. **Fetch the 2026 Polymarket dataset (optional for paper trading, required for optimizer):**

   ```bash
   ./scripts/fetch_prediction_market_analysis.sh
   ```

5. **Run the first autoresearch session (overnight):**

   ```bash
   mkdir -p artifacts/optimizer
   nohup uv run python optimizer.py \
       --data-dir data/polymarket \
       --iters 500 --walk-forward-splits 4 \
       > artifacts/optimizer/optimizer.log 2>&1 &
   ```

6. **Start paper trading + dashboard:**

   ```bash
   export BOT_MODE=paper DRY_RUN=true LIVE_TRADING_ENABLED=false PORT=8080
   nohup uv run python -m bot.main > bot.log 2>&1 &
   ```

7. **Open the dashboard** in your browser at
   `http://<instance-public-ip>:8080/`. The Oracle Cloud firewall must allow
   ingress on that port. If you prefer not to expose it publicly, SSH-tunnel
   instead:

   ```bash
   ssh -i instance_key.pem -L 8080:127.0.0.1:8080 ubuntu@<instance-public-ip>
   ```

   and then open `http://localhost:8080/` locally.

### Example best config after optimization

After an overnight run on the 2026 dataset, read the leaderboard with:

```bash
cat artifacts/optimizer/latest.json | jq '.params, .oos'
```

A typical result tightens the NO-price cap (e.g. `no_price_cap ≈ 0.56`), bumps
`min_market_days` toward 50–60 (short-horizon markets are noisier), and
raises `exit_threshold` to ≈0.90 so winners have more room to run.

## Heroku Workflow

The shell helpers use either an explicit app name argument or `HEROKU_APP_NAME`.

```bash
export HEROKU_APP_NAME=<your-app>
./alive.sh
./logs.sh
./live_enabled.sh
./live_disabled.sh
./kill.sh
```

Generic deployment flow:

```bash
heroku config:set BOT_MODE=live DRY_RUN=false LIVE_TRADING_ENABLED=true -a "$HEROKU_APP_NAME"
heroku config:set PRIVATE_KEY=<key> FUNDER_ADDRESS=<addr> POLYGON_RPC_URL=<url> DATABASE_URL=<url> -a "$HEROKU_APP_NAME"
git push heroku <branch>:main
heroku ps:scale web=1 worker=0 -a "$HEROKU_APP_NAME"
```

Only run the `web` dyno. The `worker` entry exists only to fail fast if it is started accidentally.

## Tests

```bash
python -m pytest -q
```

## Included Scripts

| Script | Purpose |
| --- | --- |
| `scripts/db_stats.py` | Inspect live database table counts and recent activity |
| `scripts/export_db.py` | Export live tables from `DATABASE_URL` or a Heroku app |
| `scripts/wallet_history.py` | Pull positions, trades, and balances for the configured wallet |
| `scripts/parse_logs.py` | Convert Heroku JSON logs into readable terminal or HTML output |

## Repository Hygiene

Local config, ledgers, exports, reports, and deployment artifacts are ignored by default.
