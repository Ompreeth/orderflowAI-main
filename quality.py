"""
Quality control dashboard, built on the `quality_logs` table that already
existed (populated by the chat command "Log quality for order #42: ...").
That chat path still writes a free-text `note` and nothing else — this
module adds the structured fields (pass/fail result, defect category,
corrective action) and a real UI around them, plus a printable
certificate of conformance per order.
"""

from datetime import datetime

from flask import Blueprint, request, jsonify, session

from database import get_db
from auth import log_audit
from notifications import notify

quality_bp = Blueprint("quality", __name__)

RESULTS = ["pass", "fail", "conditional"]


@quality_bp.route("/api/quality-logs", methods=["GET"])
def list_quality_logs():
    order_id = request.args.get("order_id", type=int)
    conn = get_db()
    if order_id:
        rows = conn.execute("""
            SELECT q.*, o.part_name, o.material
            FROM quality_logs q LEFT JOIN orders o ON o.id = q.order_id
            WHERE q.order_id = ? ORDER BY q.id DESC
        """, (order_id,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT q.*, o.part_name, o.material
            FROM quality_logs q LEFT JOIN orders o ON o.id = q.order_id
            ORDER BY q.id DESC LIMIT 500
        """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@quality_bp.route("/api/quality-logs", methods=["POST"])
def create_quality_log():
    data = request.get_json(silent=True) or {}
    order_id = data.get("order_id")
    result = (data.get("result") or "").strip().lower()

    if not order_id:
        return jsonify({"message": "❌ order_id is required"}), 400
    if result not in RESULTS:
        return jsonify({"message": f"❌ result must be one of: {', '.join(RESULTS)}"}), 400

    conn = get_db()
    order = conn.execute("SELECT id, part_name FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        return jsonify({"message": "❌ Order not found"}), 404

    defect_category = (data.get("defect_category") or "").strip() or None
    corrective_action = (data.get("corrective_action") or "").strip() or None
    note = (data.get("note") or "").strip() or None

    if result == "fail" and not defect_category:
        conn.close()
        return jsonify({"message": "❌ A failed check needs a defect category"}), 400

    cur = conn.execute("""
        INSERT INTO quality_logs (order_id, note, log_time, result, defect_category, corrective_action, logged_by)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        order_id, note, datetime.now().isoformat(),
        result, defect_category, corrective_action,
        session.get("username", "guest"),
    ))
    conn.commit()
    log_id = cur.lastrowid
    conn.close()

    log_audit("quality_check", "order", order_id, f"{result}" + (f" — {defect_category}" if defect_category else ""))

    # A failed check is a business event — fan it out to whatever notification
    # channels are subscribed to 'quality_fail' (fire-and-log, never raises).
    if result == "fail":
        detail = f" — {defect_category}" if defect_category else ""
        if corrective_action:
            detail += f"; corrective action: {corrective_action}"
        notify("quality_fail",
               f"Quality FAIL on order #{order_id} ({order['part_name']}){detail}")

    icon = {"pass": "✅", "fail": "❌", "conditional": "⚠️"}[result]
    return jsonify({"message": f"{icon} Quality check logged for order #{order_id}: {result}", "id": log_id})


@quality_bp.route("/api/orders/<int:order_id>/quality-summary")
def order_quality_summary(order_id):
    """Everything a certificate of conformance needs for one order."""
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        return jsonify({"message": "Order not found"}), 404

    logs = conn.execute("""
        SELECT * FROM quality_logs WHERE order_id = ? ORDER BY id ASC
    """, (order_id,)).fetchall()
    conn.close()

    logs_list = [dict(r) for r in logs]
    checked_logs = [l for l in logs_list if l["result"] in RESULTS]
    overall = "no checks logged"
    if checked_logs:
        if any(l["result"] == "fail" for l in checked_logs):
            overall = "fail"
        elif any(l["result"] == "conditional" for l in checked_logs):
            overall = "conditional"
        else:
            overall = "pass"

    return jsonify({
        "order": dict(order),
        "logs": logs_list,
        "overall_result": overall,
    })
