"""
Alerts & notifications: stockout risk, order status changes, and late
suppliers, dispatched to whichever channels are configured — email (SMTP),
SMS (Twilio), Slack (incoming webhook). Every send attempt is logged to
notification_log regardless of outcome.

The whole point is that this app runs with ZERO of these configured: an
unconfigured channel doesn't raise, doesn't block the action it's attached
to, and doesn't pretend to have sent anything — it logs
status='skipped_not_configured' with a clear message and moves on. Nothing
about placing an order, changing a status, or triggering a reorder depends
on any notification succeeding.

Configuration (all optional, all via env vars):
    SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASSWORD, SMTP_FROM
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER
    SLACK_WEBHOOK_URL
"""

import os
import smtplib
from email.mime.text import MIMEText
from datetime import datetime

import requests
from flask import Blueprint, request, jsonify

from database import get_db

notifications_bp = Blueprint("notifications", __name__)

EVENT_TYPES = ["stockout_risk", "order_status_change", "late_supplier", "quality_fail"]
CHANNELS = ["email", "sms", "slack", "ntfy"]

SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "").strip()
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER).strip() or SMTP_USER
# STARTTLS on by default (what Gmail/Outlook/most hosted SMTP want on 587).
# Set SMTP_TLS=0 for a plain internal relay / local test server that doesn't
# offer STARTTLS. Auth (login) is skipped automatically when USER/PASSWORD
# aren't both set, so an open internal relay works with just SMTP_HOST.
SMTP_USE_TLS = os.environ.get("SMTP_TLS", "1").strip().lower() not in ("0", "false", "no", "off")

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "").strip()

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "").strip()

# ntfy.sh — pub/sub push notifications with NO account, API key, or config.
# The notification rule's `target` IS the topic name; anyone subscribed to
# that topic (ntfy mobile/desktop app, or just the web page
# https://ntfy.sh/<topic>) gets the alert. Self-hosted ntfy works too — point
# NTFY_SERVER at it. This is the one channel that delivers for real out of the
# box, which is why it's the default the UI pre-fills.
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").strip().rstrip("/")


def _channel_configured(channel):
    if channel == "email":
        # A host is the only hard requirement — auth is optional (open relay)
        # and only matters if you're going through a provider that demands it.
        return bool(SMTP_HOST and SMTP_FROM)
    if channel == "sms":
        return bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER)
    if channel == "slack":
        return bool(SLACK_WEBHOOK_URL)
    if channel == "ntfy":
        return True  # no credentials needed; the topic lives in each rule's target
    return False


def _send_email(target, message):
    msg = MIMEText(message)
    msg["Subject"] = "OrderFlow AI alert"
    msg["From"] = SMTP_FROM
    msg["To"] = target
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
        if SMTP_USE_TLS:
            server.starttls()
        if SMTP_USER and SMTP_PASSWORD:
            server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_FROM, [target], msg.as_string())


def _send_sms(target, message):
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    resp = requests.post(
        url,
        data={"From": TWILIO_FROM_NUMBER, "To": target, "Body": message},
        auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
        timeout=10,
    )
    resp.raise_for_status()


def _send_slack(target, message):
    resp = requests.post(SLACK_WEBHOOK_URL, json={"text": message}, timeout=10)
    resp.raise_for_status()


def _send_ntfy(target, message):
    topic = (target or "").strip().strip("/")
    if not topic:
        raise ValueError("ntfy needs a topic name as the target")
    resp = requests.post(
        f"{NTFY_SERVER}/{topic}",
        data=message.encode("utf-8"),
        headers={"Title": "OrderFlow AI alert", "Tags": "package,factory"},
        timeout=10,
    )
    resp.raise_for_status()


_SENDERS = {"email": _send_email, "sms": _send_sms, "slack": _send_slack, "ntfy": _send_ntfy}


def _safe_print(msg):
    """print() that can't itself raise — a legacy-codepage console (Windows
    cp1252) can't encode the → / — / emoji chars in alert text, and an
    unguarded print here would turn a skipped notification into a 500."""
    try:
        print(msg)
    except Exception:
        try:
            print(msg.encode("ascii", "replace").decode("ascii"))
        except Exception:
            pass


def _log(channel, event_type, recipient, message, status):
    try:
        conn = get_db()
        conn.execute("""
            INSERT INTO notification_log (channel, event_type, recipient, message, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (channel, event_type, recipient, message, status, datetime.now().isoformat()))
        conn.commit()
        conn.close()
    except Exception as e:
        print("notification log write failed:", e)


def notify(event_type, message):
    """Fan a message out to every enabled notification_settings row for
    this event_type. Safe to call unconditionally from anywhere in the
    app — every failure mode (no settings configured, channel not
    configured, send error) is caught and logged, never raised."""
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM notification_settings WHERE event_type = ? AND enabled = 1", (event_type,)
        ).fetchall()
        conn.close()
    except Exception as e:
        print("notify() could not read settings:", e)
        return

    for row in rows:
        # Nothing in here — not even a print() failing on a legacy console
        # encoding — may escape: notify() is called mid-request from actions
        # that must not 500 just because an alert couldn't be logged.
        try:
            channel, target = row["channel"], row["target"]
            if not _channel_configured(channel):
                _log(channel, event_type, target, message, "skipped_not_configured")
                _safe_print(f"[notify] {channel} not configured — would have sent to {target}: {message}")
                continue
            try:
                _SENDERS[channel](target, message)
                _log(channel, event_type, target, message, "sent")
            except Exception as e:
                _log(channel, event_type, target, message, f"failed: {e}")
                _safe_print(f"[notify] {channel} send failed: {e}")
        except Exception as e:
            print("notify() row failed:", repr(e))


# ─────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────

@notifications_bp.route("/api/notifications/channel-status")
def channel_status():
    return jsonify({c: _channel_configured(c) for c in CHANNELS})


@notifications_bp.route("/api/notifications/settings", methods=["GET"])
def list_settings():
    conn = get_db()
    rows = conn.execute("SELECT * FROM notification_settings ORDER BY event_type, channel").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@notifications_bp.route("/api/notifications/settings", methods=["POST"])
def create_setting():
    data = request.get_json(silent=True) or {}
    channel = data.get("channel")
    target = (data.get("target") or "").strip()
    event_type = data.get("event_type")

    if channel not in CHANNELS:
        return jsonify({"message": f"❌ channel must be one of: {', '.join(CHANNELS)}"}), 400
    if event_type not in EVENT_TYPES:
        return jsonify({"message": f"❌ event_type must be one of: {', '.join(EVENT_TYPES)}"}), 400
    if not target:
        return jsonify({"message": "❌ target is required (email address, phone number, or Slack channel name)"}), 400

    conn = get_db()
    cur = conn.execute("""
        INSERT INTO notification_settings (channel, target, event_type, enabled, created_at)
        VALUES (?, ?, ?, 1, ?)
    """, (channel, target, event_type, datetime.now().isoformat()))
    conn.commit()
    setting_id = cur.lastrowid
    conn.close()
    return jsonify({"message": f"✅ Will notify {target} via {channel} on {event_type}", "id": setting_id})


@notifications_bp.route("/api/notifications/settings/<int:setting_id>", methods=["DELETE"])
def delete_setting(setting_id):
    conn = get_db()
    conn.execute("DELETE FROM notification_settings WHERE id = ?", (setting_id,))
    conn.commit()
    conn.close()
    return jsonify({"message": "✅ Notification setting removed"})


@notifications_bp.route("/api/notifications/log")
def get_log():
    conn = get_db()
    rows = conn.execute("SELECT * FROM notification_log ORDER BY id DESC LIMIT 200").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@notifications_bp.route("/api/notifications/test", methods=["POST"])
def test_notification():
    data = request.get_json(silent=True) or {}
    channel = data.get("channel")
    target = (data.get("target") or "").strip()
    if channel not in CHANNELS or not target:
        return jsonify({"message": "❌ channel and target are required"}), 400

    if not _channel_configured(channel):
        _log(channel, "test", target, "Test notification from OrderFlow AI", "skipped_not_configured")
        return jsonify({"message": f"⚠️ {channel} isn't configured (see README for the env vars) — logged as skipped, nothing was sent"})

    try:
        _SENDERS[channel](target, "Test notification from OrderFlow AI — if you're reading this, it works.")
        _log(channel, "test", target, "Test notification from OrderFlow AI", "sent")
        return jsonify({"message": f"✅ Test {channel} notification sent to {target}"})
    except Exception as e:
        _log(channel, "test", target, "Test notification from OrderFlow AI", f"failed: {e}")
        return jsonify({"message": f"❌ Send failed: {e}"}), 502


@notifications_bp.route("/api/notifications/check-late-suppliers", methods=["POST"])
def check_late_suppliers():
    """No background scheduler in this app, so 'checking for late
    suppliers' is an on-demand scan rather than a timer — call it from a
    button, or hit it from your own cron if you want it automatic. A PO
    counts as late once it's been 'sent' longer than the supplier's
    promised lead_days without being received."""
    conn = get_db()
    rows = conn.execute("""
        SELECT po.*, s.name AS supplier_name, s.lead_days
        FROM purchase_orders po
        JOIN suppliers s ON s.id = po.supplier_id
        WHERE po.status IN ('sent', 'partially_received')
    """).fetchall()
    conn.close()

    late = []
    for po in rows:
        try:
            sent_at = datetime.fromisoformat(po["updated_at"])
        except (TypeError, ValueError):
            continue
        days_out = (datetime.now() - sent_at).total_seconds() / 86400
        if days_out > (po["lead_days"] or 0):
            late.append(dict(po))
            notify("late_supplier",
                   f"PO #{po['id']} from {po['supplier_name']} is {days_out:.1f} days out "
                   f"(promised {po['lead_days']}d) and still '{po['status']}'.")

    return jsonify({"checked": len(rows), "late": late})
