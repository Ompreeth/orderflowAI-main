"""CSV bulk import/export for the inventory catalog. Import upserts by
part_name (case-insensitive exact match) — existing items get their
quantity/reorder_at/material/unit updated, unrecognized names are created
fresh. One bad row doesn't fail the whole file: it's skipped and reported
by row number so you can fix just that line and re-run.
"""

import csv
import io
from datetime import datetime

from flask import Blueprint, request, jsonify, Response

from database import get_db

inventory_io_bp = Blueprint("inventory_io", __name__)

EXPORT_FIELDS = ["id", "part_name", "material", "unit", "quantity", "reorder_at", "rfid_tag", "barcode"]


@inventory_io_bp.route("/api/inventory/export.csv")
def export_inventory_csv():
    conn = get_db()
    rows = [dict(r) for r in conn.execute("SELECT * FROM inventory ORDER BY part_name").fetchall()]
    conn.close()

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=EXPORT_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    resp = Response(buf.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = "attachment; filename=orderflow_inventory.csv"
    return resp


@inventory_io_bp.route("/api/inventory/import-csv", methods=["POST"])
def import_inventory_csv():
    if "file" not in request.files:
        return jsonify({"message": "❌ No file uploaded — attach a CSV with a 'part_name' column"}), 400
    file = request.files["file"]
    if not file.filename:
        return jsonify({"message": "❌ No file selected"}), 400

    try:
        text = file.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return jsonify({"message": "❌ Couldn't read the file as UTF-8 text — is it really a CSV?"}), 400

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "part_name" not in [f.strip().lower() for f in reader.fieldnames]:
        return jsonify({"message": "❌ CSV must have a 'part_name' column (material, unit, quantity, reorder_at are optional)"}), 400

    # Normalize header casing so "Part_Name" / "PART_NAME" both work
    field_map = {f.strip().lower(): f for f in reader.fieldnames}

    def get(row, key, default=""):
        col = field_map.get(key)
        val = row.get(col, default) if col else default
        return (val or "").strip()

    conn = get_db()
    existing = {r["part_name"].lower(): r for r in conn.execute("SELECT * FROM inventory").fetchall()}

    created, updated, skipped = 0, 0, []
    for i, row in enumerate(reader, start=2):  # header is row 1
        part_name = get(row, "part_name")
        if not part_name:
            skipped.append(f"row {i}: missing part_name")
            continue
        try:
            quantity = int(get(row, "quantity") or 0)
            reorder_at = int(get(row, "reorder_at") or 10)
        except ValueError:
            skipped.append(f"row {i} ({part_name}): quantity/reorder_at must be whole numbers")
            continue

        material = get(row, "material") or None
        unit = get(row, "unit") or "pcs"
        match = existing.get(part_name.lower())

        if match:
            conn.execute(
                "UPDATE inventory SET material=?, unit=?, quantity=?, reorder_at=? WHERE id=?",
                (material, unit, quantity, reorder_at, match["id"]),
            )
            updated += 1
        else:
            try:
                conn.execute("""
                    INSERT INTO inventory (part_name, material, unit, quantity, reorder_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (part_name, material, unit, quantity, reorder_at, datetime.now().isoformat()))
                created += 1
            except Exception as e:
                skipped.append(f"row {i} ({part_name}): {e}")

    conn.commit()
    conn.close()

    msg = f"✅ Import done — {created} created, {updated} updated"
    if skipped:
        msg += f", {len(skipped)} skipped"
    return jsonify({"message": msg, "created": created, "updated": updated, "skipped": skipped})
