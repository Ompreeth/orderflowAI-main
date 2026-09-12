from flask import Flask, request, jsonify, render_template, session
from flask_cors import CORS

import os
import sys
import secrets
import psycopg2
import requests
import json
import re
import math
import statistics
from datetime import datetime

# On Windows, stdout/stderr default to a legacy code page (e.g. cp1252) that
# can't encode the ✅ 🔔 → — characters this app uses all over its log lines.
# A bare print() of such a string then raises UnicodeEncodeError — and when
# that happens inside notify()/dispatch_webhook() mid-request it bubbles up as
# a 500. Force UTF-8 so logging can never be the thing that breaks a request.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Load .env (DATABASE_URL, SMTP_*, TWILIO_*, SLACK_WEBHOOK_URL, SECRET_KEY,
# OLLAMA_*, …) into the environment before any local module import — both
# database.py (DATABASE_URL) and notifications.py read these at import
# time, so this has to run first. No-op if python-dotenv isn't installed
# or there's no .env file.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from database import get_db, init_db
from auth import auth_bp, bootstrap_admin
from procurement import procurement_bp, _best_supplier_for, _create_po_row
from payments import payments_bp
from quality import quality_bp
from reports import reports_bp, _inventory_valuation_rows, _supplier_scorecard_rows, _fulfillment_metrics
from production import production_bp
from notifications import notifications_bp, notify
from warehouses import warehouses_bp
from integrations import integrations_bp, dispatch_webhook, start_backup_scheduler
from search import search_bp
from saved_views import saved_views_bp
from inventory_io import inventory_io_bp

# templates/ and static/ resolve relative to this file normally, but once
# PyInstaller frozen (desktop.py), everything is unpacked into a temp
# sys._MEIPASS dir at runtime instead — this makes both cases work.
_base_path = sys._MEIPASS if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__,
            template_folder=os.path.join(_base_path, "templates"),
            static_folder=os.path.join(_base_path, "static"))
CORS(app, supports_credentials=True)

# Session signing key — set SECRET_KEY in the environment so logins survive
# a restart; a random one is generated otherwise so the app still runs.
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
if not os.environ.get("SECRET_KEY"):
    print("SECRET_KEY not set — using a random one for this run (sessions "
          "won't survive a restart). Set SECRET_KEY to fix that.")

app.register_blueprint(auth_bp)
app.register_blueprint(procurement_bp)
app.register_blueprint(payments_bp)
app.register_blueprint(quality_bp)
app.register_blueprint(reports_bp)
app.register_blueprint(production_bp)
app.register_blueprint(notifications_bp)
app.register_blueprint(warehouses_bp)
app.register_blueprint(integrations_bp)
app.register_blueprint(search_bp)
app.register_blueprint(saved_views_bp)
app.register_blueprint(inventory_io_bp)

# ── AI backend config ────────────────────────────────────
# Points at a local/remote Ollama server. Configurable via env vars so this
# runs wherever you deploy it, instead of a hardcoded address that only
# ever worked on the original machine's network:
#   export OLLAMA_BASE_URL="http://localhost:11434"
#   export OLLAMA_MODEL="qwen2.5:7b"
OLLAMA_BASE  = os.environ.get("OLLAMA_BASE_URL", "http://100.71.169.79:11434").rstrip("/")
OLLAMA_URL   = os.environ.get("OLLAMA_URL", f"{OLLAMA_BASE}/api/generate")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")

# Verbose per-request chat/LLM logging (raw user messages, full model output) —
# useful when troubleshooting a local Ollama setup, noisy and slightly privacy-
# sensitive otherwise. Off by default; set CHAT_DEBUG=1 to turn it back on.
CHAT_DEBUG = os.environ.get("CHAT_DEBUG", "0") == "1"

STATUS_FLOW = ["Received", "In Review", "Accepted"]


# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────

def extract_json(text):
    try:
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            return json.loads(match.group())
        return {"intent": "unknown"}
    except Exception as e:
        print("JSON ERROR:", e)
        print(text)
        return {"intent": "unknown"}


def detect_language(text):
    try:
        prompt = f"""
Detect the language.

Return ONLY one word:

english
telugu
kannada
hindi
tamil
malayalam

Text:
{text}
"""
        response = requests.post(
            OLLAMA_URL,
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=30
        )
        return response.json()["response"].strip().lower()
    except Exception:
        return "english"


def get_inventory_names() -> list[str]:
    """Fetch all part names from the DB to use as translation anchors."""
    try:
        conn = get_db()
        rows = conn.execute("SELECT part_name FROM inventory").fetchall()
        conn.close()
        return [r["part_name"] for r in rows]
    except Exception:
        return []


def translate_to_english(text: str) -> str:
    """
    Translate regional-language user input to English.

    Strategy:
    1. Pull actual inventory part names from the DB.
    2. Give the model those names as a reference glossary so it matches
       known terms (e.g. 'Steel Rods') instead of guessing ('Steel Rails').
    3. Ask the model to preserve numbers and quantities exactly.
    """
    inventory_names = get_inventory_names()
    glossary_block  = ""
    if inventory_names:
        glossary_block = (
            "Known inventory items in this system — "
            "prefer these exact English names when translating:\n"
            + "\n".join(f"  - {n}" for n in inventory_names)
            + "\n\n"
        )

    prompt = f"""You are a manufacturing inventory assistant translator.

{glossary_block}Translate the user message below into English.

Rules:
- Return ONLY the translated text, nothing else.
- Keep all numbers exactly as they are.
- For part/material names, match the closest item from the Known inventory list above if one fits.
- Do NOT invent or paraphrase inventory terms — use the exact name from the list.
- If no inventory item matches, translate as literally as possible.

User message:
{text}
"""

    try:
        response = requests.post(
            OLLAMA_URL,
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=60
        )
        translated = response.json()["response"].strip()
    except Exception as e:
        # Ollama unreachable / model not pulled — degrade gracefully instead
        # of letting the request bubble up as a 500. Assume English.
        print("TRANSLATE ERROR (falling back to original text):", e)
        return text

    # ── Post-process: fuzzy-match any inventory name against the translation ──
    # If the model still got it wrong, correct it by finding the closest
    # inventory name using simple substring / token overlap.
    translated = _correct_part_name(translated, inventory_names)

    if CHAT_DEBUG: print("TRANSLATED:", translated)
    return translated


def _correct_part_name(translated: str, inventory_names: list[str]) -> str:
    """
    Last-resort correction: if a known inventory part name shares significant
    tokens with any word in the translated string, replace that word with the
    canonical inventory name.

    Example: 'Steel Rails' → 'Steel Rods'  (because 'Steel Rods' is in inventory
    and both share the token 'Steel', and 'Rails'/'Rods' are close enough in context)
    """
    if not inventory_names:
        return translated

    translated_lower = translated.lower()

    for name in inventory_names:
        name_tokens = set(name.lower().split())
        # Check how many tokens of this inventory name appear in the translation
        matches = sum(1 for token in name_tokens if token in translated_lower)
        coverage = matches / len(name_tokens) if name_tokens else 0

        # If >50% of the inventory name's tokens appear in the translation,
        # the user almost certainly meant this item — replace the translated
        # portion with the canonical name.
        if coverage >= 0.5 and coverage < 1.0:
            # Find which tokens are missing and which are present
            present_tokens = [t for t in name_tokens if t in translated_lower]
            if present_tokens:
                # Build a pattern from the present tokens and replace with full name
                # (re is already imported at module level)
                # Replace the first token match region with the full canonical name
                pattern = r'\b' + r'\b.*?\b'.join(re.escape(t) for t in present_tokens) + r'\b'
                corrected = re.sub(pattern, name, translated, count=1, flags=re.IGNORECASE)
                if corrected != translated:
                    print(f"PART NAME CORRECTED: '{translated}' → '{corrected}' (matched '{name}')")
                    return corrected

        # Exact full match — already correct, nothing to do
        if coverage == 1.0:
            return translated

    return translated


# translate_response removed — system always replies in English.


def classify_intent(message: str, history=None) -> dict:
    # Recent turns give the model context for follow-ups like "make that
    # 300 instead" or "actually cancel it" that only make sense given
    # what was just discussed. Capped short — this is context, not a
    # transcript, and every extra line costs prompt tokens on every call.
    context_block = ""
    if history:
        lines = [f"{'User' if t.get('role') == 'user' else 'Assistant'}: {t.get('content', '')}"
                 for t in history[-6:]]
        context_block = "Recent conversation, for context only (classify the NEW message below, not this):\n" + \
                         "\n".join(lines) + "\n\n"

    prompt = f"""
You are OrderFlow AI — a manufacturing inventory assistant.

{context_block}The message below is already translated to English. Extract order/inventory information from it.

Return ONLY valid JSON with NO extra text, explanation, or markdown.

Intents:
  create_order    - user wants to order / buy / get / need items
  update_status   - update or advance an order status
  query_order     - check details of a specific order
  list_orders     - list all orders
  log_quality     - log a quality note for an order
  list_inventory  - show all inventory
  query_inventory - check stock of a specific item
  consume_stock   - consume / use stock from inventory
  report_query    - a reporting/analytics question (inventory value, fulfillment time, best supplier, low stock, open orders)

Rules for part_name:
  - ALWAYS extract the item name from the message.
  - If the user says "I need 50 steel rods", part_name = "steel rods"
  - If the user says "order hex bolts", part_name = "hex bolts"
  - NEVER leave part_name as null if a product name is mentioned.
  - Use the exact words from the message for part_name.

Message:
{message}

Respond with ONLY this JSON (fill in the values, keep nulls only where truly absent):

{{
  "intent": "",
  "inventory_id": null,
  "order_id": null,
  "part_name": null,
  "material": null,
  "quantity": null,
  "specs": null,
  "deadline": null,
  "new_status": null,
  "quality_note": null
}}
"""
    try:
        response = requests.post(
            OLLAMA_URL,
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=120
        )
        result = response.json()["response"]

        if CHAT_DEBUG:
            print("\n========== OLLAMA ==========")
            print("USER:", message)
            print("MODEL:", result)
            print("============================\n")

        parsed = extract_json(result)
        if CHAT_DEBUG: print("PARSED:", parsed)
        return parsed

    except Exception as e:
        print("OLLAMA ERROR:", e)
        return {"intent": "unknown"}


def answer_report_query(text: str):
    """Deterministic keyword matches for common reporting questions,
    answered directly from the same data reports.py exposes — no LLM
    round-trip, and it still works even if Ollama is unreachable. Returns
    None (falls through to normal intent classification) on no match."""
    t = (text or "").lower()

    if any(p in t for p in ("inventory value", "inventory worth", "value of inventory", "how much is our inventory", "how much is the inventory")):
        conn = get_db()
        rows = _inventory_valuation_rows(conn)
        conn.close()
        total = sum(r["total_value"] for r in rows)
        return f"📊 Total inventory value: ${total:,.2f} across {len(rows)} item(s). See the Reports tab for the full ABC breakdown."

    if any(p in t for p in ("fulfillment time", "how long do orders take", "average fulfillment", "order fulfillment")):
        conn = get_db()
        m = _fulfillment_metrics(conn)
        conn.close()
        if not m["measured_orders"]:
            return "📊 No completed orders with a recorded status-change timestamp yet — nothing to measure."
        return (f"📊 Average fulfillment time: {m['avg_fulfillment_days']} days "
                f"(median {m['median_fulfillment_days']}), based on {m['measured_orders']} order(s).")

    if any(p in t for p in ("how many open orders", "open orders", "orders are open", "outstanding orders")):
        conn = get_db()
        m = _fulfillment_metrics(conn)
        conn.close()
        open_count = sum(c for s, c in m["status_counts"].items() if s not in ("Accepted", "Cancelled", "Returned"))
        return f"📊 {open_count} order(s) currently open (not yet Accepted, Cancelled, or Returned)."

    if any(p in t for p in ("best supplier", "top supplier", "supplier scorecard", "which supplier is best")):
        conn = get_db()
        rows = _supplier_scorecard_rows(conn)
        conn.close()
        if not rows:
            return "📊 No suppliers on file yet — add one in the Suppliers tab."
        best = rows[0]
        extra = (f", {best['on_time_rate']}% on-time over {best['completed_pos']} completed PO(s)"
                 if best["completed_pos"] else " — no completed POs yet, scored on reliability alone")
        return f"📊 Top-scored supplier: {best['name']} (score {best['scorecard_score']}), {best['reliability']}% reliability{extra}."

    if any(p in t for p in ("low stock", "low on stock", "stockout risk", "what's running low",
                            "whats running low", "running low", "what's low", "whats low")):
        conn = get_db()
        rows = conn.execute(
            "SELECT part_name, quantity, reorder_at FROM inventory WHERE quantity <= reorder_at ORDER BY part_name"
        ).fetchall()
        conn.close()
        if not rows:
            return "📊 Nothing is at or below its reorder point right now."
        lines = "\n".join(f"• {r['part_name']}: {r['quantity']} (reorder at {r['reorder_at']})" for r in rows)
        return f"📊 {len(rows)} item(s) at or below reorder point:\n{lines}"

    return None


def maybe_trigger_reorder(conn, inv_id: int):
    """If stock <= reorder_at, auto-generate a real purchase order to the
    best on-file supplier.

    This used to insert a phantom row into the *customer* `orders` table
    labelled "Auto-reorder" — it looked like a real action but wasn't tied
    to any supplier, had no cost, and polluted the sales-order list with
    fake demand. Now it creates an actual purchase_orders row through the
    same procurement pipeline as the Predictions tab's "Create PO" button,
    complete with a real supplier, cost, and approval-threshold check.

    If no supplier is on file for the part yet, this correctly does
    nothing (you can't auto-order from nobody) rather than faking it.
    """
    row = conn.execute("SELECT * FROM inventory WHERE id = ?", (inv_id,)).fetchone()
    if not row:
        return {"triggered": False}

    if row["quantity"] > row["reorder_at"]:
        return {"triggered": False}

    # Don't stack a second auto-PO on top of one that's already open.
    open_po = conn.execute("""
        SELECT id FROM purchase_orders
        WHERE inventory_id = ? AND auto_generated = 1
          AND status IN ('pending_approval', 'approved', 'sent', 'partially_received')
    """, (inv_id,)).fetchone()
    if open_po:
        return {"triggered": False, "already_open_po": open_po["id"]}

    supplier = _best_supplier_for(conn, inv_id)
    if not supplier:
        return {"triggered": False, "no_supplier": True}

    reorder_qty = max(row["reorder_at"] * 5, 50)
    po_id, po_status, total_cost = _create_po_row(conn, supplier, row, reorder_qty, auto_generated=True)

    notify("stockout_risk",
           f"{row['part_name']} hit its reorder point (stock: {row['quantity']}, reorder at: {row['reorder_at']}). "
           f"Auto-generated PO #{po_id} to {supplier['name']} for {reorder_qty} units (${total_cost:,.2f}).")

    return {
        "triggered": True,
        "part_name": row["part_name"],
        "reorder_qty": reorder_qty,
        "current_stock": row["quantity"],
        "po_id": po_id,
        "po_status": po_status,
        "supplier": supplier["name"],
        "total_cost": total_cost,
    }


# ─────────────────────────────────────────────────────────
# Page
# ─────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/system/status")
def system_status():
    """Health check the sidebar polls to show real AI-connection state —
    previously the UI just showed a hardcoded 'AI connected' dot regardless
    of whether Ollama was actually reachable."""
    connected = False
    try:
        r = requests.get(f"{OLLAMA_BASE}/api/tags", timeout=3)
        connected = r.status_code == 200
    except Exception:
        connected = False
    return jsonify({
        "ollama_connected": connected,
        "model": OLLAMA_MODEL,
        "url": OLLAMA_URL,
    })


# ─────────────────────────────────────────────────────────
# Inventory — GET all / POST add new item
# ─────────────────────────────────────────────────────────

@app.route("/api/inventory", methods=["GET"])
def get_inventory():
    conn = get_db()
    rows = conn.execute("SELECT * FROM inventory ORDER BY part_name").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/inventory", methods=["POST"])
def add_inventory_item():
    """Add a new item to the inventory catalog."""
    data = request.get_json()
    # _s(): safely strip any value that might be None
    def _s(val, default=""):
        return (val if val is not None else default).strip()

    if not data or not _s(data.get("part_name")):
        return jsonify({"message": "part_name is required"}), 400

    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO inventory
                (part_name, material, unit, quantity, reorder_at, rfid_tag, barcode, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            _s(data.get("part_name")),
            _s(data.get("material"))     or None,
            _s(data.get("unit"), "pcs") or "pcs",
            int(data.get("quantity")  or 0),
            int(data.get("reorder_at") or 10),
            _s(data.get("rfid_tag"))     or None,
            _s(data.get("barcode"))      or None,
            datetime.now().isoformat(),
        ))
        conn.commit()
        new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        return jsonify({
            "message": f"✅ '{data['part_name']}' added to inventory as item #{new_id}",
            "id": new_id,
        })
    except psycopg2.errors.UniqueViolation as e:
        conn.rollback()
        conn.close()
        return jsonify({"message": f"❌ Error: {e} — RFID tag or barcode may already exist"}), 400


@app.route("/api/inventory/<int:iid>", methods=["GET"])
def get_inventory_item(iid):
    conn = get_db()
    row  = conn.execute("SELECT * FROM inventory WHERE id = ?", (iid,)).fetchone()
    conn.close()
    return (jsonify(dict(row)), 200) if row else (jsonify({"message": "Not found"}), 404)


@app.route("/api/inventory/<int:iid>", methods=["DELETE"])
def delete_inventory_item(iid):
    """Delete an inventory item (only if no orders reference it)."""
    conn = get_db()
    orders = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE inventory_id = ?", (iid,)
    ).fetchone()[0]
    if orders > 0:
        conn.close()
        return jsonify({"message": f"❌ Cannot delete — {orders} order(s) reference this item"}), 400
    conn.execute("DELETE FROM inventory WHERE id = ?", (iid,))
    conn.commit()
    conn.close()
    return jsonify({"message": f"✅ Inventory item #{iid} deleted"})


# ─────────────────────────────────────────────────────────
# Orders — REST
# ─────────────────────────────────────────────────────────

@app.route("/api/orders/<int:oid>/status", methods=["PATCH"])
def set_order_status(oid):
    """Directly set an order's status — bypasses LLM, used by dashboard buttons."""
    data   = request.get_json(silent=True) or {}
    status = (data.get("status") or "").strip()

    STATUS_MAP = {
        "received":   "Received",
        "in review":  "In Review",
        "inreview":   "In Review",
        "accepted":   "Accepted",
    }

    canonical = STATUS_MAP.get(status.lower())
    if not canonical:
        return jsonify({"message": f"Invalid status '{status}'. Choose: Received, In Review, Accepted"}), 400

    conn = get_db()
    row  = conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"message": "Order not found"}), 404

    if row["status"] in ("Cancelled", "Returned"):
        conn.close()
        return jsonify({"message": f"❌ Order #{oid} is {row['status'].lower()} — its stock has already "
                                    f"been restored, so its status can't be changed"}), 400

    conn.execute("UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
                 (canonical, datetime.now().isoformat(), oid))
    conn.commit()
    conn.close()
    notify("order_status_change", f"Order #{oid} ({row['part_name']}): {row['status']} → {canonical}")
    dispatch_webhook("order_status_change", {"order_id": oid, "part_name": row["part_name"],
                                              "old_status": row["status"], "new_status": canonical})
    return jsonify({"message": f"✅ Order #{oid} → '{canonical}'", "status": canonical})


@app.route("/api/orders")
def get_orders():
    conn = get_db()
    rows = conn.execute("""
        SELECT o.*,
               EXISTS(
                   SELECT 1 FROM payments p
                   WHERE p.reference_type = 'order' AND p.reference_id = o.id
                     AND p.direction = 'incoming' AND p.status = 'succeeded'
               )
               AND NOT EXISTS(
                   SELECT 1 FROM payments p
                   WHERE p.reference_type = 'order' AND p.reference_id = o.id
                     AND p.direction = 'refund' AND p.status = 'succeeded'
               ) AS paid,
               EXISTS(
                   SELECT 1 FROM payments p
                   WHERE p.reference_type = 'order' AND p.reference_id = o.id
                     AND p.direction = 'refund' AND p.status = 'succeeded'
               ) AS refunded
        FROM orders o
        ORDER BY o.id DESC
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/orders/<int:oid>")
def get_order(oid):
    conn = get_db()
    row  = conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
    conn.close()
    return (jsonify(dict(row)), 200) if row else (jsonify({"message": "Not found"}), 404)


# ─────────────────────────────────────────────────────────
# RFID
# ─────────────────────────────────────────────────────────

@app.route("/api/rfid/<tag>")
def lookup_rfid(tag):
    conn = get_db()
    row  = conn.execute(
        "SELECT * FROM inventory WHERE rfid_tag = ?", (tag,)
    ).fetchone()
    conn.close()
    return (jsonify(dict(row)), 200) if row else (jsonify({"message": "RFID tag not found"}), 404)


@app.route("/api/rfid/<tag>/scan", methods=["POST"])
def scan_rfid(tag):
    data = request.get_json(silent=True) or {}
    qty  = int(data.get("qty", 1))

    conn = get_db()
    row  = conn.execute(
        "SELECT * FROM inventory WHERE rfid_tag = ?", (tag,)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({"message": "RFID tag not found"}), 404

    if qty > row["quantity"]:
        conn.close()
        return jsonify({
            "message": (
                f"❌ Insufficient stock for '{row['part_name']}'. "
                f"Requested: {qty} {row['unit']}, "
                f"Available: {row['quantity']} {row['unit']}."
            ),
            "insufficient_stock": True,
            "available": row["quantity"],
            "requested": qty,
        }), 400

    new_qty = row["quantity"] - qty
    conn.execute("UPDATE inventory SET quantity = ? WHERE id = ?", (new_qty, row["id"]))
    conn.commit()

    reorder = maybe_trigger_reorder(conn, row["id"])

    conn.execute("""
        INSERT INTO scan_events
            (inventory_id, scan_type, tag_value, qty_consumed, order_triggered, scanned_at)
        VALUES (?, 'RFID', ?, ?, ?, ?)
    """, (
        row["id"], tag, qty,
        1 if (reorder and reorder["triggered"]) else 0,
        datetime.now().isoformat(),
    ))
    conn.commit()
    conn.close()

    return jsonify({
        "item": {**dict(row), "quantity": new_qty},
        "consumed": qty,
        "new_stock": new_qty,
        "reorder": reorder,
        "message": (
            f"✅ Consumed {qty}. Stock: {new_qty}/{row['reorder_at']} threshold."
            + (f"\n🔔 Auto-reorder triggered for {reorder['reorder_qty']} units!"
               if reorder and reorder["triggered"] else "")
        ),
    })


# ─────────────────────────────────────────────────────────
# Barcode
# ─────────────────────────────────────────────────────────

@app.route("/api/barcode/<code>")
def lookup_barcode(code):
    conn = get_db()
    row  = conn.execute(
        "SELECT * FROM inventory WHERE barcode = ?", (code,)
    ).fetchone()
    conn.close()
    return (jsonify(dict(row)), 200) if row else (jsonify({"message": "Barcode not found"}), 404)


@app.route("/api/barcode/<code>/scan", methods=["POST"])
def scan_barcode(code):
    data = request.get_json(silent=True) or {}
    qty  = int(data.get("qty", 1))

    conn = get_db()
    row  = conn.execute(
        "SELECT * FROM inventory WHERE barcode = ?", (code,)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({"message": "Barcode not found"}), 404

    if qty > row["quantity"]:
        conn.close()
        return jsonify({
            "message": (
                f"❌ Insufficient stock for '{row['part_name']}'. "
                f"Requested: {qty} {row['unit']}, "
                f"Available: {row['quantity']} {row['unit']}."
            ),
            "insufficient_stock": True,
            "available": row["quantity"],
            "requested": qty,
        }), 400

    new_qty = row["quantity"] - qty
    conn.execute("UPDATE inventory SET quantity = ? WHERE id = ?", (new_qty, row["id"]))
    conn.commit()

    reorder = maybe_trigger_reorder(conn, row["id"])

    conn.execute("""
        INSERT INTO scan_events
            (inventory_id, scan_type, tag_value, qty_consumed, order_triggered, scanned_at)
        VALUES (?, 'BARCODE', ?, ?, ?, ?)
    """, (
        row["id"], code, qty,
        1 if (reorder and reorder["triggered"]) else 0,
        datetime.now().isoformat(),
    ))
    conn.commit()
    conn.close()

    return jsonify({
        "item": {**dict(row), "quantity": new_qty},
        "consumed": qty,
        "new_stock": new_qty,
        "reorder": reorder,
        "message": (
            f"✅ Consumed {qty}. Stock: {new_qty}/{row['reorder_at']} threshold."
            + (f"\n🔔 Auto-reorder triggered for {reorder['reorder_qty']} units!"
               if reorder and reorder["triggered"] else "")
        ),
    })


# ─────────────────────────────────────────────────────────
# Chat / AI  ← ALL LANGUAGE FIXES ARE HERE
# ─────────────────────────────────────────────────────────

# _translate_json_response removed — responses are always English.


@app.route("/api/chat", methods=["POST"])
def chat():
    try:
        data    = request.get_json()
        message = data.get("message", "")

        # helper: return a JSON response (always English) and remember this
        # turn for conversation memory. Session-scoped, so it works for
        # guests too (same mechanism as the "guest" audit-log attribution
        # elsewhere) — capped at the last 6 exchanges since this is context
        # for the next message, not a transcript to keep growing forever.
        def reply(payload: dict, status: int = 200):
            if status == 200:
                history = session.get("chat_history", [])
                history.append({"role": "user", "content": message})
                history.append({"role": "assistant", "content": payload.get("message", "")})
                session["chat_history"] = history[-12:]
                session.modified = True
            return jsonify(payload), status

        # ── 1. Detect language & normalise to English for the LLM ──
        user_language   = detect_language(message)
        english_message = message

        if user_language != "english":
            english_message = translate_to_english(message)

        if CHAT_DEBUG:
            print("Language:", user_language)
            print("English :", english_message)

        # ── Fast-path: deterministic report-query answers, no LLM round-
        # trip needed (and it still works if Ollama is offline) ────────
        report_answer = answer_report_query(english_message)
        if report_answer:
            return reply({"message": report_answer})

        # ── 2. Classify intent (always in English), with recent
        # conversation history so a follow-up like "make that 300
        # instead" can refer back to what was just discussed ──────────
        extracted = classify_intent(english_message, session.get("chat_history", []))
        intent    = extracted.get("intent")
        conn      = get_db()

        # ── CREATE ORDER ────────────────────────────────────────────
        if intent == "create_order":
            inv_id    = extracted.get("inventory_id")
            part_name = extracted.get("part_name")

            # ── Fallback: if LLM returned null part_name, mine the message ──
            if not part_name and not inv_id:
                # Strip common filler words and use whatever nouns remain
                filler = {"i", "need", "want", "order", "get", "buy", "please",
                          "me", "some", "the", "a", "an", "of", "for", "give",
                          "can", "you", "us", "we", "our", "stock"}
                words  = [w for w in english_message.lower().split()
                          if w.isalpha() and w not in filler and len(w) > 2]
                if words:
                    part_name = " ".join(words)
                    if CHAT_DEBUG: print(f"FALLBACK part_name from message: '{part_name}'")

            # ── Multi-strategy fuzzy inventory lookup ────────────────
            inv_row    = None
            candidates = []   # used when multiple items match

            if inv_id:
                # Exact ID match — fastest, always wins
                inv_row = conn.execute(
                    "SELECT * FROM inventory WHERE id = ?", (inv_id,)
                ).fetchone()

            elif part_name:
                pn_lower = part_name.strip().lower()

                # Strategy 1: exact substring match  e.g. "steel rod" in "Steel Rod 20mm"
                rows = conn.execute(
                    "SELECT * FROM inventory WHERE LOWER(part_name) LIKE LOWER(?)",
                    (f"%{pn_lower}%",),
                ).fetchall()

                if len(rows) == 1:
                    inv_row = rows[0]                 # unambiguous → use it
                elif len(rows) > 1:
                    candidates = list(rows)           # multiple → ask user

                else:
                    # Strategy 2: every word in part_name must appear somewhere in db name
                    # e.g. ["steel", "rod"] both appear in "Steel Rod 20mm"
                    words = [w for w in pn_lower.split() if len(w) > 2]
                    all_items = conn.execute("SELECT * FROM inventory").fetchall()

                    scored = []
                    for item in all_items:
                        item_lower = item["part_name"].lower()
                        hits = sum(1 for w in words if w in item_lower)
                        if hits > 0:
                            scored.append((hits, item))

                    scored.sort(key=lambda x: -x[0])

                    if scored:
                        best_score = scored[0][0]
                        top = [item for score, item in scored if score == best_score]

                        if len(top) == 1:
                            inv_row = top[0]          # clear winner
                        else:
                            candidates = top          # tie → ask user

            # ── Multiple matches: ask user to pick ──────────────────
            if candidates and not inv_row:
                conn.close()
                return reply({
                    "message": (
                        f"🔍 Found {len(candidates)} items matching '{part_name}'. "
                        "Which one did you mean? Click a row to select it."
                    ),
                    "ambiguous": True,
                    "matches": [
                        {
                            "id":        r["id"],
                            "part_name": r["part_name"],
                            "material":  r["material"],
                            "quantity":  r["quantity"],
                            "unit":      r["unit"],
                        }
                        for r in candidates
                    ],
                    "pending_qty": int(extracted.get("quantity") or 1),
                })

            # ── No match at all: show full inventory ─────────────────
            if not inv_row:
                items = conn.execute(
                    "SELECT id, part_name, material, quantity, unit FROM inventory ORDER BY part_name"
                ).fetchall()
                conn.close()
                return reply({
                    "message": (
                        f"⚠️ I couldn't find '{part_name or 'that item'}' in inventory. "
                        "Please choose an item from the list below, "
                        "or go to the Inventory tab to add it first."
                    ),
                    "inventory": [dict(r) for r in items],
                })

            qty = int(extracted.get("quantity") or 1)

            if qty > inv_row["quantity"]:
                conn.close()
                return reply({
                    "message": (
                        f"❌ Insufficient stock for '{inv_row['part_name']}'. "
                        f"You requested {qty} {inv_row['unit'] or 'pcs'} but only "
                        f"{inv_row['quantity']} {inv_row['unit'] or 'pcs'} are available. "
                        f"Please reduce the order quantity or wait for a restock."
                    ),
                    "insufficient_stock": True,
                    "available": inv_row["quantity"],
                    "requested": qty,
                    "part_name": inv_row["part_name"],
                    "unit": inv_row["unit"] or "pcs",
                }, 400)

            conn.execute("""
                INSERT INTO orders
                    (inventory_id, part_name, material, quantity, specs, deadline, status, payment_method, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'Received', 'cod', ?)
            """, (
                inv_row["id"],
                inv_row["part_name"],
                inv_row["material"],
                qty,
                extracted.get("specs") or "",
                extracted.get("deadline") or "",
                datetime.now().isoformat(),
            ))
            conn.commit()
            new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

            new_qty = inv_row["quantity"] - qty
            conn.execute(
                "UPDATE inventory SET quantity = ? WHERE id = ?",
                (new_qty, inv_row["id"])
            )
            conn.commit()

            reorder = maybe_trigger_reorder(conn, inv_row["id"])
            conn.close()

            dispatch_webhook("order_created", {
                "order_id": new_id, "part_name": inv_row["part_name"],
                "material": inv_row["material"], "quantity": qty,
            })

            reorder_msg = ""
            if reorder and reorder["triggered"]:
                reorder_msg = (
                    f"\n🔔 Stock hit threshold ({new_qty} ≤ {inv_row['reorder_at']})! "
                    f"Auto-reorder triggered for {reorder['reorder_qty']} units."
                )

            return reply({
                "message": (
                    f"✅ Order #{new_id} created — "
                    f"{qty} × {inv_row['part_name']} ({inv_row['material'] or 'N/A'}). "
                    f"Stock remaining: {new_qty} {inv_row['unit'] or 'pcs'}."
                    + reorder_msg
                ),
                "order": {
                    "id": new_id,
                    "inventory_id": inv_row["id"],
                    "part_name": inv_row["part_name"],
                    "material": inv_row["material"],
                    "quantity": qty,
                    "status": "Received",
                    "new_stock": new_qty,
                },
            })

        # ── LIST ORDERS ─────────────────────────────────────────────
        elif intent == "list_orders":
            rows = conn.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()
            conn.close()
            return reply({"orders": [dict(r) for r in rows]})

        # ── QUERY ORDER ─────────────────────────────────────────────
        elif intent == "query_order":
            oid = extracted.get("order_id")
            row = conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
            conn.close()
            if row:
                return reply({"order": dict(row)})
            return reply({"message": "Order not found"}, 404)

        # ── UPDATE STATUS ───────────────────────────────────────────
        elif intent == "update_status":
            oid = extracted.get("order_id")
            row = conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
            if not row:
                conn.close()
                return reply({"message": "Order not found"}, 404)

            requested = (extracted.get("new_status") or "").strip()

            STATUS_MAP = {
                "received":   "Received",
                "in review":  "In Review",
                "inreview":   "In Review",
                "review":     "In Review",
                "accepted":   "Accepted",
                "accept":     "Accepted",
                "approve":    "Accepted",
                "approved":   "Accepted",
                "done":       "Accepted",
                "complete":   "Accepted",
                "completed":  "Accepted",
            }

            if requested and requested.lower() in STATUS_MAP:
                new_status = STATUS_MAP[requested.lower()]
            else:
                current_idx = STATUS_FLOW.index(row["status"]) if row["status"] in STATUS_FLOW else -1
                if current_idx < len(STATUS_FLOW) - 1:
                    new_status = STATUS_FLOW[current_idx + 1]
                else:
                    conn.close()
                    return reply({"message": f"⚠️ Order #{oid} is already at the final status: '{row['status']}'"})

            conn.execute("UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
                         (new_status, datetime.now().isoformat(), oid))
            conn.commit()
            conn.close()
            notify("order_status_change", f"Order #{oid} ({row['part_name']}): {row['status']} → {new_status}")
            dispatch_webhook("order_status_change", {"order_id": oid, "part_name": row["part_name"],
                                                       "old_status": row["status"], "new_status": new_status})
            return reply({"message": f"✅ Order #{oid} status set to '{new_status}'"})

        # ── LOG QUALITY ─────────────────────────────────────────────
        elif intent == "log_quality":
            oid  = extracted.get("order_id")
            note = extracted.get("quality_note") or english_message  # store English note in DB
            conn.execute("""
                INSERT INTO quality_logs (order_id, note, log_time)
                VALUES (?, ?, ?)
            """, (oid, note, datetime.now().isoformat()))
            conn.commit()
            conn.close()
            return reply({"message": "✅ Quality log added"})

        # ── LIST INVENTORY ──────────────────────────────────────────
        elif intent == "list_inventory":
            rows = conn.execute("SELECT * FROM inventory ORDER BY part_name").fetchall()
            conn.close()
            return reply({
                "message": "📦 Here's your current inventory:",
                "inventory": [dict(r) for r in rows],
            })

        # ── QUERY INVENTORY ─────────────────────────────────────────
        elif intent == "query_inventory":
            inv_id    = extracted.get("inventory_id")
            part_name = extracted.get("part_name")
            row = None
            if inv_id:
                row = conn.execute(
                    "SELECT * FROM inventory WHERE id = ?", (inv_id,)
                ).fetchone()
            elif part_name:
                row = conn.execute(
                    "SELECT * FROM inventory WHERE LOWER(part_name) LIKE LOWER(?)",
                    (f"%{part_name}%",),
                ).fetchone()
            conn.close()
            if row:
                return reply({"item": dict(row)})
            return reply({"message": "Inventory item not found"}, 404)

        # ── CONSUME STOCK ───────────────────────────────────────────
        elif intent == "consume_stock":
            inv_id = extracted.get("inventory_id")
            qty    = int(extracted.get("quantity") or 1)
            row    = conn.execute(
                "SELECT * FROM inventory WHERE id = ?", (inv_id,)
            ).fetchone()
            if not row:
                conn.close()
                return reply({"message": "Inventory item not found"}, 404)

            if qty > row["quantity"]:
                conn.close()
                return reply({
                    "message": (
                        f"❌ Insufficient stock for '{row['part_name']}'. "
                        f"Requested: {qty} {row['unit']}, "
                        f"Available: {row['quantity']} {row['unit']}."
                    ),
                    "insufficient_stock": True,
                    "available": row["quantity"],
                    "requested": qty,
                }, 400)

            new_qty = row["quantity"] - qty
            conn.execute("UPDATE inventory SET quantity = ? WHERE id = ?", (new_qty, inv_id))
            conn.commit()
            reorder = maybe_trigger_reorder(conn, inv_id)
            conn.close()

            msg = f"✅ Consumed {qty} × {row['part_name']}. Stock now: {new_qty}"
            if reorder and reorder["triggered"]:
                msg += f"\n🔔 Reorder triggered for {reorder['reorder_qty']} units."
            return reply({"message": msg})

        # ── REPORT QUERY ─────────────────────────────────────────────
        # The fast-path above catches most phrasings before the LLM is
        # even called; this only fires when the LLM recognized the intent
        # on a phrasing the keyword matcher didn't.
        elif intent == "report_query":
            conn.close()
            answer = answer_report_query(english_message)
            return reply({"message": answer or "📊 Check the Reports tab for inventory valuation, supplier scorecards, and fulfillment metrics."})

        # ── UNKNOWN ─────────────────────────────────────────────────
        else:
            conn.close()
            return reply({
                "message": (
                    "I didn't understand that. Try:\n"
                    "• 'Order 50 Steel Rods'\n"
                    "• 'Show inventory'\n"
                    "• 'Update order #3'\n"
                    "• 'Log quality for order #1: inspection passed'"
                )
            })

    except Exception as e:
        return jsonify({"message": f"Server error: {e}"}), 500


@app.route("/api/chat/clear", methods=["POST"])
def clear_chat_history():
    """Start a fresh conversation — drops the memory classify_intent()
    uses for context, without affecting anything already saved to the
    database (orders, inventory, etc. are untouched)."""
    session.pop("chat_history", None)
    return jsonify({"message": "✅ Conversation history cleared"})


# ─────────────────────────────────────────────────────────
# Confirm-order  (used after ambiguous-match picker)
# Frontend posts: { inventory_id, quantity }
# ─────────────────────────────────────────────────────────

@app.route("/api/chat/confirm-order", methods=["POST"])
def confirm_order():
    """
    Called when the user clicks a row in the ambiguous-match picker.
    Body: { "inventory_id": <int>, "quantity": <int> }
    """
    data   = request.get_json(silent=True) or {}
    inv_id = data.get("inventory_id")
    qty    = int(data.get("quantity") or 1)
    payment_method = data.get("payment_method")
    if payment_method not in ("cod", "prepaid"):
        payment_method = "cod"

    if not inv_id:
        return jsonify({"message": "inventory_id is required"}), 400

    conn    = get_db()
    inv_row = conn.execute("SELECT * FROM inventory WHERE id = ?", (inv_id,)).fetchone()
    if not inv_row:
        conn.close()
        return jsonify({"message": "Inventory item not found"}), 404

    if qty > inv_row["quantity"]:
        conn.close()
        return jsonify({
            "message": (
                f"Insufficient stock for '{inv_row['part_name']}'. "
                f"Requested: {qty} {inv_row['unit'] or 'pcs'}, "
                f"Available: {inv_row['quantity']} {inv_row['unit'] or 'pcs'}."
            ),
            "insufficient_stock": True,
            "available": inv_row["quantity"],
            "requested": qty,
        }), 400

    conn.execute(
        "INSERT INTO orders (inventory_id, part_name, material, quantity, specs, deadline, status, payment_method, created_at) "
        "VALUES (?, ?, ?, ?, \'\', \'\', \'Received\', ?, ?)",
        (inv_row["id"], inv_row["part_name"], inv_row["material"], qty, payment_method, datetime.now().isoformat()),
    )
    conn.commit()
    new_id  = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    new_qty = inv_row["quantity"] - qty
    conn.execute("UPDATE inventory SET quantity = ? WHERE id = ?", (new_qty, inv_row["id"]))
    conn.commit()

    reorder = maybe_trigger_reorder(conn, inv_row["id"])
    reorder_msg = ""
    if reorder and reorder["triggered"]:
        reorder_msg = (
            f" Stock hit threshold ({new_qty} <= {inv_row['reorder_at']})! "
            f"Auto-reorder triggered for {reorder['reorder_qty']} units."
        )
    conn.close()

    dispatch_webhook("order_created", {
        "order_id": new_id, "part_name": inv_row["part_name"],
        "material": inv_row["material"], "quantity": qty,
    })

    return jsonify({
        "message": (
            f"Order #{new_id} created: "
            f"{qty} x {inv_row['part_name']} ({inv_row['material'] or 'N/A'}). "
            f"Stock remaining: {new_qty} {inv_row['unit'] or 'pcs'}."
            + reorder_msg
        ),
        "order": {
            "id":           new_id,
            "inventory_id": inv_row["id"],
            "part_name":    inv_row["part_name"],
            "material":     inv_row["material"],
            "quantity":     qty,
            "status":       "Received",
            "new_stock":    new_qty,
        },
    })


# ─────────────────────────────────────────────────────────
# DEMAND MANAGEMENT ROUTES
# ─────────────────────────────────────────────────────────

# Demand-management tables (production_plans, suppliers) are created once at
# startup by database.init_db() — previously this ran a CREATE TABLE IF NOT
# EXISTS on every single request to four different routes.

# ── /api/demand/forecast ─────────────────────────────────
@app.route("/api/demand/forecast")
def demand_forecast():
    conn  = get_db()
    items = conn.execute("SELECT * FROM inventory").fetchall()

    result = []
    for item in items:
        rows = conn.execute("""
            SELECT DATE(scanned_at::timestamp) as day, SUM(qty_consumed) as total
            FROM   scan_events
            WHERE  inventory_id = ?
              AND  scanned_at::timestamp >= NOW() - INTERVAL '30 days'
            GROUP  BY DATE(scanned_at::timestamp)
            ORDER  BY day
        """, (item["id"],)).fetchall()

        daily_totals = [r["total"] for r in rows]

        if daily_totals:
            avg_daily    = round(sum(daily_totals) / 30, 2)
            forecast_7d  = round(avg_daily * 7,  1)
            forecast_30d = round(avg_daily * 30, 1)
            trend = "stable"
            if len(daily_totals) >= 6:
                first_half  = statistics.mean(daily_totals[:len(daily_totals)//2])
                second_half = statistics.mean(daily_totals[len(daily_totals)//2:])
                if second_half > first_half * 1.2:
                    trend = "rising"
                elif second_half < first_half * 0.8:
                    trend = "falling"
        else:
            avg_daily    = 0
            forecast_7d  = 0
            forecast_30d = 0
            trend        = "no data"

        days_until_out = (
            round(item["quantity"] / avg_daily)
            if avg_daily > 0 else None
        )

        result.append({
            "id":            item["id"],
            "part_name":     item["part_name"],
            "material":      item["material"],
            "unit":          item["unit"],
            "current_stock": item["quantity"],
            "reorder_at":    item["reorder_at"],
            "avg_daily_use": avg_daily,
            "forecast_7d":   forecast_7d,
            "forecast_30d":  forecast_30d,
            "trend":         trend,
            "days_until_stockout": days_until_out,
            "history":       [{"day": r["day"], "consumed": r["total"]} for r in rows],
        })

    conn.close()
    return jsonify(result)


# ── /api/demand/production-plan ──────────────────────────
@app.route("/api/demand/production-plan", methods=["GET", "POST"])
def production_plan():
    conn = get_db()

    if request.method == "GET":
        rows = conn.execute("""
            SELECT pp.*, i.unit, i.quantity as current_stock
            FROM   production_plans pp
            LEFT JOIN inventory i ON i.id = pp.inventory_id
            ORDER  BY pp.start_date, pp.id DESC
        """).fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])

    data = request.get_json(silent=True) or {}
    if not data.get("part_name") or not data.get("target_qty"):
        conn.close()
        return jsonify({"message": "❌ part_name and target_qty are required"}), 400

    inv_id = data.get("inventory_id")
    if not inv_id and data.get("part_name"):
        row = conn.execute(
            "SELECT id FROM inventory WHERE LOWER(part_name) LIKE LOWER(?)",
            (f"%{data['part_name']}%",)
        ).fetchone()
        if row:
            inv_id = row["id"]

    conn.execute("""
        INSERT INTO production_plans
            (inventory_id, part_name, target_qty, start_date, end_date, notes, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        inv_id,
        data["part_name"],
        int(data["target_qty"]),
        data.get("start_date", ""),
        data.get("end_date", ""),
        data.get("notes", ""),
        data.get("status", "Planned"),
        datetime.now().isoformat(),
    ))
    conn.commit()
    new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return jsonify({"message": f"✅ Production plan #{new_id} created", "id": new_id})


@app.route("/api/demand/production-plan/<int:pid>", methods=["PATCH", "DELETE"])
def production_plan_item(pid):
    conn = get_db()

    if request.method == "DELETE":
        conn.execute("DELETE FROM production_plans WHERE id = ?", (pid,))
        conn.commit()
        conn.close()
        return jsonify({"message": f"✅ Plan #{pid} deleted"})

    data   = request.get_json(silent=True) or {}
    fields = []
    vals   = []
    for col in ("target_qty", "start_date", "end_date", "notes", "status"):
        if col in data:
            fields.append(f"{col} = ?")
            vals.append(data[col])
    if not fields:
        conn.close()
        return jsonify({"message": "Nothing to update"}), 400

    vals.append(pid)
    conn.execute(f"UPDATE production_plans SET {', '.join(fields)} WHERE id = ?", vals)
    conn.commit()
    conn.close()
    return jsonify({"message": f"✅ Plan #{pid} updated"})


# ── /api/demand/suppliers ────────────────────────────────
@app.route("/api/demand/suppliers", methods=["GET", "POST"])
def suppliers():
    conn = get_db()

    if request.method == "GET":
        rows = conn.execute("""
            SELECT s.*, i.part_name as inv_part_name, i.quantity as current_stock
            FROM   suppliers s
            LEFT JOIN inventory i ON i.id = s.inventory_id
            ORDER  BY s.lead_days
        """).fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])

    data = request.get_json(silent=True) or {}
    if not data.get("name"):
        conn.close()
        return jsonify({"message": "❌ supplier name is required"}), 400

    inv_id = data.get("inventory_id")
    if not inv_id and data.get("part_name"):
        row = conn.execute(
            "SELECT id FROM inventory WHERE LOWER(part_name) LIKE LOWER(?)",
            (f"%{data['part_name']}%",)
        ).fetchone()
        if row:
            inv_id = row["id"]

    conn.execute("""
        INSERT INTO suppliers
            (name, inventory_id, part_name, lead_days, reliability, contact, notes, unit_cost, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data["name"],
        inv_id,
        data.get("part_name", ""),
        int(data.get("lead_days", 7)),
        int(data.get("reliability", 90)),
        data.get("contact", ""),
        data.get("notes", ""),
        float(data.get("unit_cost") or 0),
        datetime.now().isoformat(),
    ))
    conn.commit()
    new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return jsonify({"message": f"✅ Supplier '{data['name']}' added (ID #{new_id})", "id": new_id})


@app.route("/api/demand/suppliers/<int:sid>", methods=["DELETE"])
def delete_supplier(sid):
    conn = get_db()
    conn.execute("DELETE FROM suppliers WHERE id = ?", (sid,))
    conn.commit()
    conn.close()
    return jsonify({"message": f"✅ Supplier #{sid} removed"})


# ── /api/demand/gap-analysis ─────────────────────────────
@app.route("/api/demand/gap-analysis")
def gap_analysis():
    conn    = get_db()
    items   = conn.execute("SELECT * FROM inventory").fetchall()
    horizon = int(request.args.get("days", 30))

    result = []
    for item in items:
        row = conn.execute("""
            SELECT COALESCE(SUM(qty_consumed), 0) as total,
                   COUNT(DISTINCT DATE(scanned_at::timestamp))  as active_days
            FROM   scan_events
            WHERE  inventory_id = ?
              AND  scanned_at::timestamp  >= NOW() - INTERVAL '30 days'
        """, (item["id"],)).fetchone()

        total_consumed   = row["total"]
        avg_daily        = round(total_consumed / 30, 3)
        projected_demand = round(avg_daily * horizon, 1)
        gap              = round(item["quantity"] - projected_demand, 1)
        coverage_days    = round(item["quantity"] / avg_daily) if avg_daily > 0 else None

        supplier = conn.execute("""
            SELECT name, lead_days, reliability
            FROM   suppliers
            WHERE  inventory_id = ?
            ORDER  BY lead_days
            LIMIT  1
        """, (item["id"],)).fetchone()

        pending = conn.execute("""
            SELECT COALESCE(SUM(quantity), 0) as total
            FROM   orders
            WHERE  inventory_id = ? AND status IN ('Received', 'In Review')
        """, (item["id"],)).fetchone()["total"]

        status = "ok"
        if gap < 0:
            status = "critical"
        elif gap < item["reorder_at"]:
            status = "warning"

        result.append({
            "id":               item["id"],
            "part_name":        item["part_name"],
            "material":         item["material"],
            "unit":             item["unit"],
            "current_stock":    item["quantity"],
            "reorder_at":       item["reorder_at"],
            "avg_daily_use":    avg_daily,
            "projected_demand": projected_demand,
            "gap":              gap,
            "coverage_days":    coverage_days,
            "pending_orders":   pending,
            "supplier":         dict(supplier) if supplier else None,
            "status":           status,
            "horizon_days":     horizon,
        })

    conn.close()
    order_map = {"critical": 0, "warning": 1, "ok": 2}
    result.sort(key=lambda x: order_map.get(x["status"], 3))
    return jsonify(result)


# ─────────────────────────────────────────────────────────
# FUTURE PREDICTIONS  (AI-driven forecasting via linear regression)
# ─────────────────────────────────────────────────────────

def _linear_regression(xs, ys):
    """
    Simple ordinary-least-squares linear regression.
    Returns (slope, intercept, r_squared).
    """
    n = len(xs)
    if n < 2:
        return 0.0, float(ys[0]) if ys else 0.0, 0.0

    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    ss_xy  = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    ss_xx  = sum((x - mean_x) ** 2 for x in xs)

    if ss_xx == 0:
        return 0.0, mean_y, 0.0

    slope     = ss_xy / ss_xx
    intercept = mean_y - slope * mean_x

    # R²
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0

    return slope, intercept, max(0.0, min(1.0, r2))


def _predict_stockout(current_stock, daily_consumption_fn, max_days=365):
    """
    Walk forward day by day using the consumption function until stock ≤ 0.
    consumption_fn(day_offset) → predicted units consumed that day.
    Returns day offset or None if never runs out within max_days.
    """
    stock = current_stock
    for d in range(1, max_days + 1):
        consume = max(0.0, daily_consumption_fn(d))
        stock  -= consume
        if stock <= 0:
            return d
    return None


@app.route("/api/demand/predictions")
def demand_predictions():
    """
    AI-driven future predictions for every inventory item.

    For each item we:
    1. Pull the last 60 days of daily consumption from scan_events.
    2. Fit a linear regression (trend model) over daily totals.
    3. Project forward for 7 / 14 / 30 / 60 / 90 days.
    4. Estimate stockout date with confidence bounds.
    5. Recommend a reorder action with urgency level.
    6. Return a 30-day day-by-day forecast array for charting.
    """
    conn    = get_db()
    horizon = int(request.args.get("days", 30))
    items   = conn.execute("SELECT * FROM inventory").fetchall()

    results = []

    for item in items:
        # ── 1. Pull raw daily consumption for last 60 days ──────────
        rows = conn.execute("""
            SELECT
                CAST(CURRENT_DATE - DATE(scanned_at::timestamp) AS INTEGER) AS days_ago,
                DATE(scanned_at::timestamp)   AS day,
                SUM(qty_consumed)  AS consumed
            FROM scan_events
            WHERE inventory_id = ?
              AND scanned_at::timestamp >= NOW() - INTERVAL '60 days'
            GROUP BY DATE(scanned_at::timestamp)
            ORDER BY day ASC
        """, (item["id"],)).fetchall()

        has_data = len(rows) > 0

        if has_data:
            # x = day index (0 = oldest data point), y = units consumed
            xs = list(range(len(rows)))
            ys = [float(r["consumed"]) for r in rows]

            slope, intercept, r2 = _linear_regression(xs, ys)

            # The "current" day is len(rows) steps from the first data point.
            # We project forward from there.
            offset_now  = len(rows)

            def predicted_daily(day_offset):
                return intercept + slope * (offset_now + day_offset)

            avg_recent  = sum(ys[-7:]) / min(7, len(ys))
            avg_overall = sum(ys) / len(ys)

            # Residual std-dev for confidence interval
            preds_in   = [intercept + slope * x for x in xs]
            residuals  = [abs(y - p) for y, p in zip(ys, preds_in)]
            residual_sd = (sum(r ** 2 for r in residuals) / len(residuals)) ** 0.5 if residuals else 0
        else:
            slope       = 0.0
            intercept   = 0.0
            r2          = 0.0
            offset_now  = 0
            avg_recent  = 0.0
            avg_overall = 0.0
            residual_sd = 0.0

            def predicted_daily(day_offset):
                return 0.0

        # ── 2. Projected demand for multiple horizons ────────────────
        def project_horizon(days):
            total = sum(max(0.0, predicted_daily(d)) for d in range(1, days + 1))
            lo    = sum(max(0.0, predicted_daily(d) - residual_sd) for d in range(1, days + 1))
            hi    = sum(max(0.0, predicted_daily(d) + residual_sd) for d in range(1, days + 1))
            return round(total, 1), round(lo, 1), round(hi, 1)

        p7,  p7_lo,  p7_hi  = project_horizon(7)
        p14, p14_lo, p14_hi = project_horizon(14)
        p30, p30_lo, p30_hi = project_horizon(30)
        p60, p60_lo, p60_hi = project_horizon(60)
        p90, p90_lo, p90_hi = project_horizon(90)

        # ── 3. Stockout date prediction ──────────────────────────────
        current_stock = item["quantity"]

        stockout_day      = _predict_stockout(current_stock, predicted_daily) if has_data else None
        stockout_day_lo   = _predict_stockout(current_stock,
            lambda d, _sd=residual_sd: predicted_daily(d) + _sd) if has_data else None
        stockout_day_hi   = _predict_stockout(current_stock,
            lambda d, _sd=residual_sd: max(0.0, predicted_daily(d) - _sd)) if has_data else None

        # ── 4. Reorder recommendation ────────────────────────────────
        supplier = conn.execute("""
            SELECT lead_days, reliability, name
            FROM   suppliers
            WHERE  inventory_id = ?
            ORDER  BY lead_days
            LIMIT  1
        """, (item["id"],)).fetchone()

        lead_days   = supplier["lead_days"]   if supplier else 7
        reliability = supplier["reliability"] if supplier else 90

        # Safety stock = lead_days × avg_daily × (2 - reliability/100)
        avg_daily_for_ss = avg_recent if has_data else 0.0
        safety_stock     = math.ceil(lead_days * avg_daily_for_ss * (2 - reliability / 100))

        # Reorder point: enough stock to last through lead time + safety stock
        reorder_point = math.ceil(lead_days * avg_daily_for_ss + safety_stock)

        # How many to order: cover horizon demand + safety stock - current stock
        projected_for_horizon = sum(max(0.0, predicted_daily(d)) for d in range(1, horizon + 1))
        reorder_qty = max(0, math.ceil(projected_for_horizon + safety_stock - current_stock))

        # Urgency
        if stockout_day is not None and stockout_day <= lead_days:
            urgency = "critical"    # will run out before supplier can deliver
        elif stockout_day is not None and stockout_day <= lead_days * 2:
            urgency = "high"
        elif current_stock <= reorder_point:
            urgency = "medium"
        elif has_data and slope > 0.1 and stockout_day is not None and stockout_day <= 30:
            urgency = "medium"
        else:
            urgency = "low"

        # ── 5. Day-by-day 30-day forecast for charting ───────────────
        today       = datetime.now().date()
        daily_chart = []
        running     = float(current_stock)

        for d in range(1, 31):
            consume    = max(0.0, predicted_daily(d))
            consume_lo = max(0.0, predicted_daily(d) - residual_sd)
            consume_hi = max(0.0, predicted_daily(d) + residual_sd)
            running   -= consume
            daily_chart.append({
                "date":          str(today.fromordinal(today.toordinal() + d)),
                "day":           d,
                "consumption":   round(consume,    2),
                "consume_lo":    round(consume_lo, 2),
                "consume_hi":    round(consume_hi, 2),
                "stock_eod":     round(max(0.0, running), 1),
            })

        # ── 6. Trend label & velocity ────────────────────────────────
        if not has_data:
            trend_label    = "no data"
            velocity_label = "unknown"
        elif slope >  0.5:
            trend_label    = "accelerating"
            velocity_label = f"+{round(slope, 2)}/day"
        elif slope >  0.05:
            trend_label    = "rising"
            velocity_label = f"+{round(slope, 2)}/day"
        elif slope < -0.5:
            trend_label    = "decelerating"
            velocity_label = f"{round(slope, 2)}/day"
        elif slope < -0.05:
            trend_label    = "falling"
            velocity_label = f"{round(slope, 2)}/day"
        else:
            trend_label    = "stable"
            velocity_label = "±0"

        results.append({
            "id":           item["id"],
            "part_name":    item["part_name"],
            "material":     item["material"],
            "unit":         item["unit"] or "pcs",
            "current_stock": current_stock,
            "reorder_at":   item["reorder_at"],

            # Model quality
            "has_data":     has_data,
            "data_points":  len(rows),
            "r_squared":    round(r2, 3),
            "slope":        round(slope, 4),
            "trend":        trend_label,
            "velocity":     velocity_label,
            "avg_daily":    round(avg_overall, 2),
            "avg_recent_7d": round(avg_recent, 2),
            "confidence":   round(r2 * 100),

            # Multi-horizon projections
            "forecast": {
                "7d":  {"demand": p7,  "lo": p7_lo,  "hi": p7_hi},
                "14d": {"demand": p14, "lo": p14_lo, "hi": p14_hi},
                "30d": {"demand": p30, "lo": p30_lo, "hi": p30_hi},
                "60d": {"demand": p60, "lo": p60_lo, "hi": p60_hi},
                "90d": {"demand": p90, "lo": p90_lo, "hi": p90_hi},
            },

            # Stockout prediction
            "stockout": {
                "days":    stockout_day,
                "days_lo": stockout_day_lo,
                "days_hi": stockout_day_hi,
                "date":    str(today.fromordinal(today.toordinal() + stockout_day))
                           if stockout_day else None,
            },

            # Reorder recommendation
            "reorder_rec": {
                "urgency":       urgency,
                "reorder_point": reorder_point,
                "safety_stock":  safety_stock,
                "suggested_qty": reorder_qty,
                "lead_days":     lead_days,
                "supplier":      supplier["name"] if supplier else None,
            },

            # Chart data
            "daily_forecast": daily_chart,
        })

    conn.close()

    # Sort: critical first, then high, medium, low, no-data
    urgency_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    results.sort(key=lambda x: (
        urgency_order.get(x["reorder_rec"]["urgency"], 4),
        -(x["current_stock"] == 0),
    ))

    return jsonify(results)


# ─────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    bootstrap_admin()
    from payments import payment_gateway_status
    pay_status = payment_gateway_status()
    debug_mode = os.environ.get("FLASK_DEBUG", "1") not in ("0", "false", "False")

    # Flask's debug reloader runs this module twice: once in a "watcher"
    # parent process and once in a "worker" child process (which alone has
    # WERKZEUG_RUN_MAIN=true set). Starting the backup thread in both would
    # double the backup cadence. When the reloader isn't in play at all
    # (debug_mode is False), there's only ever one process, so it's safe to
    # start unconditionally.
    if not debug_mode or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_backup_scheduler()

    print(f"OrderFlow AI — Ollama model '{OLLAMA_MODEL}' @ {OLLAMA_URL}")
    print("(override with OLLAMA_BASE_URL / OLLAMA_URL / OLLAMA_MODEL env vars)")
    print(f"Payments — provider: {pay_status['provider']}, mode: {pay_status['mode']}"
          + ("" if pay_status["configured"] else " (set STRIPE_SECRET_KEY to use real Stripe)"))
    print("Modules — production/BOM, notifications, warehouses, search, saved views, "
          "CSV import/export, barcode labels, webhooks, backups: all enabled.")
    print("(notifications need SMTP_*/TWILIO_*/SLACK_WEBHOOK_URL env vars to actually send — "
          "without them, alerts are logged, not delivered)")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=debug_mode)