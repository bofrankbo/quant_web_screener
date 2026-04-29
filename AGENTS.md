# Repository Guidelines

## Project Structure & Module Organization
`app/` contains the FastAPI backend and screening logic: `api.py` exposes routes, `screener.py` computes filters, `pattern_matcher.py` handles DTW matching, `backtest.py` runs simulations, and `config.py` centralizes paths. `frontend/` holds the static HTML/JS pages served by the API. `scripts/` contains operational helpers such as `run_dev.sh`, `daily_update.sh`, `fetch_prices.py`, and `sync_cache.py`. Runtime data lives under `data/`, especially `data/cache/` and `data/watchlists.json`.

## Build, Test, and Development Commands
Install dependencies with `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`.
Run the app locally with `bash scripts/run_dev.sh`; it starts Uvicorn on `http://localhost:8000`. Refresh local market data with `bash scripts/daily_update.sh` or a date override like `bash scripts/daily_update.sh 2026-04-25`.
For a quick external smoke check, run `python test.py` after setting `FINMIND_API_KEY`.

## Coding Style & Naming Conventions
Use Python 3 style with 4-space indentation, `snake_case` for functions and variables, and `PascalCase` for Pydantic models. Keep API query parameters explicit and validated with `fastapi.Query(...)`. Prefer small, composable functions and keep path/config values in `app/config.py` rather than scattering literals.

## Testing Guidelines
There is no formal test suite in the repository. Validate backend changes by hitting `/health`, exercising the affected endpoint in `/docs`, and checking the relevant UI page. If you add tests, place them near the code they cover and use clear names such as `test_screen_filters.py` or `test_watchlists.py`.

## Commit & Pull Request Guidelines
Recent commits use short imperative summaries like `add backtest algo`, `rename commands`, and `better logging`. Keep commit messages brief, lower-case is acceptable, and lead with the change. Pull requests should describe the behavioral change, note any config or data migration impact, and include screenshots or API examples when UI or response shape changes.

## Security & Configuration Tips
Do not commit secrets. `.env` should hold `FINMIND_API_KEY`, `R2_*` credentials, and any non-default `TRADING_DATA_PATH`. Watchlist data is persisted in `data/watchlists.json`, so treat edits there as user-facing state.
