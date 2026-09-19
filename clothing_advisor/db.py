"""SQLite storage. One short-lived connection per operation; WAL mode allows the workers and web to share the file."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha TEXT UNIQUE NOT NULL,
    photo TEXT NOT NULL,
    thumb TEXT NOT NULL,
    category TEXT,
    subtype TEXT NOT NULL DEFAULT '',
    colors TEXT NOT NULL DEFAULT '[]',
    pattern TEXT,
    formality INTEGER,
    warmth INTEGER,
    seasons TEXT NOT NULL DEFAULT '[]',
    layer TEXT,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'clean',
    reviewed INTEGER NOT NULL DEFAULT 0,
    ai_state TEXT NOT NULL DEFAULT 'queued',
    ai_error TEXT NOT NULL DEFAULT '',
    ai_issue TEXT NOT NULL DEFAULT '',
    ai_confidence TEXT NOT NULL DEFAULT '',
    wears_since_wash INTEGER NOT NULL DEFAULT 0,
    last_worn TEXT,
    user_fields TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    excluded TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outfits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    item_ids TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    weather TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'proposed',
    chosen_on TEXT,
    worn_on TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    ym TEXT NOT NULL,
    purpose TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read INTEGER NOT NULL DEFAULT 0,
    cache_write INTEGER NOT NULL DEFAULT 0,
    cost_eur REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_ai_state ON items(ai_state);
CREATE INDEX IF NOT EXISTS idx_usage_ym ON usage(ym);
CREATE INDEX IF NOT EXISTS idx_outfits_session ON outfits(session_id);
"""

ITEM_JSON_FIELDS = ("colors", "seasons", "user_fields")
EDITABLE_ITEM_FIELDS = (
    "category", "subtype", "colors", "pattern", "formality", "warmth", "seasons", "layer", "description",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _item(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for f in ITEM_JSON_FIELDS:
        d[f] = json.loads(d[f] or "[]")
    d["reviewed"] = bool(d["reviewed"])
    return d


def _outfit(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    d["item_ids"] = json.loads(d["item_ids"])
    return d


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        with self.conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    # ---- settings -------------------------------------------------------
    def get_setting(self, key: str, default: str = "") -> str:
        with self.conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def setting_exists(self, key: str) -> bool:
        with self.conn() as c:
            return c.execute("SELECT 1 FROM settings WHERE key=?", (key,)).fetchone() is not None

    # ---- items ----------------------------------------------------------
    def add_item(self, sha: str, photo: str, thumb: str) -> int | None:
        """Insert a queued item. Returns None when the same photo was added before."""
        with self.conn() as c:
            try:
                cur = c.execute(
                    "INSERT INTO items(sha, photo, thumb, created_at) VALUES(?,?,?,?)",
                    (sha, photo, thumb, now_iso()),
                )
            except sqlite3.IntegrityError:
                return None
            return cur.lastrowid

    def get_item(self, item_id: int) -> dict[str, Any] | None:
        with self.conn() as c:
            return _item(c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone())

    def list_items(self, *, reviewed: bool | None = None, status: str | None = None,
                   category: str | None = None, ai_state: str | None = None,
                   limit: int | None = None) -> list[dict[str, Any]]:
        where, args = [], []
        if reviewed is not None:
            where.append("reviewed=?"); args.append(int(reviewed))
        if status:
            where.append("status=?"); args.append(status)
        if category:
            where.append("category=?"); args.append(category)
        if ai_state:
            where.append("ai_state=?"); args.append(ai_state)
        sql = "SELECT * FROM items" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self.conn() as c:
            return [_item(r) for r in c.execute(sql, args).fetchall()]

    def items_by_ids(self, ids: list[int]) -> dict[int, dict[str, Any]]:
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        with self.conn() as c:
            rows = c.execute(f"SELECT * FROM items WHERE id IN ({marks})", ids).fetchall()
        return {r["id"]: _item(r) for r in rows}

    def update_item(self, item_id: int, fields: dict[str, Any]) -> None:
        if not fields:
            return
        cols, args = [], []
        for k, v in fields.items():
            cols.append(f"{k}=?")
            args.append(json.dumps(v) if k in ITEM_JSON_FIELDS else v)
        args.append(item_id)
        with self.conn() as c:
            c.execute(f"UPDATE items SET {', '.join(cols)} WHERE id=?", args)

    def delete_item(self, item_id: int) -> dict[str, Any] | None:
        item = self.get_item(item_id)
        with self.conn() as c:
            c.execute("DELETE FROM items WHERE id=?", (item_id,))
        return item

    def counts(self) -> dict[str, int]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT ai_state, reviewed, COUNT(*) n FROM items GROUP BY ai_state, reviewed"
            ).fetchall()
        out = {"queued": 0, "processing": 0, "error": 0, "done_unreviewed": 0, "reviewed": 0}
        for r in rows:
            if r["reviewed"]:
                out["reviewed"] += r["n"]
            elif r["ai_state"] == "done":
                out["done_unreviewed"] += r["n"]
            else:
                out[r["ai_state"]] += r["n"]
        return out

    def claim_next_queued(self) -> dict[str, Any] | None:
        with self.conn() as c:
            row = c.execute(
                "UPDATE items SET ai_state='processing' WHERE id=("
                "SELECT id FROM items WHERE ai_state='queued' ORDER BY id LIMIT 1) RETURNING *"
            ).fetchone()
        return _item(row)

    def reset_processing(self) -> None:
        with self.conn() as c:
            c.execute("UPDATE items SET ai_state='queued' WHERE ai_state='processing'")

    # ---- sessions & messages --------------------------------------------
    def new_session(self) -> int:
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO sessions(created_at, updated_at) VALUES(?,?)", (now_iso(), now_iso())
            )
            return cur.lastrowid

    def get_session(self, session_id: int) -> dict[str, Any] | None:
        with self.conn() as c:
            row = c.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["excluded"] = json.loads(d["excluded"])
        return d

    def latest_session(self) -> dict[str, Any] | None:
        with self.conn() as c:
            row = c.execute("SELECT id FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
        return self.get_session(row["id"]) if row else None

    def set_session_excluded(self, session_id: int, excluded: list[int]) -> None:
        with self.conn() as c:
            c.execute(
                "UPDATE sessions SET excluded=?, updated_at=? WHERE id=?",
                (json.dumps(sorted(set(excluded))), now_iso(), session_id),
            )

    def touch_session(self, session_id: int) -> None:
        with self.conn() as c:
            c.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now_iso(), session_id))

    def add_message(self, session_id: int, role: str, content: str) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO messages(session_id, role, content, created_at) VALUES(?,?,?,?)",
                (session_id, role, content, now_iso()),
            )

    def get_messages(self, session_id: int) -> list[dict[str, Any]]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT role, content, created_at FROM messages WHERE session_id=? ORDER BY id", (session_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- outfits ---------------------------------------------------------
    def add_outfit(self, session_id: int, name: str, item_ids: list[int], reason: str, weather: str) -> int:
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO outfits(session_id, name, item_ids, reason, weather, created_at) VALUES(?,?,?,?,?,?)",
                (session_id, name, json.dumps(item_ids), reason, weather, now_iso()),
            )
            return cur.lastrowid

    def get_outfit(self, outfit_id: int) -> dict[str, Any] | None:
        with self.conn() as c:
            return _outfit(c.execute("SELECT * FROM outfits WHERE id=?", (outfit_id,)).fetchone())

    def list_outfits(self, *, session_id: int | None = None, statuses: tuple[str, ...] | None = None,
                     since: str | None = None) -> list[dict[str, Any]]:
        where, args = [], []
        if session_id is not None:
            where.append("session_id=?"); args.append(session_id)
        if statuses:
            where.append(f"status IN ({','.join('?' * len(statuses))})"); args.extend(statuses)
        if since:
            where.append("COALESCE(worn_on, chosen_on, substr(created_at,1,10)) >= ?"); args.append(since)
        sql = "SELECT * FROM outfits" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id"
        with self.conn() as c:
            return [_outfit(r) for r in c.execute(sql, args).fetchall()]

    def update_outfit(self, outfit_id: int, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE outfits SET {cols} WHERE id=?", [*fields.values(), outfit_id])

    def replace_proposals(self, session_id: int) -> None:
        with self.conn() as c:
            c.execute(
                "UPDATE outfits SET status='replaced' WHERE session_id=? AND status='proposed'", (session_id,)
            )

    def demote_chosen(self, date: str, except_id: int) -> None:
        with self.conn() as c:
            c.execute(
                "UPDATE outfits SET status='proposed', chosen_on=NULL "
                "WHERE status='chosen' AND chosen_on=? AND id<>?",
                (date, except_id),
            )

    # ---- usage -----------------------------------------------------------
    def add_usage(self, ym: str, purpose: str, model: str, in_tok: int, out_tok: int,
                  cache_read: int, cache_write: int, cost_eur: float) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO usage(ts, ym, purpose, model, input_tokens, output_tokens, cache_read, cache_write, cost_eur)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (now_iso(), ym, purpose, model, in_tok, out_tok, cache_read, cache_write, cost_eur),
            )

    def month_spend(self, ym: str) -> float:
        with self.conn() as c:
            row = c.execute("SELECT COALESCE(SUM(cost_eur),0) s FROM usage WHERE ym=?", (ym,)).fetchone()
        return float(row["s"])

    def usage_by_purpose(self, ym: str) -> list[dict[str, Any]]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT purpose, COUNT(*) calls, SUM(cost_eur) eur, SUM(input_tokens) in_tok, "
                "SUM(output_tokens) out_tok, SUM(cache_read) cache_read "
                "FROM usage WHERE ym=? GROUP BY purpose",
                (ym,),
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_usage(self, limit: int = 15) -> list[dict[str, Any]]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM usage ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def avg_cost(self, purpose: str, last_n: int = 50) -> tuple[float, int]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT cost_eur FROM usage WHERE purpose=? ORDER BY id DESC LIMIT ?", (purpose, last_n)
            ).fetchall()
        if not rows:
            return 0.0, 0
        return sum(r["cost_eur"] for r in rows) / len(rows), len(rows)
