"""
Payments — pays suppliers on purchase orders (outgoing) and collects
customer payments on sales orders (incoming).

Provider: Stripe, via the official `stripe` SDK, configured entirely
through environment variables — same pattern as OLLAMA_BASE_URL:

    export STRIPE_SECRET_KEY="sk_test_..."   # from your Stripe dashboard

If STRIPE_SECRET_KEY is not set, payments run in DEMO MODE: no network
call is made, no card is charged, and every payment is recorded with
provider='demo' and clearly labelled as such in every response. This
means the entire feature — UI, status tracking, downstream effects like
unblocking a PO — is fully testable with zero setup, and stays honest
about not touching real money until real keys are added.

IMPORTANT caveat: the Stripe code path below is written to Stripe's
documented API for a server-confirmed test payment (PaymentIntent with
the well-known test payment method `pm_card_visa`, which Stripe
provides specifically for this kind of automated/off-session test-mode
confirmation). It has NOT been exercised against a real Stripe account
in this environment — there is no way to do that without your actual
API keys. Test it once with your real test key before relying on it.
"""

import os
import uuid
from datetime import datetime

from flask import Blueprint, request, jsonify, session

from database import get_db
from auth import role_required, log_audit

payments_bp = Blueprint("payments", __name__)

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()

_stripe = None
if STRIPE_SECRET_KEY:
    try:
        import stripe as _stripe_module
        _stripe_module.api_key = STRIPE_SECRET_KEY
        _stripe = _stripe_module
    except ImportError:
        print("STRIPE_SECRET_KEY is set but the 'stripe' package isn't installed "
              "(pip install stripe) — falling back to demo mode.")
        _stripe = None


def payment_gateway_status():
    if STRIPE_SECRET_KEY and _stripe:
        mode = "test" if STRIPE_SECRET_KEY.startswith("sk_test_") else "live"
        return {"configured": True, "provider": "stripe", "mode": mode}
    return {"configured": False, "provider": "demo", "mode": "demo"}


def _process_payment(amount, currency, description):
    """Returns (status, provider, provider_payment_id, error_message)."""
    if _stripe:
        try:
            intent = _stripe.PaymentIntent.create(
                amount=int(round(amount * 100)),  # Stripe wants the smallest currency unit
                currency=currency,
                payment_method_types=["card"],
                payment_method="pm_card_visa",   # Stripe's documented test payment method
                confirm=True,
                description=description,
            )
            status = "succeeded" if intent.status == "succeeded" else intent.status
            return status, "stripe", intent.id, None
        except Exception as e:
            return "failed", "stripe", None, str(e)
    else:
        return "succeeded", "demo", f"demo_{uuid.uuid4().hex[:16]}", None


def _process_refund(provider, provider_payment_id, amount):
    """Returns (status, provider, provider_refund_id, error_message).
    Mirrors _process_payment above — same demo-mode-when-no-Stripe-key
    behavior, so refunds are just as testable with zero setup."""
    if _stripe and provider == "stripe" and provider_payment_id:
        try:
            refund = _stripe.Refund.create(
                payment_intent=provider_payment_id,
                amount=int(round(amount * 100)),
            )
            status = "succeeded" if refund.status in ("succeeded", "pending") else refund.status
            return status, "stripe", refund.id, None
        except Exception as e:
            return "failed", "stripe", None, str(e)
    else:
        return "succeeded", "demo", f"demo_refund_{uuid.uuid4().hex[:16]}", None


def refund_order_payment(conn, order_id, created_by):
    """Refund whatever was collected on a sales order, in full. Used by both
    the cancel/return flow (procurement.py) and the standalone refund route
    below. Returns None if there's nothing to refund (e.g. an unpaid COD
    order), otherwise a dict describing the refund."""
    payment = conn.execute("""
        SELECT * FROM payments
        WHERE reference_type = 'order' AND reference_id = ?
          AND direction = 'incoming' AND status = 'succeeded'
          AND NOT EXISTS (
              SELECT 1 FROM payments r
              WHERE r.direction = 'refund' AND r.status = 'succeeded'
                AND r.refund_of_payment_id = payments.id
          )
        ORDER BY id DESC LIMIT 1
    """, (order_id,)).fetchone()

    if not payment:
        return None

    status, provider, refund_id, error = _process_refund(
        payment["provider"], payment["provider_payment_id"], payment["amount"]
    )
    now = datetime.now().isoformat()
    conn.execute("""
        INSERT INTO payments (direction, reference_type, reference_id, amount, currency,
                               provider, provider_payment_id, status, created_by, created_at,
                               updated_at, refund_of_payment_id)
        VALUES ('refund', 'order', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (order_id, payment["amount"], payment["currency"], provider, refund_id,
          status, created_by, now, now, payment["id"]))
    conn.commit()

    return {"amount": payment["amount"], "status": status, "error": error}


# ─────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────

@payments_bp.route("/api/payments/status")
def get_payment_status():
    return jsonify(payment_gateway_status())


@payments_bp.route("/api/payments", methods=["GET"])
def list_payments():
    conn = get_db()
    rows = conn.execute("SELECT * FROM payments ORDER BY id DESC").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@payments_bp.route("/api/payments/purchase-order/<int:po_id>", methods=["POST"])
@role_required("admin", "operator")
def pay_purchase_order(po_id):
    conn = get_db()
    po = conn.execute("SELECT * FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()
    if not po:
        conn.close()
        return jsonify({"message": "Purchase order not found"}), 404
    if po["status"] not in ("approved", "sent", "partially_received", "received"):
        conn.close()
        return jsonify({"message": f"❌ Cannot pay a PO with status '{po['status']}' — approve it first"}), 400

    already_paid = conn.execute(
        "SELECT 1 FROM payments WHERE reference_type='purchase_order' AND reference_id=? AND status='succeeded'",
        (po_id,),
    ).fetchone()
    if already_paid:
        conn.close()
        return jsonify({"message": "❌ This purchase order has already been paid"}), 400

    amount = po["total_cost"] or 0
    if amount <= 0:
        conn.close()
        return jsonify({"message": "❌ This PO has no cost on file (no supplier unit_cost) — nothing to pay"}), 400

    status, provider, provider_id, error = _process_payment(
        amount, "usd", f"Purchase order #{po_id} - {po['part_name']}"
    )
    now = datetime.now().isoformat()
    cur = conn.execute("""
        INSERT INTO payments (direction, reference_type, reference_id, amount, currency,
                               provider, provider_payment_id, status, created_by, created_at, updated_at)
        VALUES ('outgoing', 'purchase_order', ?, ?, 'usd', ?, ?, ?, ?, ?, ?)
    """, (po_id, amount, provider, provider_id, status, session.get("username", "guest"), now, now))
    conn.commit()
    payment_id = cur.lastrowid
    conn.close()

    log_audit("pay_purchase_order", "purchase_order", po_id, f"${amount} via {provider}: {status}")

    if status == "succeeded":
        tag = " (demo — no real charge)" if provider == "demo" else ""
        return jsonify({
            "message": f"✅ Paid ${amount:,.2f} to supplier for PO #{po_id}{tag}",
            "payment_id": payment_id, "status": status, "provider": provider,
        })
    return jsonify({"message": f"❌ Payment failed: {error or 'unknown error'}",
                     "payment_id": payment_id, "status": status}), 402


@payments_bp.route("/api/payments/order/<int:order_id>", methods=["POST"])
@role_required("admin", "operator")
def pay_sales_order(order_id):
    """Collect a customer payment against a sales/production order. The
    `orders` table has no price field (it's a manufacturing order, not a
    priced line item), so the amount is entered at charge time — same as
    writing an invoice amount by hand."""
    data = request.get_json(silent=True) or {}
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        return jsonify({"message": "Order not found"}), 404

    already_paid = conn.execute(
        "SELECT 1 FROM payments WHERE reference_type='order' AND reference_id=? AND status='succeeded'",
        (order_id,),
    ).fetchone()
    if already_paid:
        conn.close()
        return jsonify({"message": "❌ This order has already been paid"}), 400

    try:
        amount = float(data.get("amount"))
        assert amount > 0
    except Exception:
        conn.close()
        return jsonify({"message": "❌ A positive 'amount' is required"}), 400

    status, provider, provider_id, error = _process_payment(
        amount, "usd", f"Order #{order_id} - {order['part_name']} x{order['quantity']}"
    )
    now = datetime.now().isoformat()
    cur = conn.execute("""
        INSERT INTO payments (direction, reference_type, reference_id, amount, currency,
                               provider, provider_payment_id, status, created_by, created_at, updated_at)
        VALUES ('incoming', 'order', ?, ?, 'usd', ?, ?, ?, ?, ?, ?)
    """, (order_id, amount, provider, provider_id, status, session.get("username", "guest"), now, now))
    conn.commit()
    payment_id = cur.lastrowid
    conn.close()

    log_audit("pay_order", "order", order_id, f"${amount} via {provider}: {status}")

    if status == "succeeded":
        tag = " (demo — no real charge)" if provider == "demo" else ""
        return jsonify({
            "message": f"✅ Collected ${amount:,.2f} from customer for order #{order_id}{tag}",
            "payment_id": payment_id, "status": status, "provider": provider,
        })
    return jsonify({"message": f"❌ Payment failed: {error or 'unknown error'}",
                     "payment_id": payment_id, "status": status}), 402


@payments_bp.route("/api/payments/order/<int:order_id>/refund", methods=["POST"])
@role_required("admin", "operator")
def refund_sales_order(order_id):
    """Standalone/goodwill refund — outside of cancelling or returning the
    order itself (those call refund_order_payment directly). Useful when an
    order stays Accepted but the customer still needs their money back."""
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        return jsonify({"message": "Order not found"}), 404

    result = refund_order_payment(conn, order_id, session.get("username", "guest"))
    conn.close()

    if not result:
        return jsonify({"message": "❌ Nothing to refund — this order hasn't been paid"}), 400

    log_audit("refund_order", "order", order_id, f"${result['amount']} — {result['status']}")

    if result["status"] == "succeeded":
        return jsonify({"message": f"✅ Refunded ${result['amount']:,.2f} for order #{order_id}",
                         "status": result["status"]})
    return jsonify({"message": f"❌ Refund failed: {result['error'] or 'unknown error'}",
                     "status": result["status"]}), 402
