"""Saved dashboard filters — name a filter combination once, re-apply it
with a click instead of re-clicking the same filter buttons every visit.
Scoped by view_type ('orders', 'purchase_orders', 'quality') so each
table's saved views only show up in that table's picker."""

import json
from datetime import datetime

from flask import Blueprint, request, jsonify, session

from database import get_db

saved_views_bp = Blueprint("saved_views", __name__)


@saved_views_bp.route("/api/saved-views", methods=["GET"])
def list_saved_views():
    view_type = request.args.get("view_type")
    conn = get_db()
    if view_type:
        rows = conn.execute(
            "SELECT * FROM saved_views WHERE view_type = ? ORDER BY id DESC", (view_type,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM saved_views ORDER BY id DESC").fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["filter"] = json.loads(d["filter_json"]) if d["filter_json"] else {}
        except (TypeError, ValueError):
            d["filter"] = {}
        out.append(d)
    return jsonify(out)


@saved_views_bp.route("/api/saved-views", methods=["POST"])
def create_saved_view():
    data = request.get_json(silent=True) or {}
    view_name = (data.get("view_name") or "").strip()
    view_type = data.get("view_type")
    filter_data = data.get("filter", {})

    if not view_name or not view_type:
        return jsonify({"message": "❌ view_name and view_type are required"}), 400

    conn = get_db()
    cur = conn.execute("""
        INSERT INTO saved_views (username, view_name, view_type, filter_json, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (session.get("username", "guest"), view_name, view_type, json.dumps(filter_data), datetime.now().isoformat()))
    conn.commit()
    view_id = cur.lastrowid
    conn.close()
    return jsonify({"message": f"✅ View '{view_name}' saved", "id": view_id})


@saved_views_bp.route("/api/saved-views/<int:view_id>", methods=["DELETE"])
def delete_saved_view(view_id):
    conn = get_db()
    conn.execute("DELETE FROM saved_views WHERE id = ?", (view_id,))
    conn.commit()
    conn.close()
    return jsonify({"message": "✅ View removed"})
