"""
Procurement automation: purchase orders TO suppliers, distinct from the
existing `orders` table (which represents demand/production orders FOR
manufactured parts). A purchase order buys raw materials to replenish stock;
a sales/production order consumes it.

Approval workflow: a PO above PO_APPROVAL_THRESHOLD (env var, default $500)
starts as 'pending_approval' and needs an explicit approve step; smaller
ones are auto-approved on creation, matching how most small shops actually
work (nobody needs sign-off to reorder $40 of bolts).

Status flow: pending_approval -> approved -> sent -> partially_received /
received. cancel is available from any non-received state.
"""

import os
from datetime import datetime

from flask import Blueprint, request, jsonify, session

from database import get_db
from auth import login_required, role_required, log_audit

procurement_bp = Blueprint("procurement", __name__)

PO_APPROVAL_THRESHOLD = float(os.environ.get("PO_APPROVAL_THRESHOLD", "500"))


def _po_to_dict(row):
    return dict(row)


def _score_suppliers(suppliers):
    """Weighted score: cheaper, faster, and more reliable all score higher.
    Shared by /compare and auto-PO supplier selection so 'best supplier'
    means the same thing everywhere in the app."""
    if not suppliers:
        return []
    max_cost = max((s["unit_cost"] or 0) for s in suppliers) or 1
    max_lead = max((s["lead_days"] or 1) for s in suppliers) or 1
    scored = []
    for s in suppliers:
        d = dict(s)
        cost_score = 1 - ((d["unit_cost"] or 0) / max_cost)
        lead_score = 1 - ((d["lead_days"] or 0) / max_lead)
        rel_score  = (d["reliability"] or 0) / 100
        d["score"] = round((cost_score * 0.4 + lead_score * 0.3 + rel_score * 0.3) * 100, 1)
        scored.append(d)
    scored.sort(key=lambda s: -s["score"])
    return scored


def _best_supplier_for(conn, inventory_id):
    """Pick the best supplier for an item using the same weighted score
    shown in the supplier comparison view, so 'auto-generate' picks
    whoever a human comparing the same list would pick first."""
    rows = conn.execute("SELECT * FROM suppliers WHERE inventory_id = ?", (inventory_id,)).fetchall()
    ranked = _score_suppliers(rows)
    if not ranked:
        return None
    best = ranked[0]
    # Re-fetch as a sqlite3.Row so callers can keep using row['col'] access
    return conn.execute("SELECT * FROM suppliers WHERE id = ?", (best["id"],)).fetchone()


def _create_po_row(conn, supplier, item, quantity, auto_generated):
    unit_cost = (supplier["unit_cost"] if supplier and supplier["unit_cost"] else 0) or 0
    total_cost = round(unit_cost * quantity, 2)
    status = "pending_approval" if total_cost > PO_APPROVAL_THRESHOLD else "approved"
    now = datetime.now().isoformat()

    cur = conn.execute("""
        INSERT INTO purchase_orders
            (supplier_id, inventory_id, part_name, quantity, unit_cost, total_cost,
             status, auto_generated, received_qty, created_by, approved_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
    """, (
        supplier["id"] if supplier else None,
        item["id"], item["part_name"], quantity, unit_cost, total_cost,
        status, 1 if auto_generated else 0,
        session.get("username", "guest"),
        session.get("username", "guest") if status == "approved" else None,
        now, now,
    ))
    conn.commit()
    return cur.lastrowid, status, total_cost


# ─────────────────────────────────────────────────────────
# Supplier comparison
# ─────────────────────────────────────────────────────────

@procurement_bp.route("/api/suppliers/compare")
def compare_suppliers():
    """All suppliers for one inventory item, ranked by a simple weighted
    score: cheaper, faster, and more reliable all score higher. Weights
    are deliberately simple (no ML) so the ranking is easy to explain."""
    inventory_id = request.args.get("inventory_id", type=int)
    if not inventory_id:
        return jsonify({"message": "inventory_id is required"}), 400

    conn = get_db()
    rows = conn.execute("SELECT * FROM suppliers WHERE inventory_id = ?", (inventory_id,)).fetchall()
    conn.close()
    return jsonify(_score_suppliers(rows))


# ─────────────────────────────────────────────────────────
# Purchase orders — CRUD + workflow
# ─────────────────────────────────────────────────────────

_PO_SELECT = """
    SELECT po.*, s.name AS supplier_name
    FROM purchase_orders po
    LEFT JOIN suppliers s ON s.id = po.supplier_id
"""


@procurement_bp.route("/api/purchase-orders", methods=["GET"])
def list_purchase_orders():
    status = request.args.get("status")
    conn = get_db()
    if status:
        rows = conn.execute(_PO_SELECT + " WHERE po.status = ? ORDER BY po.id DESC", (status,)).fetchall()
    else:
        rows = conn.execute(_PO_SELECT + " ORDER BY po.id DESC").fetchall()
    conn.close()
    return jsonify([_po_to_dict(r) for r in rows])


@procurement_bp.route("/api/purchase-orders/<int:po_id>", methods=["GET"])
def get_purchase_order(po_id):
    conn = get_db()
    row = conn.execute(_PO_SELECT + " WHERE po.id = ?", (po_id,)).fetchone()
    conn.close()
    return (jsonify(_po_to_dict(row)), 200) if row else (jsonify({"message": "Not found"}), 404)


@procurement_bp.route("/api/purchase-orders", methods=["POST"])
def create_purchase_order():
    """Manually create a purchase order (a person picking the supplier),
    as opposed to /from-recommendation which picks one automatically."""
    data = request.get_json(silent=True) or {}
    inventory_id = data.get("inventory_id")
    supplier_id = data.get("supplier_id")
    quantity = data.get("quantity")

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

    supplier = None
    if supplier_id:
        supplier = conn.execute("SELECT * FROM suppliers WHERE id = ?", (supplier_id,)).fetchone()
    else:
        supplier = _best_supplier_for(conn, inventory_id)

    po_id, status, total_cost = _create_po_row(conn, supplier, item, quantity, auto_generated=False)
    conn.close()

    log_audit("create_purchase_order", "purchase_order", po_id,
              f"{quantity}x {item['part_name']} — ${total_cost}")

    supplier_note = f" from {supplier['name']}" if supplier else " (no supplier on file for this part yet)"
    return jsonify({
        "message": f"✅ Purchase order #{po_id} created{supplier_note} — status: {status}",
        "id": po_id, "status": status, "total_cost": total_cost,
    })


@procurement_bp.route("/api/purchase-orders/from-recommendation", methods=["POST"])
def create_po_from_recommendation():
    """One-click 'Create PO' from a Predictions card's reorder recommendation.
    Picks the best supplier on file automatically — this is the 'auto' in
    auto-generate; a human can always create one manually and pick a
    different supplier via /api/purchase-orders instead."""
    data = request.get_json(silent=True) or {}
    inventory_id = data.get("inventory_id")
    quantity = data.get("quantity")

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

    supplier = _best_supplier_for(conn, inventory_id)
    if not supplier:
        conn.close()
        return jsonify({"message": f"❌ No supplier on file for '{item['part_name']}' yet — add one in the Suppliers tab first"}), 400

    po_id, status, total_cost = _create_po_row(conn, supplier, item, quantity, auto_generated=True)
    conn.close()

    log_audit("auto_create_purchase_order", "purchase_order", po_id,
              f"{quantity}x {item['part_name']} from {supplier['name']} — ${total_cost}")

    return jsonify({
        "message": f"✅ Auto-generated PO #{po_id}: {quantity}x {item['part_name']} from {supplier['name']} "
                    f"(${total_cost:,.2f}, {supplier['lead_days']}d lead) — status: {status}",
        "id": po_id, "status": status, "supplier": supplier["name"], "total_cost": total_cost,
    })


@procurement_bp.route("/api/purchase-orders/<int:po_id>/approve", methods=["POST"])
@role_required("admin", "operator")
def approve_purchase_order(po_id):
    conn = get_db()
    po = conn.execute("SELECT * FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()
    if not po:
        conn.close()
        return jsonify({"message": "Not found"}), 404
    if po["status"] != "pending_approval":
        conn.close()
        return jsonify({"message": f"❌ PO is '{po['status']}', not awaiting approval"}), 400

    conn.execute(
        "UPDATE purchase_orders SET status='approved', approved_by=?, updated_at=? WHERE id=?",
        (session["username"], datetime.now().isoformat(), po_id),
    )
    conn.commit()
    conn.close()
    log_audit("approve_purchase_order", "purchase_order", po_id)
    return jsonify({"message": f"✅ PO #{po_id} approved"})


@procurement_bp.route("/api/purchase-orders/<int:po_id>/send", methods=["POST"])
def send_purchase_order(po_id):
    conn = get_db()
    po = conn.execute("SELECT * FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()
    if not po:
        conn.close()
        return jsonify({"message": "Not found"}), 404
    if po["status"] != "approved":
        conn.close()
        return jsonify({"message": f"❌ PO must be 'approved' before sending (currently '{po['status']}')"}), 400

    conn.execute("UPDATE purchase_orders SET status='sent', updated_at=? WHERE id=?",
                 (datetime.now().isoformat(), po_id))
    conn.commit()
    conn.close()
    log_audit("send_purchase_order", "purchase_order", po_id)
    return jsonify({"message": f"✅ PO #{po_id} marked sent to supplier"})


@procurement_bp.route("/api/purchase-orders/<int:po_id>/receive", methods=["POST"])
def receive_purchase_order(po_id):
    """Record goods arriving — partial or full. Actually increments
    inventory stock by whatever quantity arrived."""
    data = request.get_json(silent=True) or {}
    conn = get_db()
    po = conn.execute("SELECT * FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()
    if not po:
        conn.close()
        return jsonify({"message": "Not found"}), 404
    if po["status"] not in ("sent", "partially_received"):
        conn.close()
        return jsonify({"message": f"❌ PO must be 'sent' to receive against it (currently '{po['status']}')"}), 400

    try:
        qty_received_now = int(data.get("quantity"))
        assert qty_received_now > 0
    except Exception:
        conn.close()
        return jsonify({"message": "❌ 'quantity' (received now) must be a positive integer"}), 400

    remaining = po["quantity"] - po["received_qty"]
    if qty_received_now > remaining:
        conn.close()
        return jsonify({"message": f"❌ Only {remaining} still outstanding on this PO"}), 400

    new_received = po["received_qty"] + qty_received_now
    new_status = "received" if new_received >= po["quantity"] else "partially_received"

    conn.execute(
        "UPDATE purchase_orders SET received_qty=?, status=?, updated_at=? WHERE id=?",
        (new_received, new_status, datetime.now().isoformat(), po_id),
    )
    if po["inventory_id"]:
        conn.execute(
            "UPDATE inventory SET quantity = quantity + ? WHERE id = ?",
            (qty_received_now, po["inventory_id"]),
        )
    conn.commit()
    conn.close()

    log_audit("receive_purchase_order", "purchase_order", po_id,
              f"+{qty_received_now} (total {new_received}/{po['quantity']})")
    return jsonify({
        "message": f"✅ Received {qty_received_now} units for PO #{po_id} "
                    f"({new_received}/{po['quantity']} total) — stock updated",
        "status": new_status, "received_qty": new_received,
    })


@procurement_bp.route("/api/purchase-orders/<int:po_id>/cancel", methods=["POST"])
@role_required("admin", "operator")
def cancel_purchase_order(po_id):
    conn = get_db()
    po = conn.execute("SELECT * FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()
    if not po:
        conn.close()
        return jsonify({"message": "Not found"}), 404
    if po["status"] == "received":
        conn.close()
        return jsonify({"message": "❌ Cannot cancel a fully received PO"}), 400

    conn.execute("UPDATE purchase_orders SET status='cancelled', updated_at=? WHERE id=?",
                 (datetime.now().isoformat(), po_id))
    conn.commit()
    conn.close()
    log_audit("cancel_purchase_order", "purchase_order", po_id)
    return jsonify({"message": f"✅ PO #{po_id} cancelled"})


# ─────────────────────────────────────────────────────────
# Sales/production order editing (the existing `orders` table)
# ─────────────────────────────────────────────────────────

@procurement_bp.route("/api/orders/<int:oid>", methods=["PATCH"])
def edit_order(oid):
    """Edit quantity/deadline/specs on an existing order — previously
    orders were create-and-status-only with no way to fix a mistake
    short of leaving it wrong or deleting the whole row."""
    data = request.get_json(silent=True) or {}
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
    if not order:
        conn.close()
        return jsonify({"message": "Not found"}), 404

    quantity = data.get("quantity", order["quantity"])
    deadline = data.get("deadline", order["deadline"])
    specs = data.get("specs", order["specs"])
    try:
        if quantity is not None:
            quantity = int(quantity)
            assert quantity > 0
    except Exception:
        conn.close()
        return jsonify({"message": "❌ quantity must be a positive integer"}), 400

    conn.execute("UPDATE orders SET quantity=?, deadline=?, specs=?, updated_at=? WHERE id=?",
                 (quantity, deadline, specs, datetime.now().isoformat(), oid))
    conn.commit()
    conn.close()
    log_audit("edit_order", "order", oid, f"qty={quantity} deadline={deadline}")
    return jsonify({"message": f"✅ Order #{oid} updated"})


@procurement_bp.route("/api/orders/<int:oid>/cancel", methods=["POST"])
def cancel_order(oid):
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
    if not order:
        conn.close()
        return jsonify({"message": "Not found"}), 404
    conn.execute("UPDATE orders SET status='Cancelled', updated_at=? WHERE id=?",
                 (datetime.now().isoformat(), oid))
    conn.commit()
    conn.close()
    log_audit("cancel_order", "order", oid)
    return jsonify({"message": f"✅ Order #{oid} cancelled"})
