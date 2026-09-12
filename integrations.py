"""
Integrations: printable barcode labels, an outbound webhook layer for
ERP/accounting systems, and scheduled SQLite backups.

- Barcode labels: renders real Code128 barcodes (python-barcode, pure
  Python) into a PDF label sheet. An item with no barcode on file yet
  gets one auto-assigned at print time, so the label you print is
  immediately scannable by the existing RFID/barcode scan feature
  instead of being a disconnected decoration.
- Webhooks: POSTs a JSON payload to every enabled, matching webhook URL
  when an event fires, signed with HMAC-SHA256 (X-OrderFlow-Signature)
  so the receiver can verify it actually came from here. Every attempt
  is logged; a failed or unreachable endpoint never blocks the action
  that triggered it, same principle as notifications.
- Backups: a daemon thread copies orders.db on an interval
  (BACKUP_INTERVAL_HOURS, default 24) and prunes old copies beyond
  BACKUP_RETENTION_COUNT (default 14), plus a manual "back up now" and a
  download list. Guarded against double-starting under Flask's debug
  reloader, which runs this module's import twice.
"""

import glob
import hashlib
import hmac
import io
import json
import os
import threading
import time
from datetime import datetime

import requests
from flask import Blueprint, request, jsonify, send_file

from database import get_db, DB_NAME

integrations_bp = Blueprint("integrations", __name__)

WEBHOOK_EVENT_TYPES = ["order_created", "order_status_change", "purchase_order_received", "payment_succeeded"]

BACKUP_DIR = os.environ.get("BACKUP_DIR", "backups")
BACKUP_INTERVAL_HOURS = float(os.environ.get("BACKUP_INTERVAL_HOURS", "24"))
BACKUP_RETENTION_COUNT = int(os.environ.get("BACKUP_RETENTION_COUNT", "14"))


# ─────────────────────────────────────────────────────────
# Barcode labels
# ─────────────────────────────────────────────────────────

@integrations_bp.route("/api/labels/print")
def print_labels():
    from fpdf import FPDF
    import barcode
    from barcode.writer import ImageWriter

    ids_param = request.args.get("inventory_ids", "")
    try:
        ids = [int(x) for x in ids_param.split(",") if x.strip()]
    except ValueError:
        return jsonify({"message": "❌ inventory_ids must be a comma-separated list of integers"}), 400
    if not ids:
        return jsonify({"message": "❌ inventory_ids is required, e.g. ?inventory_ids=1,2,3"}), 400

    conn = get_db()
    items = []
    for iid in ids:
        row = conn.execute("SELECT * FROM inventory WHERE id = ?", (iid,)).fetchone()
        if not row:
            continue
        item = dict(row)
        if not item["barcode"]:
            code = f"BC{iid:06d}"
            conn.execute("UPDATE inventory SET barcode = ? WHERE id = ?", (code, iid))
            item["barcode"] = code
        items.append(item)
    conn.commit()
    conn.close()

    if not items:
        return jsonify({"message": "❌ None of those inventory ids were found"}), 404

    LABEL_W, LABEL_H = 90, 40  # mm — roughly a standard 3.5" x 1.6" label
    COLS = 2
    pdf = FPDF(unit="mm", format="A4")
    pdf.set_auto_page_break(auto=False)
    pdf.add_page()
    x0, y0 = 10, 10
    col = row_n = 0

    for item in items:
        code = barcode.get("code128", item["barcode"], writer=ImageWriter())
        buf = io.BytesIO()
        code.write(buf, options={"write_text": False, "module_height": 10, "quiet_zone": 2})
        buf.seek(0)

        x = x0 + col * (LABEL_W + 5)
        y = y0 + row_n * (LABEL_H + 5)
        if y + LABEL_H > 287:
            pdf.add_page()
            row_n = 0
            y = y0

        pdf.rect(x, y, LABEL_W, LABEL_H)
        pdf.set_xy(x + 3, y + 3)
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(LABEL_W - 6, 6, item["part_name"][:32], new_x="LMARGIN", new_y="NEXT")
        pdf.set_xy(x + 3, y + 10)
        pdf.image(buf, x=x + 5, y=y + 12, w=LABEL_W - 10)
        pdf.set_xy(x + 3, y + LABEL_H - 8)
        pdf.set_font("Courier", "", 9)
        pdf.cell(LABEL_W - 6, 5, item["barcode"], align="C")

        col += 1
        if col >= COLS:
            col = 0
            row_n += 1

    pdf_bytes = bytes(pdf.output())
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"orderflow_labels_{datetime.now().strftime('%Y%m%d')}.pdf",
    )


# ─────────────────────────────────────────────────────────
# Outbound webhooks
# ─────────────────────────────────────────────────────────

def dispatch_webhook(event_type, payload):
    """Fire-and-log — never raises, never blocks the caller on a slow or
    dead endpoint for long (short timeout)."""
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM webhooks WHERE event_type = ? AND enabled = 1", (event_type,)
        ).fetchall()
        conn.close()
    except Exception as e:
        print("dispatch_webhook() could not read webhooks:", e)
        return

    body = json.dumps({"event": event_type, "timestamp": datetime.now().isoformat(), "data": payload})
    for wh in rows:
        status_code, error = None, None
        headers = {"Content-Type": "application/json"}
        if wh["secret"]:
            signature = hmac.new(wh["secret"].encode(), body.encode(), hashlib.sha256).hexdigest()
            headers["X-OrderFlow-Signature"] = signature
        try:
            resp = requests.post(wh["url"], data=body, headers=headers, timeout=5)
            status_code = resp.status_code
        except Exception as e:
            error = str(e)
            print(f"[webhook] {wh['url']} failed: {e}")

        try:
            conn = get_db()
            conn.execute("""
                INSERT INTO webhook_log (webhook_id, event_type, status_code, error, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (wh["id"], event_type, status_code, error, datetime.now().isoformat()))
            conn.commit()
            conn.close()
        except Exception as e:
            print("webhook log write failed:", e)


@integrations_bp.route("/api/webhooks", methods=["GET"])
def list_webhooks():
    # Never return the secret itself — only whether one is set, so the UI
    # can show "Signed" without exposing anything a receiver-side check
    # would need kept private.
    conn = get_db()
    rows = conn.execute("""
        SELECT id, url, event_type, enabled, created_at,
               (secret IS NOT NULL AND secret != '') AS has_secret
        FROM webhooks ORDER BY id DESC
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@integrations_bp.route("/api/webhooks", methods=["POST"])
def create_webhook():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    event_type = data.get("event_type")
    secret = (data.get("secret") or "").strip() or None

    if not url.startswith(("http://", "https://")):
        return jsonify({"message": "❌ url must start with http:// or https://"}), 400
    if event_type not in WEBHOOK_EVENT_TYPES:
        return jsonify({"message": f"❌ event_type must be one of: {', '.join(WEBHOOK_EVENT_TYPES)}"}), 400

    conn = get_db()
    cur = conn.execute("INSERT INTO webhooks (url, event_type, secret, enabled, created_at) VALUES (?, ?, ?, 1, ?)",
                        (url, event_type, secret, datetime.now().isoformat()))
    conn.commit()
    webhook_id = cur.lastrowid
    conn.close()
    return jsonify({"message": f"✅ Webhook registered for {event_type}", "id": webhook_id})


@integrations_bp.route("/api/webhooks/<int:webhook_id>", methods=["DELETE"])
def delete_webhook(webhook_id):
    conn = get_db()
    conn.execute("DELETE FROM webhooks WHERE id = ?", (webhook_id,))
    conn.commit()
    conn.close()
    return jsonify({"message": "✅ Webhook removed"})


@integrations_bp.route("/api/webhooks/<int:webhook_id>/test", methods=["POST"])
def test_webhook(webhook_id):
    conn = get_db()
    wh = conn.execute("SELECT * FROM webhooks WHERE id = ?", (webhook_id,)).fetchone()
    conn.close()
    if not wh:
        return jsonify({"message": "Not found"}), 404

    dispatch_webhook(wh["event_type"], {"test": True, "message": "Test payload from OrderFlow AI"})
    return jsonify({"message": "✅ Test payload sent — check /api/webhooks/log for the result"})


@integrations_bp.route("/api/webhooks/log")
def webhook_log():
    conn = get_db()
    rows = conn.execute("""
        SELECT wl.*, w.url FROM webhook_log wl LEFT JOIN webhooks w ON w.id = wl.webhook_id
        ORDER BY wl.id DESC LIMIT 200
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ─────────────────────────────────────────────────────────
# Scheduled backups
# ─────────────────────────────────────────────────────────

def _safe_backup_name(name):
    return os.path.basename(name)


def backup_db():
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        if not os.path.exists(DB_NAME):
            return None
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = os.path.join(BACKUP_DIR, f"orders_{stamp}.db")

        conn = get_db()
        backup_conn = __import__("sqlite3").connect(dest)
        conn.backup(backup_conn)
        backup_conn.close()
        conn.close()

        # Retention: keep only the newest BACKUP_RETENTION_COUNT files
        existing = sorted(glob.glob(os.path.join(BACKUP_DIR, "orders_*.db")))
        for old in existing[:-BACKUP_RETENTION_COUNT] if BACKUP_RETENTION_COUNT > 0 else []:
            try:
                os.remove(old)
            except OSError:
                pass

        print(f"[backup] wrote {dest}")
        return dest
    except Exception as e:
        print("[backup] failed:", e)
        return None


def _backup_loop():
    while True:
        time.sleep(max(BACKUP_INTERVAL_HOURS, 0.01) * 3600)
        backup_db()


def start_backup_scheduler():
    """Call once at app startup — unconditionally starts the thread.
    The guard against Flask's debug reloader running this twice (once in
    its watcher process, once in the actual worker) lives at the call
    site in app.py, which is the only place that actually knows whether
    debug mode / a reloader is in play; WERKZEUG_RUN_MAIN alone can't
    tell a non-debug single process apart from the reloader's watcher
    process, since both leave it unset."""
    t = threading.Thread(target=_backup_loop, daemon=True)
    t.start()
    print(f"[backup] scheduler started — every {BACKUP_INTERVAL_HOURS}h, keeping last {BACKUP_RETENTION_COUNT}")


@integrations_bp.route("/api/backups", methods=["GET"])
def list_backups():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "orders_*.db")), reverse=True)
    out = [{"filename": os.path.basename(f), "size_bytes": os.path.getsize(f),
            "created_at": datetime.fromtimestamp(os.path.getmtime(f)).isoformat()} for f in files]
    return jsonify(out)


@integrations_bp.route("/api/backups/run", methods=["POST"])
def run_backup_now():
    dest = backup_db()
    if not dest:
        return jsonify({"message": "❌ Backup failed — see server log"}), 500
    return jsonify({"message": f"✅ Backed up to {os.path.basename(dest)}"})


@integrations_bp.route("/api/backups/<path:filename>", methods=["GET"])
def download_backup(filename):
    safe_name = _safe_backup_name(filename)
    path = os.path.join(BACKUP_DIR, safe_name)
    if not safe_name.startswith("orders_") or not os.path.isfile(path):
        return jsonify({"message": "Not found"}), 404
    return send_file(path, as_attachment=True, download_name=safe_name)
