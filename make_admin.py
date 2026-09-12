"""
Recovery helper — promote an account to admin, or reset the admin password.

You only need this if you've lost access to every admin account. Normally you
promote users from the Admin tab inside the app.

Usage (run from this folder, with the server stopped):

    python make_admin.py                      # list all accounts and their roles
    python make_admin.py someuser             # make 'someuser' an admin
    python make_admin.py admin --password X   # set admin's password to X

After changing a role, log out and log back in — your role is read into the
session at login, so an existing session keeps the old one.
"""

import argparse
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from database import get_db


def connect():
    try:
        return get_db()
    except Exception as e:
        sys.exit(f"Could not connect to the database: {e}\nRun this from the folder that contains app.py.")


def show(conn):
    rows = conn.execute("SELECT id, username, role, created_at FROM users ORDER BY id").fetchall()
    if not rows:
        print("No accounts yet — start the app once and it will create a default admin.")
        return
    print(f"{'id':<4} {'username':<28} {'role':<10} created")
    print("-" * 70)
    for r in rows:
        print(f"{r['id']:<4} {r['username']:<28} {r['role']:<10} {(r['created_at'] or '')[:10]}")


def main():
    ap = argparse.ArgumentParser(description="Promote an account to admin, or reset its password.")
    ap.add_argument("username", nargs="?", help="account to promote (omit to just list accounts)")
    ap.add_argument("--role", default="admin", choices=["admin", "operator", "viewer"],
                    help="role to set (default: admin)")
    ap.add_argument("--password", help="also set a new password for this account")
    args = ap.parse_args()

    conn = connect()

    if not args.username:
        show(conn)
        return

    user = conn.execute("SELECT id, username, role FROM users WHERE username = ?",
                        (args.username,)).fetchone()
    if not user:
        print(f"No account named '{args.username}'.\n")
        show(conn)
        sys.exit(1)

    conn.execute("UPDATE users SET role = ? WHERE id = ?", (args.role, user["id"]))
    print(f"'{user['username']}': {user['role']} -> {args.role}")

    if args.password:
        if len(args.password) < 6:
            sys.exit("Password must be at least 6 characters.")
        try:
            from werkzeug.security import generate_password_hash
        except ImportError:
            sys.exit("werkzeug is not installed — run: pip install -r requirements.txt")
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (generate_password_hash(args.password), user["id"]))
        print(f"'{user['username']}': password updated")

    conn.commit()
    print()
    show(conn)
    print("\nDone. Start the app, then log out and log back in for the change to take effect.")


if __name__ == "__main__":
    main()
