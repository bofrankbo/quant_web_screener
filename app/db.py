import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import SQLITE_PATH


def _connect(db_path: Path = SQLITE_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


@contextmanager
def get_connection(db_path: Path = SQLITE_PATH):
    conn = _connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path = SQLITE_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                google_sub TEXT NOT NULL UNIQUE,
                email TEXT,
                name TEXT,
                picture TEXT,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            );

            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id INTEGER PRIMARY KEY,
                theme TEXT NOT NULL DEFAULT 'dark',
                default_watchlist TEXT,
                preferences_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS user_watchlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                custom_label TEXT NOT NULL DEFAULT '備註',
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                UNIQUE(user_id, name),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS watchlist_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                watchlist_id INTEGER NOT NULL,
                ticker TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                UNIQUE(watchlist_id, ticker),
                FOREIGN KEY (watchlist_id) REFERENCES user_watchlists(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS activity_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_user_watchlists_user_id
                ON user_watchlists(user_id);
            CREATE INDEX IF NOT EXISTS idx_watchlist_items_watchlist_id
                ON watchlist_items(watchlist_id);
            CREATE INDEX IF NOT EXISTS idx_activity_logs_user_id
                ON activity_logs(user_id);

            CREATE TABLE IF NOT EXISTS user_portfolios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                UNIQUE(user_id, name),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS portfolio_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                portfolio_id INTEGER NOT NULL,
                ticker TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                shares REAL NOT NULL DEFAULT 0,
                lots REAL NOT NULL DEFAULT 0,
                avg_cost REAL NOT NULL DEFAULT 0,
                currency TEXT NOT NULL DEFAULT 'TWD',
                market TEXT NOT NULL DEFAULT 'TW',
                price_override REAL,
                entry_date TEXT NOT NULL DEFAULT '',
                entry_reason TEXT NOT NULL DEFAULT '',
                margin_lots REAL NOT NULL DEFAULT 0,
                futures_lots REAL NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                UNIQUE(portfolio_id, ticker),
                FOREIGN KEY (portfolio_id) REFERENCES user_portfolios(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS portfolio_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                portfolio_id INTEGER,
                ticker TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL,
                shares REAL NOT NULL,
                price REAL NOT NULL,
                currency TEXT NOT NULL DEFAULT 'TWD',
                commission REAL NOT NULL DEFAULT 0,
                trade_date TEXT NOT NULL,
                entry_reason TEXT NOT NULL DEFAULT '',
                exit_reason TEXT NOT NULL DEFAULT '',
                realized_pnl REAL NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (portfolio_id) REFERENCES user_portfolios(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS portfolio_monthly_pnl (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                year_month TEXT NOT NULL,
                unrealized_tw REAL NOT NULL DEFAULT 0,
                realized_tw REAL NOT NULL DEFAULT 0,
                dividend_tw REAL NOT NULL DEFAULT 0,
                unrealized_us_usd REAL NOT NULL DEFAULT 0,
                realized_us_usd REAL NOT NULL DEFAULT 0,
                usd_rate REAL NOT NULL DEFAULT 31.0,
                unrealized_futures REAL NOT NULL DEFAULT 0,
                realized_futures REAL NOT NULL DEFAULT 0,
                algo_pnl REAL NOT NULL DEFAULT 0,
                fee_rebate REAL NOT NULL DEFAULT 0,
                total_pnl REAL NOT NULL DEFAULT 0,
                nav REAL NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                UNIQUE(user_id, year_month),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_user_portfolios_user_id
                ON user_portfolios(user_id);
            CREATE INDEX IF NOT EXISTS idx_portfolio_positions_portfolio_id
                ON portfolio_positions(portfolio_id);
            CREATE INDEX IF NOT EXISTS idx_portfolio_trades_user_id
                ON portfolio_trades(user_id);
            CREATE INDEX IF NOT EXISTS idx_portfolio_monthly_pnl_user_id
                ON portfolio_monthly_pnl(user_id);
            """
        )
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(user_watchlists)").fetchall()
        }
        if "custom_label" not in columns:
            conn.execute(
                """
                ALTER TABLE user_watchlists
                ADD COLUMN custom_label TEXT NOT NULL DEFAULT '備註'
                """
            )


def log_activity(action: str, payload: dict | None = None, user_id: int | None = None) -> None:
    payload_json = json.dumps(payload or {}, ensure_ascii=False)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO activity_logs (user_id, action, payload_json)
            VALUES (?, ?, ?)
            """,
            (user_id, action, payload_json),
        )


def upsert_google_user(
    google_sub: str,
    email: str | None = None,
    name: str | None = None,
    picture: str | None = None,
) -> dict:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO users (google_sub, email, name, picture)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(google_sub) DO UPDATE SET
                email = excluded.email,
                name = excluded.name,
                picture = excluded.picture,
                updated_at = CURRENT_TIMESTAMP
            """,
            (google_sub, email, name, picture),
        )
        row = conn.execute(
            """
            SELECT id, google_sub, email, name, picture, created_at, updated_at
            FROM users
            WHERE google_sub = ?
            """,
            (google_sub,),
        ).fetchone()
        return dict(row)


def get_user_by_id(user_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT id, google_sub, email, name, picture, created_at, updated_at
            FROM users
            WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def get_watchlists_for_user(user_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, name, custom_label
            FROM user_watchlists
            WHERE user_id = ?
            ORDER BY created_at, id
            """,
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_watchlist_counts_for_user(user_id: int) -> dict[str, int]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT uw.name AS name, COUNT(wi.id) AS count
            FROM user_watchlists uw
            LEFT JOIN watchlist_items wi ON wi.watchlist_id = uw.id
            WHERE uw.user_id = ?
            GROUP BY uw.id, uw.name
            ORDER BY uw.created_at, uw.id
            """,
            (user_id,),
        ).fetchall()
        return {row["name"]: int(row["count"]) for row in rows}


def get_watchlist_by_name(user_id: int, name: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT id, user_id, name, custom_label, created_at, updated_at
            FROM user_watchlists
            WHERE user_id = ? AND name = ?
            """,
            (user_id, name),
        ).fetchone()
        return dict(row) if row else None


def create_watchlist_for_user(user_id: int, name: str) -> dict:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO user_watchlists (user_id, name, custom_label)
            VALUES (?, ?, '備註')
            ON CONFLICT(user_id, name) DO UPDATE SET
                updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, name),
        )
        row = conn.execute(
            """
            SELECT id, user_id, name, custom_label, created_at, updated_at
            FROM user_watchlists
            WHERE user_id = ? AND name = ?
            """,
            (user_id, name),
        ).fetchone()
        return dict(row)


def delete_watchlist_for_user(user_id: int, name: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            DELETE FROM user_watchlists
            WHERE user_id = ? AND name = ?
            """,
            (user_id, name),
        )


def rename_watchlist_for_user(user_id: int, old_name: str, new_name: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE user_watchlists
            SET name = ?, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND name = ?
            """,
            (new_name, user_id, old_name),
        )


def add_ticker_to_watchlist(user_id: int, name: str, ticker: str) -> None:
    with get_connection() as conn:
        wl = conn.execute(
            """
            SELECT id
            FROM user_watchlists
            WHERE user_id = ? AND name = ?
            """,
            (user_id, name),
        ).fetchone()
        if wl is None:
            raise KeyError(name)
        conn.execute(
            """
            INSERT INTO watchlist_items (watchlist_id, ticker)
            VALUES (?, ?)
            ON CONFLICT(watchlist_id, ticker) DO NOTHING
            """,
            (wl["id"], ticker),
        )


def remove_ticker_from_watchlist(user_id: int, name: str, ticker: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            DELETE FROM watchlist_items
            WHERE watchlist_id = (
                SELECT id
                FROM user_watchlists
                WHERE user_id = ? AND name = ?
            )
            AND ticker = ?
            """,
            (user_id, name, ticker),
        )


def get_watchlist_items_for_user(user_id: int, name: str) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT wi.ticker, wi.note, wi.active
            FROM watchlist_items wi
            JOIN user_watchlists uw ON uw.id = wi.watchlist_id
            WHERE uw.user_id = ? AND uw.name = ?
            ORDER BY wi.id
            """,
            (user_id, name),
        ).fetchall()
        return [dict(row) for row in rows]


def set_watchlist_custom_label(user_id: int, name: str, label: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE user_watchlists
            SET custom_label = ?, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND name = ?
            """,
            (label, user_id, name),
        )


def set_watchlist_note(user_id: int, name: str, ticker: str, note: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE watchlist_items
            SET note = ?, updated_at = CURRENT_TIMESTAMP
            WHERE watchlist_id = (
                SELECT id
                FROM user_watchlists
                WHERE user_id = ? AND name = ?
            )
            AND ticker = ?
            """,
            (note, user_id, name, ticker),
        )


def set_watchlist_item_active(user_id: int, name: str, ticker: str, active: bool) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE watchlist_items
            SET active = ?, updated_at = CURRENT_TIMESTAMP
            WHERE watchlist_id = (
                SELECT id
                FROM user_watchlists
                WHERE user_id = ? AND name = ?
            )
            AND ticker = ?
            """,
            (1 if active else 0, user_id, name, ticker),
        )


def get_watchlist_summary_for_user(user_id: int, name: str) -> dict | None:
    with get_connection() as conn:
        watchlist = conn.execute(
            """
            SELECT id, name
            FROM user_watchlists
            WHERE user_id = ? AND name = ?
            """,
            (user_id, name),
        ).fetchone()
        if watchlist is None:
            return None

        rows = conn.execute(
            """
            SELECT ticker, note, active
            FROM watchlist_items
            WHERE watchlist_id = ?
            ORDER BY id
            """,
            (watchlist["id"],),
        ).fetchall()
        return {
            "id": watchlist["id"],
            "name": watchlist["name"],
            "rows": [dict(row) for row in rows],
        }


def clear_watchlists_for_user(user_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            DELETE FROM user_watchlists
            WHERE user_id = ?
            """,
            (user_id,),
        )


# ---------------------------------------------------------------------------
# Portfolio CRUD
# ---------------------------------------------------------------------------


def get_portfolios_for_user(user_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, name, description, created_at
            FROM user_portfolios
            WHERE user_id = ?
            ORDER BY created_at, id
            """,
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_portfolio_by_name(user_id: int, name: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT id, name, description, created_at
            FROM user_portfolios
            WHERE user_id = ? AND name = ?
            """,
            (user_id, name),
        ).fetchone()
        return dict(row) if row else None


def create_portfolio_for_user(user_id: int, name: str, description: str = "") -> dict:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO user_portfolios (user_id, name, description)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, name) DO NOTHING
            """,
            (user_id, name, description),
        )
        row = conn.execute(
            """
            SELECT id, name, description, created_at
            FROM user_portfolios
            WHERE user_id = ? AND name = ?
            """,
            (user_id, name),
        ).fetchone()
        return dict(row)


def delete_portfolio_for_user(user_id: int, name: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            DELETE FROM user_portfolios
            WHERE user_id = ? AND name = ?
            """,
            (user_id, name),
        )


def get_portfolio_positions(portfolio_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, ticker, name, shares, lots, avg_cost, currency, market,
                   price_override, entry_date, entry_reason, margin_lots,
                   futures_lots, note, created_at, updated_at
            FROM portfolio_positions
            WHERE portfolio_id = ?
            ORDER BY created_at, id
            """,
            (portfolio_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def upsert_portfolio_position(
    portfolio_id: int,
    ticker: str,
    name: str = "",
    shares: float = 0,
    lots: float = 0,
    avg_cost: float = 0,
    currency: str = "TWD",
    market: str = "TW",
    price_override: float | None = None,
    entry_date: str = "",
    entry_reason: str = "",
    margin_lots: float = 0,
    futures_lots: float = 0,
    note: str = "",
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO portfolio_positions
                (portfolio_id, ticker, name, shares, lots, avg_cost, currency, market,
                 price_override, entry_date, entry_reason, margin_lots, futures_lots, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(portfolio_id, ticker) DO UPDATE SET
                name = excluded.name,
                shares = excluded.shares,
                lots = excluded.lots,
                avg_cost = excluded.avg_cost,
                currency = excluded.currency,
                market = excluded.market,
                price_override = excluded.price_override,
                entry_date = excluded.entry_date,
                entry_reason = excluded.entry_reason,
                margin_lots = excluded.margin_lots,
                futures_lots = excluded.futures_lots,
                note = excluded.note,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                portfolio_id, ticker, name, shares, lots, avg_cost, currency, market,
                price_override, entry_date, entry_reason, margin_lots, futures_lots, note,
            ),
        )


def delete_portfolio_position(portfolio_id: int, ticker: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            DELETE FROM portfolio_positions
            WHERE portfolio_id = ? AND ticker = ?
            """,
            (portfolio_id, ticker),
        )


def add_portfolio_trade(
    user_id: int,
    portfolio_id: int | None,
    ticker: str,
    name: str,
    action: str,
    shares: float,
    price: float,
    currency: str = "TWD",
    commission: float = 0,
    trade_date: str = "",
    entry_reason: str = "",
    exit_reason: str = "",
    realized_pnl: float = 0,
    note: str = "",
) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO portfolio_trades
                (user_id, portfolio_id, ticker, name, action, shares, price,
                 currency, commission, trade_date, entry_reason, exit_reason,
                 realized_pnl, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id, portfolio_id, ticker, name, action, shares, price,
                currency, commission, trade_date, entry_reason, exit_reason,
                realized_pnl, note,
            ),
        )
        return cur.lastrowid


def get_portfolio_trades(user_id: int, portfolio_id: int | None = None) -> list[dict]:
    with get_connection() as conn:
        if portfolio_id is not None:
            rows = conn.execute(
                """
                SELECT id, portfolio_id, ticker, name, action, shares, price,
                       currency, commission, trade_date, entry_reason, exit_reason,
                       realized_pnl, note, created_at
                FROM portfolio_trades
                WHERE user_id = ? AND portfolio_id = ?
                ORDER BY trade_date DESC, created_at DESC
                """,
                (user_id, portfolio_id),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, portfolio_id, ticker, name, action, shares, price,
                       currency, commission, trade_date, entry_reason, exit_reason,
                       realized_pnl, note, created_at
                FROM portfolio_trades
                WHERE user_id = ?
                ORDER BY trade_date DESC, created_at DESC
                """,
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Monthly P&L CRUD
# ---------------------------------------------------------------------------

_PNL_FIELDS = (
    "unrealized_tw", "realized_tw", "dividend_tw",
    "unrealized_us_usd", "realized_us_usd", "usd_rate",
    "unrealized_futures", "realized_futures",
    "algo_pnl", "fee_rebate", "total_pnl", "nav", "note",
)


def upsert_monthly_pnl(user_id: int, year_month: str, **kwargs) -> None:
    fields = {k: v for k, v in kwargs.items() if k in _PNL_FIELDS}
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO portfolio_monthly_pnl (user_id, year_month)
            VALUES (?, ?)
            ON CONFLICT(user_id, year_month) DO NOTHING
            """,
            (user_id, year_month),
        )
        if fields:
            set_clause = ", ".join(f"{k} = ?" for k in fields)
            set_clause += ", updated_at = CURRENT_TIMESTAMP"
            conn.execute(
                f"UPDATE portfolio_monthly_pnl SET {set_clause} WHERE user_id = ? AND year_month = ?",
                (*fields.values(), user_id, year_month),
            )


def get_monthly_pnl(user_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT year_month, unrealized_tw, realized_tw, dividend_tw,
                   unrealized_us_usd, realized_us_usd, usd_rate,
                   unrealized_futures, realized_futures,
                   algo_pnl, fee_rebate, total_pnl, nav, note, updated_at
            FROM portfolio_monthly_pnl
            WHERE user_id = ?
            ORDER BY year_month
            """,
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]
