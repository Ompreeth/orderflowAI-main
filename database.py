import sqlite3
from datetime import datetime

DB_NAME = "orders.db"


def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def _column_names(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


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
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
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
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
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
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id    INTEGER,
        note        TEXT,
        log_time    TEXT,
        FOREIGN KEY(order_id) REFERENCES orders(id)
    )
    """)

    # ── Scan events (RFID / barcode) ─────────────────────
    conn.execute("""
    CREATE TABLE IF NOT EXISTS scan_events (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
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
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
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
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
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
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        username      TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        role          TEXT NOT NULL DEFAULT 'operator',
        created_at    TEXT
    )
    """)

    # ── Audit log ──────────────────────────────────────────
    conn.execute("""
    CREATE TABLE IF NOT EXISTS audit_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
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
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
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
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
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
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        parent_inventory_id   INTEGER NOT NULL,
        component_inventory_id INTEGER NOT NULL,
        qty_per_unit          REAL NOT NULL DEFAULT 1,
        created_at            TEXT,
        FOREIGN KEY(parent_inventory_id) REFERENCES inventory(id),
        FOREIGN KEY(component_inventory_id) REFERENCES inventory(id)
    )
    """)

    # ── Work orders (shop-floor production jobs) ───────────
    conn.execute("""
    CREATE TABLE IF NOT EXISTS work_orders (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
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

    # ── Machines / work centers ─────────────────────────────
    conn.execute("""
    CREATE TABLE IF NOT EXISTS machines (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL,
        location    TEXT,
        status      TEXT DEFAULT 'running',
        created_at  TEXT
    )
    """)

    # ── Machine downtime log ────────────────────────────────
    conn.execute("""
    CREATE TABLE IF NOT EXISTS machine_downtime (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
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
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL,
        address     TEXT,
        created_at  TEXT
    )
    """)

    # ── Per-warehouse stock split (optional — inventory.quantity remains the
    #    single-location default / grand total when this table is unused) ────
    conn.execute("""
    CREATE TABLE IF NOT EXISTS inventory_locations (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
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
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        channel     TEXT NOT NULL,
        target      TEXT,
        event_type  TEXT NOT NULL,
        enabled     INTEGER DEFAULT 1,
        created_at  TEXT
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS notification_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
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
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
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
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        url         TEXT NOT NULL,
        event_type  TEXT NOT NULL,
        secret      TEXT,
        enabled     INTEGER DEFAULT 1,
        created_at  TEXT
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS webhook_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
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

    conn.close()
