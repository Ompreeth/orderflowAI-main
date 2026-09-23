"""
Alerts & notifications: stockout risk, order status changes, and late
suppliers, dispatched to whichever channels are configured — email (SMTP),
SMS (Fast2SMS or Twilio), WhatsApp (CallMeBot or Twilio), Slack (incoming
webhook), ntfy. Every send attempt is logged to notification_log regardless
of outcome.

SMS and WhatsApp each have two possible providers, tried in this order:
  - SMS: Fast2SMS if FAST2SMS_API_KEY is set, else Twilio.
  - WhatsApp: CallMeBot if CALLMEBOT_APIKEY/CALLMEBOT_PHONE are set, else Twilio.
Fast2SMS and CallMeBot exist because Twilio trial accounts can't send
arbitrary custom text: SMS is locked to a fixed set of canned template
bodies, and the WhatsApp Sandbox can't use custom templates at all (a real
custom WhatsApp template needs a Meta-approved production WhatsApp Business
number — out of scope for a trial setup). Fast2SMS's "Quick SMS" route and
CallMeBot both send free-text messages immediately, no approval step.

The whole point is that this app runs with ZERO of these configured: an
unconfigured channel doesn't raise, doesn't block the action it's attached
to, and doesn't pretend to have sent anything — it logs
status='skipped_not_configured' with a clear message and moves on. Nothing
about placing an order, changing a status, or triggering a reorder depends
on any notification succeeding.

Configuration (all optional, all via env vars):
    SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASSWORD, SMTP_FROM
    FAST2SMS_API_KEY  (preferred SMS provider)
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, TWILIO_WHATSAPP_FROM_NUMBER
    CALLMEBOT_APIKEY, CALLMEBOT_PHONE  (preferred WhatsApp provider)
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
CHANNELS = ["email", "sms", "whatsapp", "slack", "ntfy"]

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
# WhatsApp rides the same Twilio account/auth as SMS but needs its own
# WhatsApp-enabled sender — Twilio's sandbox number (whatsapp:+14155238886)
# for testing, or an approved WhatsApp Business sender in production.
TWILIO_WHATSAPP_FROM_NUMBER = os.environ.get("TWILIO_WHATSAPP_FROM_NUMBER", "").strip()

# Fast2SMS — preferred SMS path when set: unlike a Twilio trial account
# (which only accepts a fixed set of canned template bodies), Fast2SMS's
# "q" (Quick SMS) route sends arbitrary custom text immediately, no DLT
# template registration needed. Falls back to Twilio if unset.
FAST2SMS_API_KEY = os.environ.get("FAST2SMS_API_KEY", "").strip()

# Green API — first-choice WhatsApp path when set: links your own WhatsApp
# account (QR-code pairing, like WhatsApp Web) and sends arbitrary text
# immediately, no template/approval step. Free "Developer" tier is capped
# at 3 distinct chats but unlimited messages to those — fine for a single
# admin recipient. GREEN_API_URL is the per-instance host the console shows
# (e.g. https://7107.api.greenapi.com) — it's sharded per instance, not a
# fixed domain, so it has to be captured from your own console.
GREEN_API_URL = os.environ.get("GREEN_API_URL", "").strip().rstrip("/")
GREEN_API_ID_INSTANCE = os.environ.get("GREEN_API_ID_INSTANCE", "").strip()
GREEN_API_TOKEN_INSTANCE = os.environ.get("GREEN_API_TOKEN_INSTANCE", "").strip()

# CallMeBot — second-choice WhatsApp path: a free hobbyist API for personal
# use, link your own number once (see README), then send arbitrary text
# with no approval step. Falls back to Twilio if neither this nor Green API
# is configured (Twilio's WhatsApp Sandbox can only use 3 fixed templates,
# and real custom templates need a registered, Meta-approved WhatsApp
# Business number).
CALLMEBOT_APIKEY = os.environ.get("CALLMEBOT_APIKEY", "").strip()
CALLMEBOT_PHONE = os.environ.get("CALLMEBOT_PHONE", "").strip()

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
        return bool(FAST2SMS_API_KEY) or bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER)
    if channel == "whatsapp":
        return (
            bool(GREEN_API_URL and GREEN_API_ID_INSTANCE and GREEN_API_TOKEN_INSTANCE)
            or bool(CALLMEBOT_APIKEY and CALLMEBOT_PHONE)
            or bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM_NUMBER)
        )
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


def _send_sms_fast2sms(target, message):
    # "q" = Quick SMS route: sends arbitrary custom text immediately, no
    # DLT template registration needed (unlike Fast2SMS's other routes,
    # meant for registered promotional/transactional senders).
    number = (target or "").strip().lstrip("+")
    if number.startswith("91") and len(number) > 10:
        number = number[2:]  # Fast2SMS wants the bare 10-digit Indian number
    resp = requests.post(
        "https://www.fast2sms.com/dev/bulkV2",
        headers={"authorization": FAST2SMS_API_KEY},
        data={"route": "q", "message": message, "language": "english", "flash": 0, "numbers": number},
        timeout=10,
    )
    resp.raise_for_status()
    body = resp.json()
    if not body.get("return"):
        raise RuntimeError(f"Fast2SMS rejected the message: {body}")


def _send_sms_twilio(target, message):
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    resp = requests.post(
        url,
        data={"From": TWILIO_FROM_NUMBER, "To": target, "Body": message},
        auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
        timeout=10,
    )
    resp.raise_for_status()


def _send_sms(target, message):
    if FAST2SMS_API_KEY:
        _send_sms_fast2sms(target, message)
    else:
        _send_sms_twilio(target, message)


def _send_whatsapp_greenapi(target, message):
    number = (target or "").strip().lstrip("+")
    if not number:
        raise ValueError("whatsapp needs a phone number as the target")
    url = f"{GREEN_API_URL}/waInstance{GREEN_API_ID_INSTANCE}/sendMessage/{GREEN_API_TOKEN_INSTANCE}"
    resp = requests.post(
        url,
        json={"chatId": f"{number}@c.us", "message": message},
        timeout=10,
    )
    resp.raise_for_status()
    body = resp.json()
    if not body.get("idMessage"):
        raise RuntimeError(f"Green API rejected the message: {body}")


def _send_whatsapp_callmebot(target, message):
    # CallMeBot always delivers to whichever number activated CALLMEBOT_APIKEY
    # (see README) — target isn't used to route the message, but is still
    # required so a misconfigured rule doesn't silently no-op.
    if not (target or "").strip():
        raise ValueError("whatsapp needs a phone number as the target")
    resp = requests.get(
        "https://api.callmebot.com/whatsapp.php",
        params={"phone": CALLMEBOT_PHONE, "text": message, "apikey": CALLMEBOT_APIKEY},
        timeout=10,
    )
    resp.raise_for_status()
    if "message queued" not in resp.text.lower() and "message sent" not in resp.text.lower():
        raise RuntimeError(f"CallMeBot rejected the message: {resp.text[:200]}")


def _send_whatsapp_twilio(target, message):
    # Twilio's WhatsApp API is the same Messages endpoint as SMS — the
    # only difference is both From and To carry a "whatsapp:" prefix.
    number = (target or "").strip()
    if not number:
        raise ValueError("whatsapp needs a phone number (with country code) as the target")
    number = number[len("whatsapp:"):] if number.lower().startswith("whatsapp:") else number

    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    resp = requests.post(
        url,
        data={
            "From": f"whatsapp:{TWILIO_WHATSAPP_FROM_NUMBER}",
            "To": f"whatsapp:{number}",
            "Body": message,
        },
        auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
        timeout=10,
    )
    resp.raise_for_status()


def _send_whatsapp(target, message):
    if GREEN_API_URL and GREEN_API_ID_INSTANCE and GREEN_API_TOKEN_INSTANCE:
        _send_whatsapp_greenapi(target, message)
    elif CALLMEBOT_APIKEY and CALLMEBOT_PHONE:
        _send_whatsapp_callmebot(target, message)
    else:
        _send_whatsapp_twilio(target, message)


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


_SENDERS = {"email": _send_email, "sms": _send_sms, "whatsapp": _send_whatsapp, "slack": _send_slack, "ntfy": _send_ntfy}


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
