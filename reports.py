"""
Reporting & analytics — read-only views over data the rest of the app
already produces. Nothing here writes to the database.

- CSV export for orders, inventory valuation, and supplier scorecards
  (real file downloads, Content-Disposition: attachment).
- Inventory valuation + ABC analysis: value = quantity x best-known unit
  cost (averaged across on-file suppliers for that part). An item with
  no supplier on file has no real cost basis yet, so it values at $0
  rather than a guess. ABC uses the standard 80/95 cumulative-value
  cutoffs (A = top ~80% of value, B = next ~15%, C = the long tail).
- Order fulfillment time: created_at -> updated_at for orders that
  reached 'Accepted'. This depends on `orders.updated_at`, which is a
  new column (see database.py) only stamped going forward — older rows
  that never had a recorded status change simply have no fulfillment
  data, which is reported as such rather than guessed at.
- Supplier scorecard: half the manually-entered `reliability` rating,
  half a real on-time-delivery rate computed from completed purchase
  orders (actual received-date lead time vs. the supplier's promised
  lead_days). A supplier with no completed POs yet scores on
  reliability alone — "unproven", not "bad".
- One combined PDF (fpdf2, pure-Python, no system dependencies) pulling
  all of the above into a single printable report.
"""

import csv
import io
from datetime import datetime

from flask import Blueprint, Response, jsonify, send_file

from database import get_db

reports_bp = Blueprint("reports", __name__)


# ─────────────────────────────────────────────────────────
# Shared computations
# ─────────────────────────────────────────────────────────

def _inventory_valuation_rows(conn):
    rows = conn.execute("""
        SELECT i.id, i.part_name, i.material, i.quantity, i.reorder_at,
               (SELECT AVG(s.unit_cost) FROM suppliers s
                WHERE s.inventory_id = i.id AND s.unit_cost > 0) AS avg_unit_cost
        FROM inventory i
        ORDER BY i.part_name
    """).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        unit_cost = round(d.pop("avg_unit_cost") or 0, 2)
        d["unit_cost"] = unit_cost
        d["total_value"] = round(unit_cost * d["quantity"], 2)
        out.append(d)
    return out


def _abc_classify(rows):
    """Adds value_pct / cumulative_pct / abc_class to each row. Standard
    80/15/5 cumulative-value cutoffs, with two adjustments that matter a
    lot on a small/young catalog (which is the common case — most shops
    don't have hundreds of priced SKUs on day one):

    - Items with no cost data yet (total_value == 0, i.e. no supplier on
      file) aren't rankable at all and are marked 'N/A' rather than
      lumped in with genuinely low-value C-class parts — "unpriced" and
      "low priority" are different things.
    - Class is decided from each item's MIDPOINT in the cumulative curve
      (half its own value added to the running total before it), not
      where the curve lands after the whole item. The end-of-item
      convention is overly sensitive with few SKUs: a single dominant
      item can shove the cumulative line straight past the A/B bands
      into C, which reads as "our biggest-value part is bottom
      priority" — backwards. The midpoint convention is standard
      practice for exactly this reason.
    """
    priced = [r for r in rows if r["total_value"] > 0]
    unpriced = [r for r in rows if r["total_value"] <= 0]
    total_value = sum(r["total_value"] for r in priced)

    ranked = sorted(priced, key=lambda r: -r["total_value"])
    cumulative = 0
    for r in ranked:
        midpoint = cumulative + r["total_value"] / 2
        cumulative += r["total_value"]
        r["value_pct"] = round(r["total_value"] / total_value * 100, 1)
        r["cumulative_pct"] = round(cumulative / total_value * 100, 1)
        midpoint_pct = midpoint / total_value * 100
        r["abc_class"] = "A" if midpoint_pct <= 80 else ("B" if midpoint_pct <= 95 else "C")

    for r in unpriced:
        r["value_pct"] = None
        r["cumulative_pct"] = None
        r["abc_class"] = "N/A"

    return ranked + unpriced


def _supplier_scorecard_rows(conn):
    suppliers = conn.execute("SELECT * FROM suppliers ORDER BY name").fetchall()
    out = []
    for srow in suppliers:
        s = dict(srow)
        completed = conn.execute("""
            SELECT created_at, updated_at FROM purchase_orders
            WHERE supplier_id = ? AND status = 'received'
        """, (s["id"],)).fetchall()

        lead_times, on_time = [], 0
        for po in completed:
            try:
                created = datetime.fromisoformat(po["created_at"])
                received = datetime.fromisoformat(po["updated_at"])
                actual_days = (received - created).total_seconds() / 86400
            except (TypeError, ValueError):
                continue
            lead_times.append(actual_days)
            if actual_days <= (s["lead_days"] or 0):
                on_time += 1

        n = len(lead_times)
        avg_actual_lead = round(sum(lead_times) / n, 1) if n else None
        on_time_rate = round(on_time / n * 100, 1) if n else None

        # Half reliability (as entered), half real on-time history once
        # there's history to draw on; reliability alone until then.
        scorecard_score = round(s["reliability"] * 0.5 + on_time_rate * 0.5, 1) if n else s["reliability"]

        s["completed_pos"] = n
        s["avg_actual_lead_days"] = avg_actual_lead
        s["on_time_rate"] = on_time_rate
        s["scorecard_score"] = scorecard_score
        out.append(s)

    out.sort(key=lambda r: -r["scorecard_score"])
    return out


def _fulfillment_metrics(conn):
    rows = conn.execute("""
        SELECT id, created_at, updated_at FROM orders
        WHERE status = 'Accepted' AND updated_at IS NOT NULL
    """).fetchall()

    durations = []
    for r in rows:
        try:
            created = datetime.fromisoformat(r["created_at"])
            done = datetime.fromisoformat(r["updated_at"])
            days = (done - created).total_seconds() / 86400
        except (TypeError, ValueError):
            continue
        if days >= 0:
            durations.append(days)
    durations.sort()

    n = len(durations)
    avg_days = round(sum(durations) / n, 2) if n else None
    if n:
        mid = n // 2
        median_days = round(durations[mid] if n % 2 else (durations[mid - 1] + durations[mid]) / 2, 2)
    else:
        median_days = None

    status_counts = {r["status"]: r["c"] for r in
                      conn.execute("SELECT status, COUNT(*) AS c FROM orders GROUP BY status").fetchall()}

    oldest_open = conn.execute("""
        SELECT id, part_name, status, created_at, deadline FROM orders
        WHERE status NOT IN ('Accepted', 'Cancelled', 'Returned')
        ORDER BY created_at ASC LIMIT 10
    """).fetchall()

    return {
        "measured_orders": n,
        "avg_fulfillment_days": avg_days,
        "median_fulfillment_days": median_days,
        "status_counts": status_counts,
        "oldest_open_orders": [dict(r) for r in oldest_open],
    }


def _csv_response(rows, fieldnames, filename):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    resp = Response(buf.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return resp


# ─────────────────────────────────────────────────────────
# Routes — JSON
# ─────────────────────────────────────────────────────────

@reports_bp.route("/api/reports/inventory-valuation")
def inventory_valuation():
    conn = get_db()
    rows = _abc_classify(_inventory_valuation_rows(conn))
    conn.close()
    return jsonify({
        "items": rows,
        "total_value": round(sum(r["total_value"] for r in rows), 2),
    })


@reports_bp.route("/api/reports/supplier-scorecard")
def supplier_scorecard():
    conn = get_db()
    rows = _supplier_scorecard_rows(conn)
    conn.close()
    return jsonify(rows)


@reports_bp.route("/api/reports/fulfillment")
def fulfillment():
    conn = get_db()
    data = _fulfillment_metrics(conn)
    conn.close()
    return jsonify(data)


# ─────────────────────────────────────────────────────────
# Routes — CSV exports
# ─────────────────────────────────────────────────────────

@reports_bp.route("/api/reports/orders.csv")
def orders_csv():
    conn = get_db()
    rows = [dict(r) for r in conn.execute("SELECT * FROM orders ORDER BY id").fetchall()]
    conn.close()
    fields = ["id", "part_name", "material", "quantity", "specs", "deadline",
              "status", "created_at", "updated_at", "inventory_id"]
    return _csv_response(rows, fields, "orderflow_orders.csv")


@reports_bp.route("/api/reports/inventory-valuation.csv")
def inventory_valuation_csv():
    conn = get_db()
    rows = _abc_classify(_inventory_valuation_rows(conn))
    conn.close()
    fields = ["id", "part_name", "material", "quantity", "reorder_at",
              "unit_cost", "total_value", "value_pct", "cumulative_pct", "abc_class"]
    return _csv_response(rows, fields, "orderflow_inventory_valuation.csv")


@reports_bp.route("/api/reports/supplier-scorecard.csv")
def supplier_scorecard_csv():
    conn = get_db()
    rows = _supplier_scorecard_rows(conn)
    conn.close()
    fields = ["id", "name", "part_name", "lead_days", "reliability", "unit_cost",
              "completed_pos", "avg_actual_lead_days", "on_time_rate", "scorecard_score"]
    return _csv_response(rows, fields, "orderflow_supplier_scorecard.csv")


# ─────────────────────────────────────────────────────────
# Combined PDF report
# ─────────────────────────────────────────────────────────

def _pdf_table(pdf, headers, col_widths, rows, row_height=7):
    from fpdf import FPDF  # local import keeps this helper self-contained

    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(230, 230, 230)
    for h, w in zip(headers, col_widths):
        pdf.cell(w, row_height, str(h), border=1, fill=True)
    pdf.ln(row_height)

    pdf.set_font("Helvetica", "", 9)
    for row in rows:
        for val, w in zip(row, col_widths):
            pdf.cell(w, row_height, str(val), border=1)
        pdf.ln(row_height)


@reports_bp.route("/api/reports/summary.pdf")
def summary_pdf():
    from fpdf import FPDF

    conn = get_db()
    status_counts = {r["status"]: r["c"] for r in
                      conn.execute("SELECT status, COUNT(*) AS c FROM orders GROUP BY status").fetchall()}
    total_orders = sum(status_counts.values())
    fulfillment = _fulfillment_metrics(conn)
    valuation = _abc_classify(_inventory_valuation_rows(conn))
    scorecard = _supplier_scorecard_rows(conn)
    conn.close()

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 10, "OrderFlow AI - Operations Report", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(110, 110, 110)
    pdf.cell(0, 6, f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(4)

    # ── Orders summary ──
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "Orders Summary", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, f"Total orders: {total_orders}", new_x="LMARGIN", new_y="NEXT")
    for status, count in status_counts.items():
        pdf.cell(0, 6, f"  {status}: {count}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    # ── Fulfillment ──
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "Order Fulfillment Time", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    if fulfillment["measured_orders"]:
        pdf.cell(0, 6, f"Measured orders (Received -> Accepted): {fulfillment['measured_orders']}",
                  new_x="LMARGIN", new_y="NEXT")
        pdf.cell(0, 6, f"Average: {fulfillment['avg_fulfillment_days']} days", new_x="LMARGIN", new_y="NEXT")
        pdf.cell(0, 6, f"Median: {fulfillment['median_fulfillment_days']} days", new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.cell(0, 6, "No completed orders with a recorded status-change timestamp yet.",
                  new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    # ── Inventory valuation ──
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "Inventory Valuation (top 12 by value)", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, f"Total inventory value: ${sum(r['total_value'] for r in valuation):,.2f}",
              new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)
    if valuation:
        _pdf_table(
            pdf,
            ["Part", "Qty", "Unit Cost", "Value", "Class"],
            [70, 20, 30, 35, 20],
            [[r["part_name"][:38], r["quantity"], f"${r['unit_cost']:.2f}",
              f"${r['total_value']:,.2f}", r["abc_class"]] for r in valuation[:12]],
        )
    else:
        pdf.cell(0, 6, "No inventory items yet.", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # ── Supplier scorecard ──
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "Supplier Scorecard", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    if scorecard:
        _pdf_table(
            pdf,
            ["Supplier", "Promised Lead", "Actual Lead", "On-Time %", "Reliability", "Score"],
            [45, 28, 28, 25, 27, 22],
            [[r["name"][:24], f"{r['lead_days']}d",
              f"{r['avg_actual_lead_days']}d" if r["avg_actual_lead_days"] is not None else "n/a",
              f"{r['on_time_rate']}%" if r["on_time_rate"] is not None else "n/a",
              f"{r['reliability']}%", r["scorecard_score"]] for r in scorecard],
        )
    else:
        pdf.cell(0, 6, "No suppliers on file yet.", new_x="LMARGIN", new_y="NEXT")

    pdf_bytes = bytes(pdf.output())
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"orderflow_report_{datetime.now().strftime('%Y%m%d')}.pdf",
    )
