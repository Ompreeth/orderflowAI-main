# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

**OrderFlow AI** — a chat-driven manufacturing order & inventory manager (VTU MCA
major project). Users place orders in plain English; a local **Ollama** model
parses the intent; everything else is ordinary Flask + Postgres CRUD.

Flask 3 monolith, server-rendered single page (`templates/index.html`), one
vanilla-JS bundle (`static/js/app.js`, ~4.4k lines), PWA-installable, plus an
optional pywebview desktop wrapper. No build step, no framework, no tests.

## Running it

```bash
pip install -r requirements.txt
python app.py              # http://localhost:5000
```

Environment (put it in `.env`; `app.py` loads it before any local import):

| Var | Notes |
|---|---|
| `DATABASE_URL` | **Required.** `database.py` raises at import without it. Use a *pooled* Postgres URL — the app opens one connection per request. |
| `SECRET_KEY` | Unset → random per run → **every login dies on restart**. Set it. |
| `OLLAMA_BASE_URL` | Default is a stale Tailscale IP (`http://100.71.169.79:11434`). Set `http://localhost:11434` locally. |
| `OLLAMA_MODEL` | Default `qwen2.5:7b`. |
| `PORT` | Default `5000`. |
| `FLASK_DEBUG` | Default on; `0` disables the reloader. |
| `CHAT_DEBUG=1` | Dumps raw Ollama prompts/responses to stdout. |
| `SMTP_*` / `TWILIO_*` / `SLACK_WEBHOOK_URL` / `STRIPE_SECRET_KEY` | Optional. Without them alerts are logged, not sent, and payments stay in mock mode. |

`GET /api/system/status` reports Ollama reachability (it pings `/api/tags`).
If Ollama looks offline on Windows, try `127.0.0.1` instead of `localhost` —
Ollama binds IPv4 while `localhost` can resolve to `::1`.

Helper scripts (all one-off, run from this folder):

- `seed_inventory.py` — starter parts catalogue, idempotent.
- `migrate_to_postgres.py` — one-time copy of the legacy `orders.db` into Postgres.
- `make_admin.py` — recovery only: list accounts, promote to admin, reset a password.
  Normally roles are managed in the app's Admin tab.

## Architecture

`app.py` (~1900 lines) holds the AI layer, core inventory/order/RFID/forecast
routes, and registers 12 blueprints:

```
auth  procurement  payments  quality  reports  production
notifications  warehouses  integrations  search  saved_views  inventory_io
```

Adding a blueprint means editing **two** places in `app.py` — the import block
(~line 38) and the `register_blueprint` block (~line 67).

Shared helpers: `get_db`/`init_db` (database.py), `log_audit` (auth.py),
`notify` (notifications.py), `_best_supplier_for`/`_create_po_row`
(procurement.py), report row builders (reports.py).

### database.py is a SQLite-shaped shim over psycopg2

This is the single most important thing to know before writing SQL here.
The app was written against `sqlite3` and migrated to Postgres by wrapping the
connection rather than rewriting ~260 call sites. `_Connection.execute()`:

- rewrites `?` placeholders to `%s`
- rewrites `SELECT last_insert_rowid()` to `SELECT lastval()`
- appends `RETURNING id` to every INSERT so `cur.lastrowid` works
- returns `DictCursor` rows, so `row["column"]` is the access pattern

**So: write new queries in sqlite style — `conn.execute(sql, params)` with `?`
placeholders.** Using `%s` or a raw psycopg2 cursor breaks the convention and
loses `lastrowid`. Every table has an `id` primary key; every INSERT inserts a
single row — the shim relies on both.

`init_db()` creates all 21 tables and only runs from `python app.py`, so a
fresh database needs one normal app start before anything else works.

`orders.db` in this folder is a **leftover** from the pre-Postgres era. Nothing
reads it. It is gitignored, as are `.env`, `__pycache__/`, `build/`, `dist/`.
Never commit any of them — `dist/` carries a copy of the real `.env`.

### The AI layer (app.py)

Prompt-based, **no training and no dataset**. Say so plainly in any write-up;
do not describe this as a trained or fine-tuned model.

- `detect_language` → `translate_to_english` → `classify_intent`, each one
  Ollama call with `stream: false, think: false`.
- `extract_json` salvages JSON from prose-wrapped model output.
- `_correct_part_name` fuzzy-matches part names against the live catalogue by
  token overlap, which is what makes typos and partial names work.
- `answer_report_query` is a deterministic keyword fallback used when Ollama is
  unreachable — graceful degradation, worth preserving in any refactor.
- Nine intents: `create_order`, `update_status`, `query_order`, `list_orders`,
  `log_quality`, `list_inventory`, `query_inventory`, `consume_stock`,
  `report_query`.

`POST /api/chat` returns **only** `{"message": ...}` — the parsed intent is not
exposed. To measure parser accuracy, import `classify_intent` directly;
`init_db()`, `bootstrap_admin()` and `start_backup_scheduler()` all live inside
`if __name__ == "__main__":`, so **`import app` is side-effect-free**.

### Auth and roles

Session cookies, Werkzeug salted hashes, three roles: `admin`, `operator`,
`viewer`. Guards are `@login_required` and `@role_required(...)`.

Three behaviours that look like bugs and are not:

1. The role is read into the session **at login**, so changing someone's role
   takes effect only after they log in again.
2. Self-registration always creates a `viewer`, whatever the request says.
3. `POST /api/auth/switch-role` (admin acting as operator/viewer without
   logging out) gates on `stored_role()` — the role **in the database** — never
   on `session["role"]`. Keep it that way: switching down to viewer must not
   strip the ability to switch back, and an operator must never be able to
   switch up. `/api/auth/me` exposes `account_role`, `acting_as`,
   `can_switch_role` for the UI.

### CSS: token layers, and style.css is off-limits

`static/css/style.css` (~77KB) is fully token-driven — ~680 `var(--…)` uses,
radii via `--radius-*` (a dozen literals survive). Visual changes go in an
**override layer** loaded after it, never in `style.css` itself:

```
style.css           base + tokens        ← do not edit for restyling
theme-violet.css    dark  (violet)
theme-zentra.css    light (amber)
theme-haulix.css    dark  (lime/ops)     + .gauge / .pipe components
theme-coral.css     light (coral)        + .hero-row / .hero-mic
dashboard-bento.css 12-col bento grid
shell-slide.css     sidebar-as-drawer + top bar
animations.css      motion only — no color, no layout
```

Link order in `index.html` is the cascade; later wins. Each layer is removable
by deleting its one `<link>`. The Dark/Light toggle sets `data-theme="light"`
on `<html>` and persists to `localStorage`; **light is the default**.

Semantic tokens matter: use `var(--good)` / `var(--warn)` for status, not
`var(--brand)` — brand is lime in Haulix and red in Coral, so a "healthy"
indicator painted with it reads as an alarm in one theme.

## Desktop build

`desktop.py` wraps the unchanged Flask app in pywebview. `OrderFlowAI.spec` +
`requirements-desktop.txt` drive PyInstaller. The built `.exe` reads `.env`
from its own folder, so secrets stay editable without a rebuild.

## Gotchas

- The debug reloader executes `__main__` twice; the backup scheduler is guarded
  by `WERKZEUG_RUN_MAIN`. Anything thread- or timer-like needs the same guard.
- Never `pkill -f "python app.py"` — the pattern matches the killing shell's
  own argv and takes the shell down with it.
- Postgres enforces foreign keys that SQLite did not. New inserts referencing
  `inventory`, `orders` or `suppliers` need the FK violation handled; a recent
  commit fixed three crashes of exactly this kind.
- No test suite. Verify changes by running the app and exercising the route.
