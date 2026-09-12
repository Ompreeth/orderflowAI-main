"""
Production & BOM tracking: bill of materials linking a finished inventory
item to the raw-material inventory items it consumes, work orders that run
a production job against that BOM, and machine/work-center tracking with
downtime logging.

Design choices:

- Completing a work order is the one place BOM actually does something:
  it deducts every component (qty_per_unit x work order quantity) from
  inventory and adds the finished quantity, in ONE all-or-nothing check —
  if any component is short, nothing is deducted and the response lists
  exactly what's missing, rather than partially consuming stock.
- A finished item with no BOM rows on file can still run a work order
  (nothing to deduct) — BOM is optional, not a precondition for
  production, same spirit as procurement working without a supplier on
  file (it just can't auto-anything).
- Not login-gated, matching quality/procurement's day-to-day actions —
  this is shop-floor operation, not the money/user-management surface
  that requires an account.
"""

from datetime import datetime

from flask import Blueprint, request, jsonify

from database import get_db
from auth import log_audit

production_bp = Blueprint("production", __name__)

WO_STATUSES = ["planned", "in_progress", "completed", "cancelled"]


# ─────────────────────────────────────────────────────────
# Bill of materials
# ─────────────────────────────────────────────────────────

@production_bp.route("/api/bom", methods=["GET"])
def list_bom():
    parent_id = request.args.get("parent_inventory_id", type=int)
    conn = get_db()
    if parent_id:
        rows = conn.execute("""
            SELECT b.*, c.part_name AS component_name, c.quantity AS component_stock, c.unit AS component_unit
            FROM bom b JOIN inventory c ON c.id = b.component_inventory_id
            WHERE b.parent_inventory_id = ?
            ORDER BY b.id
        """, (parent_id,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT b.*, p.part_name AS parent_name, c.part_name AS component_name
            FROM bom b
            JOIN inventory p ON p.id = b.parent_inventory_id
            JOIN inventory c ON c.id = b.component_inventory_id
            ORDER BY p.part_name, b.id
        """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@production_bp.route("/api/bom", methods=["POST"])
def add_bom_line():
    data = request.get_json(silent=True) or {}
    parent_id = data.get("parent_inventory_id")
    component_id = data.get("component_inventory_id")
    qty_per_unit = data.get("qty_per_unit", 1)

    if not parent_id or not component_id:
        return jsonify({"message": "❌ parent_inventory_id and component_inventory_id are required"}), 400
    if parent_id == component_id:
        return jsonify({"message": "❌ An item can't be a component of itself"}), 400
    try:
        qty_per_unit = float(qty_per_unit)
        assert qty_per_unit > 0
    except Exception:
        return jsonify({"message": "❌ qty_per_unit must be a positive number"}), 400

    conn = get_db()
    parent = conn.execute("SELECT * FROM inventory WHERE id = ?", (parent_id,)).fetchone()
    component = conn.execute("SELECT * FROM inventory WHERE id = ?", (component_id,)).fetchone()
    if not parent or not component:
        conn.close()
        return jsonify({"message": "❌ Inventory item not found"}), 404

    existing = conn.execute(
        "SELECT id FROM bom WHERE parent_inventory_id = ? AND component_inventory_id = ?",
        (parent_id, component_id),
    ).fetchone()
    if existing:
        conn.close()
        return jsonify({"message": f"❌ {component['part_name']} is already a component of {parent['part_name']} — edit or remove it first"}), 400

    cur = conn.execute(
        "INSERT INTO bom (parent_inventory_id, component_inventory_id, qty_per_unit, created_at) VALUES (?, ?, ?, ?)",
        (parent_id, component_id, qty_per_unit, datetime.now().isoformat()),
    )
    conn.commit()
    bom_id = cur.lastrowid
    conn.close()
    log_audit("add_bom_line", "inventory", parent_id, f"+{qty_per_unit}x {component['part_name']}")
    return jsonify({"message": f"✅ {component['part_name']} added to {parent['part_name']}'s BOM ({qty_per_unit}/unit)", "id": bom_id})


@production_bp.route("/api/bom/<int:bom_id>", methods=["DELETE"])
def delete_bom_line(bom_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM bom WHERE id = ?", (bom_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"message": "Not found"}), 404
    conn.execute("DELETE FROM bom WHERE id = ?", (bom_id,))
    conn.commit()
    conn.close()
    log_audit("delete_bom_line", "inventory", row["parent_inventory_id"])
    return jsonify({"message": "✅ BOM line removed"})


# ─────────────────────────────────────────────────────────
# Work orders
# ─────────────────────────────────────────────────────────

_WO_SELECT = """
    SELECT wo.*, m.name AS machine_name
    FROM work_orders wo
    LEFT JOIN machines m ON m.id = wo.machine_id
"""


@production_bp.route("/api/work-orders", methods=["GET"])
def list_work_orders():
    status = request.args.get("status")
    conn = get_db()
    if status:
        rows = conn.execute(_WO_SELECT + " WHERE wo.status = ? ORDER BY wo.id DESC", (status,)).fetchall()
    else:
        rows = conn.execute(_WO_SELECT + " ORDER BY wo.id DESC").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@production_bp.route("/api/work-orders", methods=["POST"])
def create_work_order():
    data = request.get_json(silent=True) or {}
    inventory_id = data.get("inventory_id")
    quantity = data.get("quantity")
    machine_id = data.get("machine_id") or None
    notes = (data.get("notes") or "").strip() or None

    if not inventory_id or not quantity:
        return jsonify({"message": "❌ inventory_id and quantity are required"}), 400
    try:
        quantity = int(quantity)
        assert quantity > 0
    except Exception:
        return jsonify({"message": "❌ quantity must be a positive integer"}), 400

    conn = get_db()
    item = conn.execute("SELECT * FROM inventory WHERE id = ?", (inventory_id,)).fetchone()
    if not item:
        conn.close()
        return jsonify({"message": "❌ Inventory item not found"}), 404
    if machine_id:
        machine = conn.execute("SELECT * FROM machines WHERE id = ?", (machine_id,)).fetchone()
        if not machine:
            conn.close()
            return jsonify({"message": "❌ Machine not found"}), 404

    now = datetime.now().isoformat()
    cur = conn.execute("""
        INSERT INTO work_orders (inventory_id, part_name, quantity, machine_id, status, notes, created_at)
        VALUES (?, ?, ?, ?, 'planned', ?, ?)
    """, (inventory_id, item["part_name"], quantity, machine_id, notes, now))
    conn.commit()
    wo_id = cur.lastrowid
    conn.close()
    log_audit("create_work_order", "work_order", wo_id, f"{quantity}x {item['part_name']}")
    return jsonify({"message": f"✅ Work order #{wo_id} planned: {quantity}x {item['part_name']}", "id": wo_id})


@production_bp.route("/api/work-orders/<int:wo_id>/start", methods=["POST"])
def start_work_order(wo_id):
    conn = get_db()
    wo = conn.execute("SELECT * FROM work_orders WHERE id = ?", (wo_id,)).fetchone()
    if not wo:
        conn.close()
        return jsonify({"message": "Not found"}), 404
    if wo["status"] != "planned":
        conn.close()
        return jsonify({"message": f"❌ Work order is '{wo['status']}', not 'planned'"}), 400
    if wo["machine_id"]:
        machine = conn.execute("SELECT * FROM machines WHERE id = ?", (wo["machine_id"],)).fetchone()
        if machine and machine["status"] == "down":
            conn.close()
            return jsonify({"message": f"❌ {machine['name']} is marked down — resolve its downtime first"}), 400

    conn.execute("UPDATE work_orders SET status='in_progress', started_at=? WHERE id=?",
                 (datetime.now().isoformat(), wo_id))
    conn.commit()
    conn.close()
    log_audit("start_work_order", "work_order", wo_id)
    return jsonify({"message": f"✅ Work order #{wo_id} started"})


@production_bp.route("/api/work-orders/<int:wo_id>/complete", methods=["POST"])
def complete_work_order(wo_id):
    """The one place BOM does something: deduct every component (scaled by
    the work order's quantity), then add the finished quantity. All-or-
    nothing — if any component is short, nothing is touched."""
    conn = get_db()
    wo = conn.execute("SELECT * FROM work_orders WHERE id = ?", (wo_id,)).fetchone()
    if not wo:
        conn.close()
        return jsonify({"message": "Not found"}), 404
    if wo["status"] != "in_progress":
        conn.close()
        return jsonify({"message": f"❌ Work order is '{wo['status']}', not 'in_progress'"}), 400

    bom_rows = conn.execute("""
        SELECT b.*, c.part_name AS component_name, c.quantity AS component_stock
        FROM bom b JOIN inventory c ON c.id = b.component_inventory_id
        WHERE b.parent_inventory_id = ?
    """, (wo["inventory_id"],)).fetchall()

    shortages = []
    for b in bom_rows:
        needed = b["qty_per_unit"] * wo["quantity"]
        if b["component_stock"] < needed:
            shortages.append(f"{b['component_name']}: need {needed}, have {b['component_stock']}")
    if shortages:
        conn.close()
        return jsonify({"message": "❌ Not enough stock to complete this run — " + "; ".join(shortages)}), 400

    for b in bom_rows:
        needed = b["qty_per_unit"] * wo["quantity"]
        conn.execute("UPDATE inventory SET quantity = quantity - ? WHERE id = ?",
                     (needed, b["component_inventory_id"]))

    conn.execute("UPDATE inventory SET quantity = quantity + ? WHERE id = ?", (wo["quantity"], wo["inventory_id"]))
    conn.execute("UPDATE work_orders SET status='completed', completed_at=? WHERE id=?",
                 (datetime.now().isoformat(), wo_id))
    conn.commit()
    conn.close()
    log_audit("complete_work_order", "work_order", wo_id,
              f"+{wo['quantity']} {wo['part_name']}, {len(bom_rows)} component(s) consumed")
    return jsonify({"message": f"✅ Work order #{wo_id} completed — {wo['quantity']}x {wo['part_name']} added to stock"})


@production_bp.route("/api/work-orders/<int:wo_id>/cancel", methods=["POST"])
def cancel_work_order(wo_id):
    conn = get_db()
    wo = conn.execute("SELECT * FROM work_orders WHERE id = ?", (wo_id,)).fetchone()
    if not wo:
        conn.close()
        return jsonify({"message": "Not found"}), 404
    if wo["status"] == "completed":
        conn.close()
        return jsonify({"message": "❌ Cannot cancel a completed work order"}), 400
    conn.execute("UPDATE work_orders SET status='cancelled' WHERE id=?", (wo_id,))
    conn.commit()
    conn.close()
    log_audit("cancel_work_order", "work_order", wo_id)
    return jsonify({"message": f"✅ Work order #{wo_id} cancelled"})


# ─────────────────────────────────────────────────────────
# Machines & downtime
# ─────────────────────────────────────────────────────────

@production_bp.route("/api/machines", methods=["GET"])
def list_machines():
    conn = get_db()
    rows = conn.execute("SELECT * FROM machines ORDER BY name").fetchall()
    out = []
    for m in rows:
        d = dict(m)
        open_dt = conn.execute(
            "SELECT * FROM machine_downtime WHERE machine_id = ? AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
            (m["id"],),
        ).fetchone()
        d["open_downtime"] = dict(open_dt) if open_dt else None
        out.append(d)
    conn.close()
    return jsonify(out)


@production_bp.route("/api/machines", methods=["POST"])
def create_machine():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    location = (data.get("location") or "").strip() or None
    if not name:
        return jsonify({"message": "❌ name is required"}), 400

    conn = get_db()
    cur = conn.execute("INSERT INTO machines (name, location, status, created_at) VALUES (?, ?, 'running', ?)",
                        (name, location, datetime.now().isoformat()))
    conn.commit()
    machine_id = cur.lastrowid
    conn.close()
    log_audit("create_machine", "machine", machine_id, name)
    return jsonify({"message": f"✅ Machine '{name}' added", "id": machine_id})


@production_bp.route("/api/machines/<int:machine_id>", methods=["DELETE"])
def delete_machine(machine_id):
    conn = get_db()
    in_use = conn.execute(
        "SELECT COUNT(*) FROM work_orders WHERE machine_id = ? AND status IN ('planned','in_progress')",
        (machine_id,),
    ).fetchone()[0]
    if in_use:
        conn.close()
        return jsonify({"message": f"❌ Cannot delete — {in_use} active work order(s) reference this machine"}), 400
    conn.execute("DELETE FROM machines WHERE id = ?", (machine_id,))
    conn.commit()
    conn.close()
    log_audit("delete_machine", "machine", machine_id)
    return jsonify({"message": "✅ Machine deleted"})


@production_bp.route("/api/machines/<int:machine_id>/downtime", methods=["POST"])
def log_downtime(machine_id):
    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip() or "Unspecified"

    conn = get_db()
    machine = conn.execute("SELECT * FROM machines WHERE id = ?", (machine_id,)).fetchone()
    if not machine:
        conn.close()
        return jsonify({"message": "Not found"}), 404
    if machine["status"] == "down":
        conn.close()
        return jsonify({"message": f"❌ {machine['name']} is already marked down"}), 400

    now = datetime.now().isoformat()
    cur = conn.execute("INSERT INTO machine_downtime (machine_id, reason, started_at) VALUES (?, ?, ?)",
                        (machine_id, reason, now))
    conn.execute("UPDATE machines SET status='down' WHERE id=?", (machine_id,))
    conn.commit()
    downtime_id = cur.lastrowid
    conn.close()
    log_audit("log_downtime", "machine", machine_id, reason)
    return jsonify({"message": f"⚠️ {machine['name']} marked down: {reason}", "id": downtime_id})


@production_bp.route("/api/machine-downtime/<int:downtime_id>/resolve", methods=["POST"])
def resolve_downtime(downtime_id):
    conn = get_db()
    dt = conn.execute("SELECT * FROM machine_downtime WHERE id = ?", (downtime_id,)).fetchone()
    if not dt:
        conn.close()
        return jsonify({"message": "Not found"}), 404
    if dt["ended_at"]:
        conn.close()
        return jsonify({"message": "❌ Already resolved"}), 400

    now = datetime.now().isoformat()
    conn.execute("UPDATE machine_downtime SET ended_at=? WHERE id=?", (now, downtime_id))
    conn.execute("UPDATE machines SET status='running' WHERE id=?", (dt["machine_id"],))
    conn.commit()
    conn.close()
    log_audit("resolve_downtime", "machine", dt["machine_id"])
    return jsonify({"message": "✅ Machine back to running"})
