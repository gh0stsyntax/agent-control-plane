"""Persistenz (SQLite) fuer Cases, Events, Tasks, Queues, ERP-Mock und Settings."""

import json
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL,
    risk_tier TEXT,
    envelope TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    case_id TEXT,
    correlation_id TEXT,
    stage TEXT,
    component TEXT,
    kind TEXT,
    message TEXT,
    data TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_case ON events(case_id);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    type TEXT NOT NULL,
    status TEXT NOT NULL,
    title TEXT,
    risk_tier TEXT,
    required_approvals INTEGER DEFAULT 1,
    require_oob INTEGER DEFAULT 0,
    approvals TEXT DEFAULT '[]',
    payload TEXT,
    evidence_hash TEXT,
    decision TEXT,
    created_at TEXT NOT NULL,
    sla_due TEXT,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS queue_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue TEXT NOT NULL,
    reference TEXT NOT NULL,
    case_id TEXT,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    attempt INTEGER DEFAULT 0,
    exception_type TEXT,
    exception_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(queue, reference)
);
CREATE TABLE IF NOT EXISTS erp_vendors (
    supplier_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    iban TEXT NOT NULL,
    account_holder TEXT,
    status TEXT NOT NULL,
    avg_invoice REAL,
    email TEXT,
    phone TEXT
);
CREATE TABLE IF NOT EXISTS erp_purchase_orders (
    po_number TEXT PRIMARY KEY,
    supplier_id TEXT NOT NULL,
    amount REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS erp_documents (
    doc_no TEXT PRIMARY KEY,
    doc_type TEXT NOT NULL,
    supplier_id TEXT NOT NULL,
    reference TEXT,
    amount REAL,
    currency TEXT,
    po_number TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    case_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS erp_change_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    supplier_id TEXT,
    field TEXT,
    old_value TEXT,
    new_value TEXT,
    channel TEXT,
    idempotency_key TEXT UNIQUE,
    case_id TEXT
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    case_id TEXT,
    channel TEXT,
    recipient TEXT,
    subject TEXT,
    body TEXT,
    idempotency_key TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    case_id TEXT,
    tags TEXT,
    summary TEXT,
    idempotency_key TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS change_requests (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    proposed_by TEXT NOT NULL,
    status TEXT NOT NULL,
    comment TEXT,
    changes TEXT NOT NULL,
    diff TEXT NOT NULL,
    evaluation TEXT,
    base_hash TEXT,
    decided_by TEXT,
    decided_at TEXT,
    decision_comment TEXT
);
CREATE TABLE IF NOT EXISTS settings_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    change_id TEXT,
    applied_by TEXT
);
CREATE TABLE IF NOT EXISTS bridge_requests (
    idem_key TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    response TEXT NOT NULL,
    body_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    # ---- generic helpers -------------------------------------------------
    def execute(self, sql, params=()):
        with self.lock:
            return self.conn.execute(sql, params)

    def query(self, sql, params=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql, params=()):
        with self.lock:
            r = self.conn.execute(sql, params).fetchone()
            return dict(r) if r else None

    def transaction(self):
        return _Tx(self)

    def next_counter(self, name):
        with self.lock:
            self.conn.execute(
                "INSERT INTO counters(name, value) VALUES(?, 1) "
                "ON CONFLICT(name) DO UPDATE SET value = value + 1",
                (name,),
            )
            return self.conn.execute("SELECT value FROM counters WHERE name=?", (name,)).fetchone()[0]

    # ---- settings --------------------------------------------------------
    def get_setting(self, key, default=None):
        r = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(r["value"]) if r else default

    def set_setting(self, key, value):
        self.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    # ---- cases -----------------------------------------------------------
    def insert_case(self, envelope, state):
        ts = now_iso()
        self.execute(
            "INSERT INTO cases(case_id, correlation_id, idempotency_key, state, risk_tier, envelope, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (envelope["caseId"], envelope["correlationId"], envelope["idempotencyKey"], state,
             None, json.dumps(envelope), ts, ts),
        )

    def get_case(self, case_id):
        r = self.one("SELECT * FROM cases WHERE case_id=?", (case_id,))
        if r:
            r["envelope"] = json.loads(r["envelope"])
        return r

    def save_case(self, case_id, envelope, state):
        tier = (envelope.get("policy") or {}).get("riskTier")
        self.execute(
            "UPDATE cases SET envelope=?, state=?, risk_tier=?, updated_at=? WHERE case_id=?",
            (json.dumps(envelope), state, tier, now_iso(), case_id),
        )

    def list_cases(self, state=None, limit=500):
        if state:
            rows = self.query("SELECT * FROM cases WHERE state=? ORDER BY created_at DESC LIMIT ?", (state, limit))
        else:
            rows = self.query("SELECT * FROM cases ORDER BY created_at DESC LIMIT ?", (limit,))
        for r in rows:
            r["envelope"] = json.loads(r["envelope"])
        return rows

    # ---- events ----------------------------------------------------------
    def add_event(self, case_id, correlation_id, stage, component, kind, message, data=None):
        self.execute(
            "INSERT INTO events(ts, case_id, correlation_id, stage, component, kind, message, data) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (now_iso(), case_id, correlation_id, stage, component, kind, message,
             json.dumps(data, ensure_ascii=False) if data is not None else None),
        )

    def events(self, case_id=None, correlation_id=None, limit=500):
        sql, params = "SELECT * FROM events", []
        cond = []
        if case_id:
            cond.append("case_id=?")
            params.append(case_id)
        if correlation_id:
            cond.append("correlation_id=?")
            params.append(correlation_id)
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self.query(sql, params)
        for r in rows:
            r["data"] = json.loads(r["data"]) if r["data"] else None
        return rows


class _Tx:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        self.store.lock.acquire()
        self.store.conn.execute("BEGIN")
        return self.store

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type:
                self.store.conn.execute("ROLLBACK")
            else:
                self.store.conn.execute("COMMIT")
        finally:
            self.store.lock.release()
        return False
