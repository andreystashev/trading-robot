# Trading Robot

[![Checks](https://github.com/andreystashev/trading-robot/actions/workflows/ci.yml/badge.svg)](https://github.com/andreystashev/trading-robot/actions/workflows/ci.yml)

A local **Python research dashboard for T-Invest Sandbox**. Configure strategy profiles, test rules on historical data, inspect decisions, and run a virtual portfolio without broker orders. Live trading is disabled in code.

This is an experimental research project. A passing test suite does not establish a profitable strategy; current research has not confirmed a persistent edge. Backtests and paper trading model execution and can differ from a broker.

## Features

- JSON strategy profiles: instrument, algorithm, parameters, and costs.
- Moving-average, breakout, mean-reversion, and RSI-pullback strategies.
- Historical tests with dates, commissions, slippage, and CSV/HTML reports.
- Paper trading against current quotes, with virtual positions, P&L, and drawdown.
- Observation mode and separately enabled Sandbox execution.
- SQLite decision journal and rotating logs with token masking.
- Additional market-filter and daily portfolio-momentum research tools.

## Quick start with Python

**Python 3.12** is recommended; CI covers Linux with Python 3.11, 3.12, and 3.13. Run all commands from the repository root.

```bash
git clone https://github.com/andreystashev/trading-robot.git
cd trading-robot
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python tools/install_dependencies.py
cp trading_bot/.env.example trading_bot/.env
```

For Windows PowerShell, replace the environment and copy commands with:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python tools/install_dependencies.py
Copy-Item trading_bot/.env.example trading_bot/.env
```

Set `T_INVEST_TOKEN` in `trading_bot/.env` to a **Sandbox token**. See the [official token instructions](https://developer.tbank.ru/invest/intro/intro/token). Keep an existing configured `.env` rather than overwriting it. Both order-submission flags default to disabled.

```bash
python trading_bot/main.py info
python trading_bot/main.py panel
```

Open **<http://127.0.0.1:8765/>**. Select a profile, wait for quotes, test it on historical data, then start paper trading. Opening the panel or switching a profile does not create trades. Stop the current session before switching; only one profile runs at a time. Ctrl+C stops the panel.

Paper positions persist, but a restarted application requires an explicit session start. Data refreshes roughly every 30 seconds. New virtual trades require a current quote and an available trading session.

## Quick start with Docker

Requirements: Docker Engine/Desktop and **Compose v2**. Host Python is not needed. Copy `trading_bot/.env.example` to `trading_bot/.env`, set the token, then run:

```bash
docker compose up --build -d
docker compose ps
docker compose logs -f panel
```

Open **<http://127.0.0.1:8765/>**. Stop a Python-launched panel first if it already uses that port.

The container runs without root. The token is supplied through a Compose secret at runtime; `.env` is excluded from the image. The host port is bound to localhost. Keep the panel local.

Compose explicitly disables Sandbox order submission while retaining research and paper trading. Enabling Sandbox execution requires separate Compose configuration; see the [usage guide (Russian)](docs/usage.md).

```bash
docker compose stop        # Stop and retain data
docker compose start       # Restart; strategies do not start automatically
docker compose down        # Remove containers; retain named volumes
```

Profiles, runtime state, logs, and reports persist in named volumes. **`docker compose down -v` deletes those data volumes**; do not use it for ordinary shutdown. Existing profile volumes retain their contents after image updates; import new JSON profiles through the panel.

## Architecture

| Component | Responsibility |
| --- | --- |
| [trading_bot/main.py](trading_bot/main.py) | CLI entry point |
| [strategy.py](trading_bot/strategy.py), [strategy_profiles/](trading_bot/strategy_profiles/) | Signals and declarative profiles |
| [auto_trader.py](trading_bot/auto_trader.py), [t_invest_client.py](trading_bot/t_invest_client.py) | Risk checks, orders, and Sandbox reconciliation |
| [paper_trader.py](trading_bot/paper_trader.py) | Persistent virtual portfolio without order API calls |
| [backtest.py](trading_bot/backtest.py) | Historical execution model and reports |
| [dashboard.py](trading_bot/dashboard.py), [web/](trading_bot/web/) | Local HTTP server and interface |
| `research*.py`, `strategy_evaluation.py`, `portfolio_research.py` | Research and evaluation |
| [trading_bot/tests/](trading_bot/tests/) | Automated checks without a real token |
| [tools/](tools/) | Dependency installation and publication checks |

For Python runs, runtime data lives under `trading_bot/.runtime`, `trading_bot/logs`, `trading_bot/reports`, and local SQLite/service files. These are excluded from Git and the Docker build context. User profiles are JSON files in `trading_bot/strategy_profiles`; review their names and contents before publishing them.

## Documentation

The detailed guides are currently in Russian:

- [Commands, profiles, model limits, and troubleshooting](docs/usage.md)
- [Architecture and execution controls](docs/architecture.md)
- [Security and publication preparation](SECURITY.md)
- [Development, tests, and source export](CONTRIBUTING.md)

## Development and validation

After installing dependencies:

```bash
python -m unittest discover -s trading_bot/tests -q
python -m pip check
python tools/check_publication.py
python -m compileall -q trading_bot tools
```

GitHub Actions runs the Python test matrix and also builds the container, runs its tests, validates Compose, and checks the dashboard's status endpoint. These checks validate implementation behavior, not trading performance.

To export the allowlisted publication sources:

```bash
python tools/export_source.py
```

The archive at `dist/trading-robot-source.zip` excludes the environment file, virtual environments, account data, logs, and reports. Secret detection is heuristic; review source contents before sharing.

## Dependencies and project status

The installer verifies the official T-Bank SDK wheel with SHA-256. It uses the SDK's public CA only for downloads from `opensource.tbank.ru`; system certificates and PyPI TLS verification are unchanged. Other dependencies come from PyPI.

Transitive versions are pinned in [trading_bot/constraints.txt](trading_bot/constraints.txt). This is a version constraint set, not a complete cross-platform wheel hash lock. `tzdata` supplies time zones for Windows and slim containers. SDK upgrades require checking the adapter, models, and telemetry configuration.

A project license has not yet been selected; no `LICENSE` file is included.
