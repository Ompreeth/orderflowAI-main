"""
Authentication, roles, and the audit trail.

Design choices, spelled out because they're easy to second-guess later:

- Sessions are Flask's signed-cookie sessions (no extra table/service needed).
  Set SECRET_KEY in production so sessions survive a restart; a random one
  is generated otherwise (logged once at startup) so the app still runs.
- Three roles: admin (full control, incl. user management), operator
  (day-to-day actions: approve POs, take payments, manage inventory/orders),
  viewer (read-only).
- The pre-existing surface (chat, dashboard, inventory, RFID/barcode,
  demand & forecasting) intentionally stays USABLE WITHOUT LOGIN — this
  is additive, not a retrofit that locks people out of what already
  worked. Login is required only for the new higher-stakes actions this
  round of features introduces: approving/paying purchase orders,
  managing users, and reading the audit log.
- Every login-gated mutation writes an audit_log row.
"""

import os
import secrets
from datetime import datetime
from functools import wraps

from flask import Blueprint, request, jsonify, session

from database import get_db

try:
    from werkzeug.security import generate_password_hash, check_password_hash
except ImportError:  # pragma: no cover - werkzeug always ships with Flask
    generate_password_hash = check_password_hash = None

auth_bp = Blueprint("auth", __name__)

ROLES = ["admin", "operator", "viewer"]


# ─────────────────────────────────────────────────────────
# Bootstrap + helpers
# ─────────────────────────────────────────────────────────

def bootstrap_admin():
    """First run only: create a default admin so there's a way to log in.
    The generated password is printed once — there is no other way to
    retrieve it, so change it after the first login."""
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    if count == 0:
        password = secrets.token_urlsafe(9)
        conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
            ("admin", generate_password_hash(password), "admin", datetime.now().isoformat()),
        )
        conn.commit()
        print("=" * 64)
        print(" First run — created a default login:")
        print("   username: admin")
        print(f"   password: {password}")
        print(" This is shown only once. Log in and change it from the")
        print(" profile menu (or POST /api/auth/change-password).")
        print("=" * 64)
    conn.close()


def log_audit(action, entity_type=None, entity_id=None, details=None):
    """Record an action in the audit trail. Safe to call whether or not
    anyone is logged in — unauthenticated actions are attributed to
    'guest' so the trail stays complete."""
    try:
        conn = get_db()
        conn.execute(
            """INSERT INTO audit_log (username, action, entity_type, entity_id, details, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                session.get("username", "guest"),
                action,
                entity_type,
                entity_id,
                details,
                datetime.now().isoformat(),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        # Audit logging must never break the action it's logging.
        print("audit log write failed:", e)


def current_user():
    if "username" not in session:
        return None
    return {"username": session["username"], "role": session.get("role", "viewer")}


def stored_role(username=None):
    """The role recorded in the DATABASE for an account.

    This is the source of truth about what an account *is*, as opposed to
    session["role"], which is what it is currently *acting as* (see
    /api/auth/switch-role below). Every decision about switching is made
    against this value and never against the session, so that:
      - switching down to viewer doesn't strip the ability to switch back, and
      - a non-admin can never use the switcher to gain privileges.
    """
    name = username or session.get("username")
    if not name:
        return None
    conn = get_db()
    row = conn.execute("SELECT role FROM users WHERE username = ?", (name,)).fetchone()
    conn.close()
    return row["role"] if row else None


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "username" not in session:
            return jsonify({"message": "❌ Login required"}), 401
        return fn(*args, **kwargs)
    return wrapper


def role_required(*roles):
    """Usage: @role_required("admin") or @role_required("admin", "operator")"""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if "username" not in session:
                return jsonify({"message": "❌ Login required"}), 401
            if session.get("role") not in roles:
                return jsonify({"message": f"❌ Requires role: {' or '.join(roles)}"}), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator


# ─────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────

@auth_bp.route("/api/auth/register", methods=["POST"])
def register():
    """Public self-service sign-up — the User-module counterpart to the
    Admin panel's "Add User". Always creates the account as 'viewer'
    (read-only) no matter what the client sends for role: self-registration
    must never be a path to elevated access. An admin can promote the
    account afterward from the Admin panel's role dropdown."""
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if not username:
        return jsonify({"message": "❌ Username is required"}), 400
    if len(password) < 6:
        return jsonify({"message": "❌ Password must be at least 6 characters"}), 400

    conn = get_db()
    existing = conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
    if existing:
        conn.close()
        return jsonify({"message": f"❌ Username '{username}' is already taken"}), 400

    conn.execute(
        "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, 'viewer', ?)",
        (username, generate_password_hash(password), datetime.now().isoformat()),
    )
    conn.commit()
    new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()

    session["username"] = username
    session["role"] = "viewer"
    session.permanent = True
    log_audit("register", "user", new_id, "self-registered as viewer")
    return jsonify({"message": f"✅ Account '{username}' created", "username": username, "role": "viewer"})


@auth_bp.route("/api/auth/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()

    if not user or not check_password_hash(user["password_hash"], password):
        return jsonify({"message": "❌ Invalid username or password"}), 401

    session["username"] = user["username"]
    session["role"] = user["role"]
    session.permanent = True

    conn = get_db()
    conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (datetime.now().isoformat(), user["id"]))
    conn.commit()
    conn.close()

    log_audit("login", "user", user["id"])
    return jsonify({"username": user["username"], "role": user["role"]})


@auth_bp.route("/api/auth/logout", methods=["POST"])
def logout():
    if "username" in session:
        log_audit("logout", "user")
    session.clear()
    return jsonify({"message": "Logged out"})


@auth_bp.route("/api/auth/me")
def me():
    user = current_user()
    if not user:
        return jsonify({"authenticated": False})
    conn = get_db()
    row = conn.execute(
        "SELECT created_at, last_login FROM users WHERE username = ?", (user["username"],)
    ).fetchone()
    conn.close()
    extra = dict(row) if row else {}
    account = stored_role()
    return jsonify({
        "authenticated": True,
        **user,
        **extra,
        # What the account actually is, vs. what it's acting as right now.
        "account_role": account,
        "acting_as": bool(account and account != user["role"]),
        "can_switch_role": account == "admin",
    })


@auth_bp.route("/api/auth/switch-role", methods=["POST"])
@login_required
def switch_role():
    """Let an admin account act as a lower-privilege role without logging out.

    Useful for demonstrating and testing what operators and viewers can see.
    The gate is the role stored in the database (stored_role()), NOT the
    session's current role — so an admin who has switched down to viewer can
    still switch back, while a genuine viewer gets a 403 no matter what it
    sends. The switch is real, not cosmetic: session["role"] is what every
    @role_required check reads, so the API enforces the switched-to role too.
    """
    data = request.get_json(silent=True) or {}
    role = (data.get("role") or "").strip()

    if role not in ROLES:
        return jsonify({"message": f"❌ Role must be one of: {', '.join(ROLES)}"}), 400

    account = stored_role()
    if account != "admin":
        return jsonify({"message": "❌ Only an admin account can switch roles"}), 403

    session["role"] = role
    log_audit("switch_role", "user", None, f"acting as {role}")
    return jsonify({
        "message": f"✅ Now acting as {role}",
        "username": session["username"],
        "role": role,
        "account_role": account,
        "acting_as": role != account,
    })


@auth_bp.route("/api/auth/change-password", methods=["POST"])
@login_required
def change_password():
    data = request.get_json(silent=True) or {}
    old_password = data.get("old_password") or ""
    new_password = data.get("new_password") or ""

    if len(new_password) < 6:
        return jsonify({"message": "❌ New password must be at least 6 characters"}), 400

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE username = ?", (session["username"],)).fetchone()
    if not user or not check_password_hash(user["password_hash"], old_password):
        conn.close()
        return jsonify({"message": "❌ Current password is incorrect"}), 401

    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                 (generate_password_hash(new_password), user["id"]))
    conn.commit()
    conn.close()
    log_audit("change_password", "user", user["id"])
    return jsonify({"message": "✅ Password updated"})


@auth_bp.route("/api/users", methods=["GET"])
@role_required("admin")
def list_users():
    conn = get_db()
    rows = conn.execute(
        "SELECT id, username, role, created_at, last_login FROM users ORDER BY username"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@auth_bp.route("/api/users", methods=["POST"])
@role_required("admin")
def create_user():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    role = data.get("role") or "operator"

    if not username or len(password) < 6:
        return jsonify({"message": "❌ Username required; password must be at least 6 characters"}), 400
    if role not in ROLES:
        return jsonify({"message": f"❌ Role must be one of: {', '.join(ROLES)}"}), 400

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
            (username, generate_password_hash(password), role, datetime.now().isoformat()),
        )
        conn.commit()
        new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        log_audit("create_user", "user", new_id, f"role={role}")
        return jsonify({"message": f"✅ User '{username}' created", "id": new_id})
    except Exception as e:
        conn.close()
        return jsonify({"message": f"❌ {e}"}), 400


@auth_bp.route("/api/users/<int:uid>", methods=["PUT"])
@role_required("admin")
def update_user_role(uid):
    """Change an existing user's role. Self-service role changes are
    blocked the same way self-delete is below — otherwise an admin could
    lock themselves out with one click and have no other admin to fix it."""
    data = request.get_json(silent=True) or {}
    role = data.get("role")
    if role not in ROLES:
        return jsonify({"message": f"❌ Role must be one of: {', '.join(ROLES)}"}), 400

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if not user:
        conn.close()
        return jsonify({"message": "Not found"}), 404
    if user["username"] == session.get("username"):
        conn.close()
        return jsonify({"message": "❌ Cannot change your own role while logged in"}), 400

    conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, uid))
    conn.commit()
    conn.close()
    log_audit("update_user_role", "user", uid, f"role={role}")
    return jsonify({"message": f"✅ {user['username']}'s role updated to {role}"})


@auth_bp.route("/api/users/<int:uid>/reset-password", methods=["POST"])
@role_required("admin")
def admin_reset_password(uid):
    """Admin sets a new password for someone else's account directly —
    no old password needed, unlike the self-service change-password route.
    For when a user is locked out and can't do it themselves."""
    data = request.get_json(silent=True) or {}
    new_password = data.get("new_password") or ""
    if len(new_password) < 6:
        return jsonify({"message": "❌ New password must be at least 6 characters"}), 400

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if not user:
        conn.close()
        return jsonify({"message": "Not found"}), 404

    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                 (generate_password_hash(new_password), uid))
    conn.commit()
    conn.close()
    log_audit("admin_reset_password", "user", uid)
    return jsonify({"message": f"✅ Password reset for {user['username']}"})


@auth_bp.route("/api/users/<int:uid>", methods=["DELETE"])
@role_required("admin")
def delete_user(uid):
    conn = get_db()
    user = conn.execute("SELECT username FROM users WHERE id = ?", (uid,)).fetchone()
    if not user:
        conn.close()
        return jsonify({"message": "Not found"}), 404
    if user["username"] == session.get("username"):
        conn.close()
        return jsonify({"message": "❌ Cannot delete your own account while logged in"}), 400
    conn.execute("DELETE FROM users WHERE id = ?", (uid,))
    conn.commit()
    conn.close()
    log_audit("delete_user", "user", uid)
    return jsonify({"message": "✅ User deleted"})


@auth_bp.route("/api/audit-log")
@role_required("admin")
def get_audit_log():
    conn = get_db()
    rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 300").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])
