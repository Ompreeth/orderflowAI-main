import os
import re

import psycopg2
import psycopg2.extras
import psycopg2.extensions

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Point it at your Postgres connection string "
        "(e.g. Neon's 'Pooled connection' string) in the environment or .env file."
    )

_INSERT_RE = re.compile(r"^\s*insert\s+into\s+", re.IGNORECASE)


class _Cursor:
    """Wraps a psycopg2 cursor to add a settable .lastrowid, mirroring
    sqlite3's cursor attribute. psycopg2's own .lastrowid always reads as
    None (it's derived from table OIDs, which Postgres tables don't have
    by default), so every INSERT call site across this app that reads
    cur.lastrowid needs this populated some other way — see _Connection.execute."""

    def __init__(self, cur):
        self._cur = cur
        self.lastrowid = None

    def __getattr__(self, name):
        return getattr(self._cur, name)

    def __iter__(self):
        return iter(self._cur)


class _Connection(psycopg2.extensions.connection):
    """A psycopg2 connection with an sqlite3-style .execute() shortcut, so
    the rest of the app (written against sqlite3's conn.execute(...) API)
    doesn't need touching. Translates '?' placeholders to psycopg2's '%s',
    sqlite's SELECT last_insert_rowid() to Postgres's SELECT lastval()
    (both are per-connection/session, so the semantics match as long as the
    call happens on the same connection right after the INSERT, which is
    how every call site here uses it), and auto-appends RETURNING id to
    INSERT statements so cur.lastrowid (see _Cursor above) works the way
    every call site expects. Every table in this schema has an `id`
    primary key and every INSERT here inserts a single row, so this is
    safe to do unconditionally rather than needing per-call-site changes."""

    def execute(self, sql, params=None):
        if sql.strip().rstrip(";").lower() == "select last_insert_rowid()":
            sql = "SELECT lastval()"
        else:
            sql = sql.replace("?", "%s")

        is_insert = _INSERT_RE.match(sql) and "returning" not in sql.lower()
        if is_insert:
            sql = sql + " RETURNING id"

        raw_cur = self.cursor(cursor_factory=psycopg2.extras.DictCursor)
        raw_cur.execute(sql, params)

        cur = _Cursor(raw_cur)
        if is_insert:
            row = raw_cur.fetchone()
            if row is not None:
                cur.lastrowid = row[0]
        return cur


def get_db():
    conn = psycopg2.connect(DATABASE_URL, connection_factory=_Connection)
    return conn


def _column_names(conn, table):
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
        (table,),
    ).fetchall()
    return [r["column_name"] for r in rows]


def _add_column_if_missing(conn, table, column, ddl):
    """ddl example: 'INTEGER DEFAULT 0' """
    if column not in _column_names(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        conn.commit()


def init_db():
    conn = get_db()

    # ── Inventory catalog ────────────────────────────────
    conn.execute("""
    CREATE TABLE IF NOT EXISTS inventory (
        id          SERIAL PRIMARY KEY,
        part_name   TEXT NOT NULL,
        material    TEXT,
        unit        TEXT DEFAULT 'pcs',
        quantity    INTEGER DEFAULT 0,
        reorder_at  INTEGER DEFAULT 10,
        rfid_tag    TEXT UNIQUE,
        barcode     TEXT UNIQUE,
        created_at  TEXT
    )
    """)

    # ── Orders — always tied to an inventory item ────────
    conn.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id              SERIAL PRIMARY KEY,
        inventory_id    INTEGER,
        part_name       TEXT NOT NULL,
        material        TEXT,
        quantity        INTEGER,
        specs           TEXT,
        deadline        TEXT,
        status          TEXT DEFAULT 'Received',
        created_at      TEXT,
        FOREIGN KEY(inventory_id) REFERENCES inventory(id)
    )
    """)

    # ── Quality logs ─────────────────────────────────────
    conn.execute("""
    CREATE TABLE IF NOT EXISTS quality_logs (
        id          SERIAL PRIMARY KEY,
        order_id    INTEGER,
        note        TEXT,
        log_time    TEXT,
        FOREIGN KEY(order_id) REFERENCES orders(id)
    )
    """)

    # ── Scan events (RFID / barcode) ─────────────────────
    conn.execute("""
    CREATE TABLE IF NOT EXISTS scan_events (
        id              SERIAL PRIMARY KEY,
        inventory_id    INTEGER,
        scan_type       TEXT,
        tag_value       TEXT,
        qty_consumed    INTEGER DEFAULT 1,
        order_triggered INTEGER DEFAULT 0,
        scanned_at      TEXT,
        FOREIGN KEY(inventory_id) REFERENCES inventory(id)
    )
    """)

    # ── Production plans (demand management) ─────────────
    conn.execute("""
    CREATE TABLE IF NOT EXISTS production_plans (
        id           SERIAL PRIMARY KEY,
        inventory_id INTEGER,
        part_name    TEXT NOT NULL,
        target_qty   INTEGER NOT NULL,
        start_date   TEXT,
        end_date     TEXT,
        notes        TEXT,
        status       TEXT DEFAULT 'Planned',
        created_at   TEXT,
        FOREIGN KEY(inventory_id) REFERENCES inventory(id)
    )
    """)

    # ── Suppliers (demand management) ─────────────────────
    conn.execute("""
    CREATE TABLE IF NOT EXISTS suppliers (
        id           SERIAL PRIMARY KEY,
        name         TEXT NOT NULL,
        inventory_id INTEGER,
        part_name    TEXT,
        lead_days    INTEGER DEFAULT 7,
        reliability  INTEGER DEFAULT 90,
        contact      TEXT,
        notes        TEXT,
        created_at   TEXT,
        FOREIGN KEY(inventory_id) REFERENCES inventory(id)
    )
    """)

    # ── Users & roles ──────────────────────────────────────
    conn.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id            SERIAL PRIMARY KEY,
        username      TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        role          TEXT NOT NULL DEFAULT 'operator',
        created_at    TEXT
    )
    """)

    # ── Audit log ──────────────────────────────────────────
    conn.execute("""
    CREATE TABLE IF NOT EXISTS audit_log (
        id          SERIAL PRIMARY KEY,
        username    TEXT,
        action      TEXT NOT NULL,
        entity_type TEXT,
        entity_id   INTEGER,
        details     TEXT,
        created_at  TEXT
    )
    """)

    # ── Purchase orders (to suppliers — distinct from sales `orders`) ──
    conn.execute("""
    CREATE TABLE IF NOT EXISTS purchase_orders (
        id             SERIAL PRIMARY KEY,
        supplier_id    INTEGER,
        inventory_id   INTEGER,
        part_name      TEXT NOT NULL,
        quantity       INTEGER NOT NULL,
        unit_cost      REAL DEFAULT 0,
        total_cost     REAL DEFAULT 0,
        status         TEXT DEFAULT 'pending_approval',
        auto_generated INTEGER DEFAULT 0,
        received_qty   INTEGER DEFAULT 0,
        created_by     TEXT,
        approved_by    TEXT,
        created_at     TEXT,
        updated_at     TEXT,
        FOREIGN KEY(supplier_id) REFERENCES suppliers(id),
        FOREIGN KEY(inventory_id) REFERENCES inventory(id)
    )
    """)

    # ── Payments (covers both outgoing-to-supplier and incoming-from-customer) ──
    conn.execute("""
    CREATE TABLE IF NOT EXISTS payments (
        id                  SERIAL PRIMARY KEY,
        direction           TEXT NOT NULL,
        reference_type      TEXT NOT NULL,
        reference_id        INTEGER NOT NULL,
        amount              REAL NOT NULL,
        currency            TEXT DEFAULT 'usd',
        provider            TEXT DEFAULT 'demo',
        provider_payment_id TEXT,
        status              TEXT DEFAULT 'pending',
        created_by          TEXT,
        created_at          TEXT,
        updated_at          TEXT
    )
    """)

    # ── Bill of materials: finished inventory item -> component inventory items ──
    conn.execute("""
    CREATE TABLE IF NOT EXISTS bom (
        id                    SERIAL PRIMARY KEY,
        parent_inventory_id   INTEGER NOT NULL,
        component_inventory_id INTEGER NOT NULL,
        qty_per_unit          REAL NOT NULL DEFAULT 1,
        created_at            TEXT,
        FOREIGN KEY(parent_inventory_id) REFERENCES inventory(id),
        FOREIGN KEY(component_inventory_id) REFERENCES inventory(id)
    )
    """)

    # ── Machines / work centers ─────────────────────────────
    conn.execute("""
    CREATE TABLE IF NOT EXISTS machines (
        id          SERIAL PRIMARY KEY,
        name        TEXT NOT NULL,
        location    TEXT,
        status      TEXT DEFAULT 'running',
        created_at  TEXT
    )
    """)

    # ── Work orders (shop-floor production jobs) ───────────
    conn.execute("""
    CREATE TABLE IF NOT EXISTS work_orders (
        id              SERIAL PRIMARY KEY,
        inventory_id    INTEGER,
        part_name       TEXT NOT NULL,
        quantity        INTEGER NOT NULL,
        machine_id      INTEGER,
        status          TEXT DEFAULT 'planned',
        started_at      TEXT,
        completed_at    TEXT,
        notes           TEXT,
        created_at      TEXT,
        FOREIGN KEY(inventory_id) REFERENCES inventory(id),
        FOREIGN KEY(machine_id) REFERENCES machines(id)
    )
    """)

    # ── Machine downtime log ────────────────────────────────
    conn.execute("""
    CREATE TABLE IF NOT EXISTS machine_downtime (
        id          SERIAL PRIMARY KEY,
        machine_id  INTEGER NOT NULL,
        reason      TEXT,
        started_at  TEXT,
        ended_at    TEXT,
        FOREIGN KEY(machine_id) REFERENCES machines(id)
    )
    """)

    # ── Warehouses / locations ───────────────────────────────
    conn.execute("""
    CREATE TABLE IF NOT EXISTS warehouses (
        id          SERIAL PRIMARY KEY,
        name        TEXT NOT NULL,
        address     TEXT,
        created_at  TEXT
    )
    """)

    # ── Per-warehouse stock split (optional — inventory.quantity remains the
    #    single-location default / grand total when this table is unused) ────
    conn.execute("""
    CREATE TABLE IF NOT EXISTS inventory_locations (
        id            SERIAL PRIMARY KEY,
        inventory_id  INTEGER NOT NULL,
        warehouse_id  INTEGER NOT NULL,
        quantity      INTEGER DEFAULT 0,
        reorder_at    INTEGER DEFAULT 10,
        FOREIGN KEY(inventory_id) REFERENCES inventory(id),
        FOREIGN KEY(warehouse_id) REFERENCES warehouses(id),
        UNIQUE(inventory_id, warehouse_id)
    )
    """)

    # ── Notification settings + delivery log ─────────────────
    conn.execute("""
    CREATE TABLE IF NOT EXISTS notification_settings (
        id          SERIAL PRIMARY KEY,
        channel     TEXT NOT NULL,
        target      TEXT,
        event_type  TEXT NOT NULL,
        enabled     INTEGER DEFAULT 1,
        created_at  TEXT
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS notification_log (
        id          SERIAL PRIMARY KEY,
        channel     TEXT NOT NULL,
        event_type  TEXT,
        recipient   TEXT,
        message     TEXT,
        status      TEXT,
        created_at  TEXT
    )
    """)

    # ── Saved dashboard views/filters ─────────────────────────
    conn.execute("""
    CREATE TABLE IF NOT EXISTS saved_views (
        id          SERIAL PRIMARY KEY,
        username    TEXT,
        view_name   TEXT NOT NULL,
        view_type   TEXT,
        filter_json TEXT,
        created_at  TEXT
    )
    """)

    # ── Outbound webhooks (ERP/accounting integration) ─────────
    conn.execute("""
    CREATE TABLE IF NOT EXISTS webhooks (
        id          SERIAL PRIMARY KEY,
        url         TEXT NOT NULL,
        event_type  TEXT NOT NULL,
        secret      TEXT,
        enabled     INTEGER DEFAULT 1,
        created_at  TEXT
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS webhook_log (
        id              SERIAL PRIMARY KEY,
        webhook_id      INTEGER,
        event_type      TEXT,
        status_code     INTEGER,
        error           TEXT,
        created_at      TEXT,
        FOREIGN KEY(webhook_id) REFERENCES webhooks(id)
    )
    """)

    conn.commit()

    # ── Additive migrations for existing installs ─────────────
    _add_column_if_missing(conn, "orders", "inventory_id", "INTEGER")
    _add_column_if_missing(conn, "suppliers", "unit_cost", "REAL DEFAULT 0")
    _add_column_if_missing(conn, "inventory", "warehouse_id", "INTEGER")

    # quality_logs started as a single free-text `note` (still used by the
    # chat command "Log quality for order #42: ..."). These add structured
    # fields for the Quality dashboard without touching that existing path.
    _add_column_if_missing(conn, "quality_logs", "result", "TEXT")
    _add_column_if_missing(conn, "quality_logs", "defect_category", "TEXT")
    _add_column_if_missing(conn, "quality_logs", "corrective_action", "TEXT")
    _add_column_if_missing(conn, "quality_logs", "logged_by", "TEXT")

    # orders never recorded when a status change happened, only when the
    # order was first created — so "how long did this order take?" was
    # unanswerable. Every status-changing write (REST + chat) now stamps
    # this; NULL simply means "hasn't changed since creation yet" for
    # older rows, which reporting treats as no fulfillment data rather
    # than guessing.
    _add_column_if_missing(conn, "orders", "updated_at", "TEXT")

    # Profile view shows "last login"; NULL just means "never logged in
    # since this column existed" for pre-existing accounts.
    _add_column_if_missing(conn, "users", "last_login", "TEXT")

    # Cash-on-delivery vs prepaid, chosen at order time. Existing/chat-created
    # orders default to 'cod' since there's no upfront charge to reconcile.
    _add_column_if_missing(conn, "orders", "payment_method", "TEXT DEFAULT 'cod'")

    # Links a refund payments row back to the payment it reverses, so a
    # payment can be checked for "already refunded" without guessing from
    # amount/timing alone.
    _add_column_if_missing(conn, "payments", "refund_of_payment_id", "INTEGER")

    # Set when a "selling fast + stock under threshold" alert has already
    # fired for this item, so it doesn't refire on every stock-reducing
    # action while still under threshold. Cleared once stock recovers.
    _add_column_if_missing(conn, "inventory", "fast_sell_alerted_at", "TEXT")

    conn.close()
