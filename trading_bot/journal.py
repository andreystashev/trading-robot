"""Durable, append-only decision and order audit; no credentials are stored."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

DEFAULT_PATH = Path(__file__).with_name("journal.sqlite3")


def database(state_path):
    return Path(state_path).with_name("journal.sqlite3")


def connect(path=DEFAULT_PATH):
    connection = sqlite3.connect(path, timeout=5)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, time TEXT NOT NULL, kind TEXT NOT NULL, plan_id TEXT, request_id TEXT, payload TEXT NOT NULL)"
        )
        connection.execute("CREATE INDEX IF NOT EXISTS events_plan ON events(plan_id)")
    except Exception:
        connection.close()
        raise
    return connection


def record(kind, payload, *, path=DEFAULT_PATH, plan_id=None, request_id=None):
    event_id = str(uuid4())
    # Callers provide a fixed set of numeric trading fields, never config/token.
    payload_json = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    db = connect(path)
    try:
        with db:
            db.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,?)",
                (
                    event_id,
                    datetime.now(timezone.utc).isoformat(),
                    kind,
                    plan_id,
                    request_id,
                    payload_json,
                ),
            )
    finally:
        db.close()
    return event_id


def latest(limit=100, path=DEFAULT_PATH):
    if not Path(path).exists():
        return []
    db = sqlite3.connect(
        Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5
    )
    try:
        rows = db.execute(
            "SELECT id,time,kind,plan_id,request_id,payload FROM events ORDER BY rowid DESC LIMIT ?",
            (max(1, min(int(limit), 1000)),),
        ).fetchall()
        return [
            dict(
                zip(
                    ["id", "time", "kind", "plan_id", "request_id", "payload"],
                    (*r[:5], json.loads(r[5])),
                )
            )
            for r in rows
        ]
    finally:
        db.close()
