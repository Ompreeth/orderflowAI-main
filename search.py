"""Global search across orders, inventory, and suppliers — one box instead
of knowing which tab a thing lives in. Simple LIKE matching (no FTS index):
this app's tables are small enough that it doesn't matter, and it works
identically on every SQLite install with zero extra setup."""

from flask import Blueprint, request, jsonify

from database import get_db

search_bp = Blueprint("search", __name__)


@search_bp.route("/api/search")
def search():
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"orders": [], "inventory": [], "suppliers": []})

    like = f"%{q}%"
    conn = get_db()

    orders = conn.execute("""
        SELECT id, part_name, material, status FROM orders
        WHERE part_name LIKE ? OR material LIKE ? OR specs LIKE ?
        ORDER BY id DESC LIMIT 8
    """, (like, like, like)).fetchall()

    inventory = conn.execute("""
        SELECT id, part_name, material, quantity FROM inventory
        WHERE part_name LIKE ? OR material LIKE ? OR barcode LIKE ? OR rfid_tag LIKE ?
        ORDER BY part_name LIMIT 8
    """, (like, like, like, like)).fetchall()

    suppliers = conn.execute("""
        SELECT id, name, part_name, inventory_id FROM suppliers
        WHERE name LIKE ? OR part_name LIKE ?
        ORDER BY name LIMIT 8
    """, (like, like)).fetchall()

    conn.close()
    return jsonify({
        "orders": [dict(r) for r in orders],
        "inventory": [dict(r) for r in inventory],
        "suppliers": [dict(r) for r in suppliers],
    })
