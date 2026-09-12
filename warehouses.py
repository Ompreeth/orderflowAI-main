"""
Multi-location inventory: more than one physical warehouse/location holding
stock for the same inventory item, instead of one shared pool.

Design choice — keeping this additive rather than a breaking refactor:
`inventory.quantity` stays the single source of truth every existing query
in the app already reads (dashboard stats, reorder triggers, valuation,
ABC analysis, RFID/barcode scans, chat, order fulfillment, BOM/work-order
consumption). This module never overwrites that total — `inventory_locations`
is a *sub-allocation* of it: "of the N units we have, M sit at Warehouse A."

Earlier version of this module recomputed `inventory.quantity` as
SUM(inventory_locations.quantity) after every location write. That's wrong
whenever some stock hasn't been assigned to a location yet — as soon as you
earmark 50 of your 102 units to a warehouse, it would silently overwrite the
total down to 50, destroying the other 52. Fixed: assigning stock to a
location now validates against the *unassigned* remainder
(inventory.quantity − stock already assigned elsewhere) and never touches
inventory.quantity. Any stock not yet assigned to a location is simply
"unassigned" — still counted in the item's total, just not yet placed —
and `GET /api/inventory-locations/summary/<id>` reports it explicitly so
the UI can show it and let someone allocate it.
"""

from datetime import datetime

from flask import Blueprint, request, jsonify

from database import get_db
from auth import log_audit

warehouses_bp = Blueprint("warehouses", __name__)


@warehouses_bp.route("/api/warehouses", methods=["GET"])
def list_warehouses():
    conn = get_db()
    rows = conn.execute("SELECT * FROM warehouses ORDER BY name").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@warehouses_bp.route("/api/warehouses", methods=["POST"])
def create_warehouse():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    address = (data.get("address") or "").strip() or None
    if not name:
        return jsonify({"message": "❌ name is required"}), 400

    conn = get_db()
    cur = conn.execute("INSERT INTO warehouses (name, address, created_at) VALUES (?, ?, ?)",
                        (name, address, datetime.now().isoformat()))
    conn.commit()
    wh_id = cur.lastrowid
    conn.close()
    log_audit("create_warehouse", "warehouse", wh_id, name)
    return jsonify({"message": f"✅ Warehouse '{name}' added", "id": wh_id})


@warehouses_bp.route("/api/warehouses/<int:wh_id>", methods=["DELETE"])
def delete_warehouse(wh_id):
    conn = get_db()
    in_use = conn.execute(
        "SELECT COALESCE(SUM(quantity), 0) FROM inventory_locations WHERE warehouse_id = ?", (wh_id,)
    ).fetchone()[0]
    if in_use:
        conn.close()
        return jsonify({"message": f"❌ Cannot delete — {in_use} units of stock are still assigned here. Transfer them out first."}), 400
    conn.execute("DELETE FROM inventory_locations WHERE warehouse_id = ?", (wh_id,))
    conn.execute("DELETE FROM warehouses WHERE id = ?", (wh_id,))
    conn.commit()
    conn.close()
    log_audit("delete_warehouse", "warehouse", wh_id)
    return jsonify({"message": "✅ Warehouse deleted"})


@warehouses_bp.route("/api/inventory-locations", methods=["GET"])
def list_locations():
    inventory_id = request.args.get("inventory_id", type=int)
    conn = get_db()
    if inventory_id:
        rows = conn.execute("""
            SELECT il.*, w.name AS warehouse_name
            FROM inventory_locations il JOIN warehouses w ON w.id = il.warehouse_id
            WHERE il.inventory_id = ? ORDER BY w.name
        """, (inventory_id,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT il.*, w.name AS warehouse_name, i.part_name
            FROM inventory_locations il
            JOIN warehouses w ON w.id = il.warehouse_id
            JOIN inventory i ON i.id = il.inventory_id
            ORDER BY i.part_name, w.name
        """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@warehouses_bp.route("/api/inventory-locations/summary/<int:inventory_id>", methods=["GET"])
def location_summary(inventory_id):
    """Total vs. assigned-to-a-warehouse vs. still-unassigned, for one item."""
    conn = get_db()
    item = conn.execute("SELECT * FROM inventory WHERE id = ?", (inventory_id,)).fetchone()
    if not item:
        conn.close()
        return jsonify({"message": "❌ Inventory item not found"}), 404
    assigned = conn.execute(
        "SELECT COALESCE(SUM(quantity), 0) AS t FROM inventory_locations WHERE inventory_id = ?", (inventory_id,)
    ).fetchone()["t"]
    conn.close()
    unassigned = max(0, item["quantity"] - assigned)
    return jsonify({
        "inventory_id": inventory_id, "part_name": item["part_name"],
        "total_quantity": item["quantity"], "assigned": assigned, "unassigned": unassigned,
    })


@warehouses_bp.route("/api/inventory-locations", methods=["POST"])
def set_location():
    """Assign/update how much of an item's stock sits at one warehouse.

    This earmarks stock the item already has — it can't invent stock that
    doesn't exist. The new amount for this location, plus whatever is
    already assigned to every OTHER location for this item, can't exceed
    the item's total quantity.
    """
    data = request.get_json(silent=True) or {}
    inventory_id = data.get("inventory_id")
    warehouse_id = data.get("warehouse_id")
    quantity = data.get("quantity", 0)
    reorder_at = data.get("reorder_at", 10)

    if not inventory_id or not warehouse_id:
        return jsonify({"message": "❌ inventory_id and warehouse_id are required"}), 400
    try:
        quantity = int(quantity)
        reorder_at = int(reorder_at)
        assert quantity >= 0 and reorder_at >= 0
    except Exception:
        return jsonify({"message": "❌ quantity and reorder_at must be non-negative integers"}), 400

    conn = get_db()
    item = conn.execute("SELECT * FROM inventory WHERE id = ?", (inventory_id,)).fetchone()
    warehouse = conn.execute("SELECT * FROM warehouses WHERE id = ?", (warehouse_id,)).fetchone()
    if not item or not warehouse:
        conn.close()
        return jsonify({"message": "❌ Inventory item or warehouse not found"}), 404

    existing = conn.execute(
        "SELECT id FROM inventory_locations WHERE inventory_id = ? AND warehouse_id = ?",
        (inventory_id, warehouse_id),
    ).fetchone()

    assigned_elsewhere = conn.execute(
        "SELECT COALESCE(SUM(quantity), 0) AS t FROM inventory_locations WHERE inventory_id = ? AND warehouse_id != ?",
        (inventory_id, warehouse_id),
    ).fetchone()["t"]
    available = item["quantity"] - assigned_elsewhere
    if quantity > available:
        conn.close()
        return jsonify({
            "message": (f"❌ Only {max(available, 0)} units of {item['part_name']} are unassigned "
                        f"(total {item['quantity']}, {assigned_elsewhere} already assigned to other locations)")
        }), 400

    if existing:
        conn.execute("UPDATE inventory_locations SET quantity = ?, reorder_at = ? WHERE id = ?",
                     (quantity, reorder_at, existing["id"]))
    else:
        conn.execute("""
            INSERT INTO inventory_locations (inventory_id, warehouse_id, quantity, reorder_at)
            VALUES (?, ?, ?, ?)
        """, (inventory_id, warehouse_id, quantity, reorder_at))

    conn.commit()
    conn.close()
    log_audit("set_inventory_location", "inventory", inventory_id, f"{warehouse['name']}: {quantity}")
    return jsonify({"message": f"✅ {item['part_name']} @ {warehouse['name']}: {quantity} units"})


@warehouses_bp.route("/api/inventory-locations/transfer", methods=["POST"])
def transfer_stock():
    data = request.get_json(silent=True) or {}
    inventory_id = data.get("inventory_id")
    from_warehouse_id = data.get("from_warehouse_id")
    to_warehouse_id = data.get("to_warehouse_id")
    quantity = data.get("quantity")

    if not all([inventory_id, from_warehouse_id, to_warehouse_id, quantity]):
        return jsonify({"message": "❌ inventory_id, from_warehouse_id, to_warehouse_id, and quantity are all required"}), 400
    if from_warehouse_id == to_warehouse_id:
        return jsonify({"message": "❌ Source and destination warehouses must be different"}), 400
    try:
        quantity = int(quantity)
        assert quantity > 0
    except Exception:
        return jsonify({"message": "❌ quantity must be a positive integer"}), 400

    conn = get_db()
    source = conn.execute(
        "SELECT * FROM inventory_locations WHERE inventory_id = ? AND warehouse_id = ?",
        (inventory_id, from_warehouse_id),
    ).fetchone()
    if not source or source["quantity"] < quantity:
        conn.close()
        have = source["quantity"] if source else 0
        return jsonify({"message": f"❌ Only {have} units available at the source warehouse"}), 400

    dest = conn.execute(
        "SELECT * FROM inventory_locations WHERE inventory_id = ? AND warehouse_id = ?",
        (inventory_id, to_warehouse_id),
    ).fetchone()

    conn.execute("UPDATE inventory_locations SET quantity = quantity - ? WHERE id = ?", (quantity, source["id"]))
    if dest:
        conn.execute("UPDATE inventory_locations SET quantity = quantity + ? WHERE id = ?", (quantity, dest["id"]))
    else:
        conn.execute("""
            INSERT INTO inventory_locations (inventory_id, warehouse_id, quantity, reorder_at)
            VALUES (?, ?, ?, ?)
        """, (inventory_id, to_warehouse_id, quantity, source["reorder_at"]))

    # Note: a transfer moves stock between two locations without changing
    # the item's total — inventory.quantity is untouched here, same as
    # everywhere else in this module. See module docstring.
    conn.commit()
    conn.close()
    log_audit("transfer_stock", "inventory", inventory_id, f"{quantity} units: wh#{from_warehouse_id} -> wh#{to_warehouse_id}")
    return jsonify({"message": f"✅ Transferred {quantity} units"})
