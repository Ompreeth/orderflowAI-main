"""
Desktop entry point — wraps the existing Flask app (app.py, unchanged) in a
native window via pywebview, so the packaged .exe is a real double-click
application: no terminal, no browser tab, no visible Python at all.

Run directly for development:   python desktop.py
Build a standalone .exe with:
    pyinstaller --onefile --windowed --icon static/icons/icon.ico ^
      --add-data "templates;templates" --add-data "static;static" ^
      --name OrderFlowAI desktop.py

The built .exe reads .env from its own folder (not bundled inside it), so
DATABASE_URL / SMTP_* / TWILIO_* / OLLAMA_* stay editable without a rebuild —
copy your .env next to dist/OrderFlowAI.exe.
"""

import os
import sys
import threading
import time

import requests

# Resolve where .env lives: next to the .exe when frozen, next to this
# script otherwise. Must happen before importing app.py, which reads
# DATABASE_URL (via database.py) at import time.
if getattr(sys, "frozen", False):
    _app_dir = os.path.dirname(sys.executable)
else:
    _app_dir = os.path.dirname(os.path.abspath(__file__))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_app_dir, ".env"))
except ImportError:
    pass

HOST = "127.0.0.1"
PORT = 5000
BASE_URL = f"http://{HOST}:{PORT}"


def _run_server():
    from app import app, init_db, bootstrap_admin
    init_db()
    bootstrap_admin()
    # debug/reloader off: the reloader's watcher+worker double-process model
    # (see app.py's start_backup_scheduler guard) isn't compatible with
    # running embedded in a GUI wrapper on a background thread.
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)


def _wait_for_server(timeout=20):
    """/api/system/status itself can take up to ~3s (it pings the configured
    Ollama server with its own 3s timeout) — the per-request timeout here
    has to comfortably clear that, or every probe reads-out before the
    endpoint even finishes, regardless of the server being up."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if requests.get(f"{BASE_URL}/api/system/status", timeout=5).status_code == 200:
                return True
        except requests.exceptions.RequestException:
            pass
        time.sleep(0.3)
    return False


def main():
    server_thread = threading.Thread(target=_run_server, daemon=True)
    server_thread.start()

    if not _wait_for_server():
        print("OrderFlow AI server didn't start in time — check DATABASE_URL in .env.")
        sys.exit(1)

    import webview
    webview.create_window("OrderFlow AI", BASE_URL, width=1400, height=900, min_size=(900, 600))
    webview.start()


if __name__ == "__main__":
    main()
