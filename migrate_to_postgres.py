"""
One-time data migration: copy everything out of the old local orders.db
(SQLite) into the Postgres database pointed at by DATABASE_URL.

Usage (run once, after setting DATABASE_URL and before using the app for
real, from the folder that contains orders.db):

    python migrate_to_postgres.py

Safe to re-run: each table is copied with explicit ids and INSERT ... ON
CONFLICT (id) DO NOTHING, so re-running just skips rows already migrated.
Tables are copied in FK-dependency order so foreign keys never point at a
row that doesn't exist yet.
"""

import sqlite3
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from database import get_db, init_db

SQLITE_PATH = "orders.db"

# Parent tables before the tables that reference them.
TABLE_ORDER = [
    "warehouses",
    "machines",
    "inventory",
    "users",
    "suppliers",
    "orders",
    "bom",
    "work_orders",
    "machine_downtime",
    "inventory_locations",
    "quality_logs",
    "scan_events",
    "production_plans",
    "purchase_orders",
    "payments",
    "audit_log",
    "notification_settings",
    "notification_log",
    "saved_views",
    "webhooks",
    "webhook_log",
]


def migrate():
    try:
        src = sqlite3.connect(SQLITE_PATH)
        src.row_factory = sqlite3.Row
    except sqlite3.Error as e:
        sys.exit(f"Could not open {SQLITE_PATH}: {e}")

    print("Ensuring Postgres schema exists...")
    init_db()
    dst = get_db()

    for table in TABLE_ORDER:
        rows = src.execute(f"SELECT * FROM {table}").fetchall()
        if not rows:
            continue

        cols = rows[0].keys()
        col_list = ", ".join(cols)
        placeholders = ", ".join("?" for _ in cols)

        copied = 0
        for row in rows:
            dst.execute(
                f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
                f"ON CONFLICT (id) DO NOTHING",
                tuple(row[c] for c in cols),
            )
            copied += 1
        dst.commit()

        # Keep the SERIAL sequence in sync so future inserts don't collide
        # with the ids we just migrated.
        dst.execute(
            "SELECT setval(pg_get_serial_sequence(?, 'id'), "
            "COALESCE((SELECT MAX(id) FROM " + table + "), 1))",
            (table,),
        )
        dst.commit()

        print(f"  {table}: {copied} row(s)")

    dst.close()
    src.close()
    print("Migration complete.")


if __name__ == "__main__":
    migrate()
