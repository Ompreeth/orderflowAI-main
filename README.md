# OrderFlow AI — Setup Guide

A chat-driven manufacturing order & inventory manager: place orders in plain
English, track them on a dashboard, scan RFID/barcode tags to consume stock,
forecast demand, automate purchasing, take payments, run quality checks,
track production against a bill of materials, manage multi-location
inventory, send alerts to email/SMS/Slack, integrate with external systems
via webhooks, and export reports.

## Prerequisites

- Python 3.10+
- **Ollama** running somewhere reachable, with a model pulled (default: `qwen2.5:7b`)
  → [ollama.com](https://ollama.com) · `ollama pull qwen2.5:7b`

> This app talks to a local/self-hosted **Ollama** server for language
> detection, translation, and intent parsing — not the Anthropic API. (An
> earlier version of this README, and `requirements.txt`, referenced Claude/an
> Anthropic API key; that was left over from an earlier direction and didn't
> match the code. It's been corrected below.)

---

## 1. Install dependencies

```bash
cd orderflow
pip install -r requirements.txt
```

---

## 2. Point the app at your Ollama server

The Ollama address is **configurable via environment variables** — it no
longer defaults to a hardcoded machine on the original developer's network.
If you don't set anything, it falls back to that original address, which
almost certainly isn't reachable from wherever you're running this now, so
set at least `OLLAMA_BASE_URL`:

**Mac / Linux:**
```bash
export OLLAMA_BASE_URL="http://localhost:11434"   # or your Ollama server's address
export OLLAMA_MODEL="qwen2.5:7b"                  # optional, this is the default
```

**Windows (PowerShell):**
```powershell
$env:OLLAMA_BASE_URL = "http://localhost:11434"
$env:OLLAMA_MODEL    = "qwen2.5:7b"
```

**Windows (CMD):**
```cmd
set OLLAMA_BASE_URL=http://localhost:11434
set OLLAMA_MODEL=qwen2.5:7b
```

The sidebar's "AI connected / AI offline" indicator pings this address live
(`GET /api/system/status`) so you can tell immediately if the model server
isn't reachable, instead of guessing from a chat message that silently fails.

---

## 3. Run the app

```bash
python app.py
```

Open your browser at: **http://localhost:5000**

(Port is also configurable: `PORT=8080 python app.py`.)

**First run** creates a default `admin` login and prints its one-time
password to the console — copy it from there, since it's shown nowhere else.
Log in, then change it from your **Profile** page (bottom of the sidebar
once logged in — or `POST /api/auth/change-password` directly).

---

## Other environment variables

Everything below has a working default — set these only to change the
behavior, not to get the app running.

| Variable | Default | What it does |
|---|---|---|
| `SECRET_KEY` | random each restart | Signs login session cookies (and chat conversation memory, which rides the same session). Set this in any real deployment — otherwise everyone is logged out and chat history resets on every restart. |
| `PO_APPROVAL_THRESHOLD` | `500` | Purchase orders at or under this dollar total auto-approve; above it, they need an explicit approval from an admin/operator. |
| `STRIPE_SECRET_KEY` | *(unset)* | Enables real Stripe payments in test or live mode. Unset, the app runs in a clearly-labeled **demo mode**: payments "succeed" instantly with no external call, so you can use the full purchasing/payment workflow without a Stripe account. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | *(unset)* | Enables the **email** notification channel. `SMTP_PORT` defaults to `587`; `SMTP_FROM` defaults to `SMTP_USER`. All of `SMTP_HOST`/`SMTP_USER`/`SMTP_PASSWORD` must be set for email to count as configured. |
| `FAST2SMS_API_KEY` | *(unset)* | Enables the **SMS** channel via [Fast2SMS](https://www.fast2sms.com)'s "Quick SMS" route — sends arbitrary custom text immediately, no DLT template registration. Preferred over Twilio; falls back to Twilio if unset. Note: Fast2SMS requires one ₹100+ account top-up before their API accepts any request, even with free signup credit. |
| `GREEN_API_URL`, `GREEN_API_ID_INSTANCE`, `GREEN_API_TOKEN_INSTANCE` | *(unset)* | Enables the **WhatsApp** channel via [Green API](https://green-api.com) — links your own WhatsApp account (QR-code pairing, like WhatsApp Web) from its console and sends custom text immediately. Free "Developer" tier: unlimited messages, capped at 3 distinct chats. First choice among the WhatsApp providers. |
| `CALLMEBOT_APIKEY`, `CALLMEBOT_PHONE` | *(unset)* | Enables the **WhatsApp** channel via [CallMeBot](https://www.callmebot.com) — a free hobbyist API for personal use. Add `+34 623 91 22 04` as a WhatsApp contact, message it `I allow callmebot to send me messages`, and use the APIKEY it replies with. Second choice if Green API isn't set. |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` | *(unset)* | Enables the **SMS** notification channel via Twilio, used only if `FAST2SMS_API_KEY` isn't set. A Twilio *trial* account can only send a fixed set of canned template bodies, not custom text — upgrade the account to lift that. |
| `TWILIO_WHATSAPP_FROM_NUMBER` | *(unset)* | Enables the **WhatsApp** notification channel via Twilio (reuses `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN` above), used only if neither Green API nor CallMeBot is set. Twilio's WhatsApp *Sandbox* can't send custom text at all — only 3 fixed templates; a real custom template needs a Meta-approved production WhatsApp Business number. Target numbers in notification rules are plain phone numbers (e.g. `+15551234567`) — the `whatsapp:` prefix is added automatically. |
| `SLACK_WEBHOOK_URL` | *(unset)* | Enables the **Slack** notification channel via an incoming webhook URL. |
| `NTFY_SERVER` | `https://ntfy.sh` | Server for the **ntfy** push channel. This channel needs **no account, key, or env var at all** — it works out of the box against the public ntfy.sh. Point this at a self-hosted [ntfy](https://ntfy.sh) instance if you'd rather not use the public one. |
| `FAST_SELL_WINDOW_DAYS` | `7` | Trailing window (days) used to judge whether a part is "selling fast" for the low-stock alert below. |
| `FAST_SELL_MIN_UNITS` | `5` | Minimum units sold within that window to count as "selling fast" — below this, a low-stock part doesn't get flagged as hot. |
| `FAST_SELL_STOCK_PCT` | `50` | A fast-selling part fires a `stockout_risk` notification once stock drops to this percentage (or below) of what it had at the start of the window. |
| `BACKUP_DIR` | `backups` | Folder scheduled/manual database backups are written to. |
| `BACKUP_INTERVAL_HOURS` | `24` | How often the background thread backs up `orders.db`. |
| `BACKUP_RETENTION_COUNT` | `14` | How many backup files to keep before pruning the oldest. |

None of the notification or webhook variables are required to run the app —
every channel is optional, and an unconfigured one just gets logged as
"skipped" rather than blocking anything (see **Notifications** below).

---

## What you can say in the chat

| Action | Example |
|---|---|
| Place an order | `I need 200 titanium flanges, 80mm bore, by July 20` |
| Update status | `Mark order #42 as accepted` |
| Auto-advance status | `Order #42 has been reviewed` |
| Log quality | `Log quality for order #42: visual inspection passed, no defects` |
| Check status | `What is the status of order #42?` |
| List all orders | `Show me all orders` |
| Ask a reporting question | `What's my total inventory value?` · `What's my average fulfillment time?` · `Which supplier is best for Steel Rod?` · `What's low on stock?` |

Orders can only be placed against items that already exist in your
**Inventory** catalog — add items there first. Report-style questions are
answered directly from the same numbers behind the Reports tab — instantly,
and without needing Ollama to be reachable, since they're matched by keyword
before falling through to the AI model. The assistant also remembers the
last few exchanges in a conversation (so "and the one before that?" works),
until you click **New Conversation** to start fresh; a microphone button
next to the input uses your browser's built-in speech recognition, so
nothing is sent to a third-party voice service.

---

## Beyond chat: the other views

- **Purchasing** — purchase orders to suppliers (distinct from customer
  orders): compare suppliers on a weighted cost/lead-time/reliability score,
  create POs by hand or auto-generate them from a reorder recommendation,
  approve → send → receive against real stock, and pay suppliers. A
  Payment History table at the bottom shows every charge collected or
  paid out, incoming and outgoing. Restocking a low-stock item
  automatically creates a real PO here instead of a fake order.
- **Profile** (sidebar, once logged in) — your account details (username,
  role, member since, last login) and a self-service change-password form.
- **Admin** (sidebar, admin role only) — create accounts, change anyone's
  role, reset a forgotten password without needing the old one, delete
  accounts, and read the full audit trail (last 300 actions across the app).
- **Sign up** — anyone can create their own account from the "New here?"
  link on the login screen, without an admin doing it for them. Self-created
  accounts always start as `viewer` (read-only) no matter what — an admin
  has to explicitly promote one from the Admin panel before it can approve
  POs, take payments, or do anything else higher-stakes.
- **Production** — a bill of materials linking a finished inventory item to
  the components it consumes; work orders that plan → start → complete a
  production run (completing one deducts every BOM component, all-or-
  nothing, and adds the finished quantity); and machines/work-centers with
  downtime logging. A finished item with no BOM on file can still run a work
  order — nothing to deduct, same "optional, not a precondition" spirit as
  procurement without a supplier on file.
- **Quality** — structured pass/fail/conditional checks with defect category
  and corrective action, on top of the same log the chat command "Log
  quality for order #42: ..." writes to. Pull a printable certificate of
  conformance for any order.
- **Reports** — inventory valuation with ABC analysis, a supplier scorecard
  that blends the on-file reliability rating with real on-time-delivery
  history, order fulfillment time, and CSV/PDF export.
- **Notifications** — configurable rules routing stockout risk, order status
  changes, late suppliers, and quality fails to **ntfy** (push — works with no
  setup), email, SMS, or Slack. For ntfy the rule's *target* is just a topic
  name: pick one, subscribe to it in the ntfy mobile/desktop app or at
  `https://ntfy.sh/<topic>`, and alerts start arriving — no account or key.
  Every send attempt is logged (sent / skipped-not-configured / failed) so you
  can see exactly what went out (or would have) even with zero channels set
  up. "Check Late Suppliers" runs an on-demand scan (there's no background
  scheduler for that one) comparing each sent purchase order's age against its
  supplier's promised lead time.
- **Integrations** — register outbound webhooks (payloads signed with
  HMAC-SHA256 so ERP/accounting systems can verify they're genuine), print
  Code128 barcode label sheets for any inventory item (auto-assigning a
  barcode at print time if one isn't set yet), and trigger or download
  scheduled SQLite backups.
- **Inventory → Warehouses & Locations** (collapsible section at the bottom
  of the Inventory view) — optionally track *where* stock physically sits
  across more than one warehouse. Assigning stock to a location earmarks
  part of what an item already has; it can never exceed the item's total, and
  an item with no location assignments behaves exactly as before (one shared
  pool). Transfers move stock between locations without changing the total.

Every table view with filter buttons (Orders, Purchase Orders) also supports
**saved views** — name your current filter combination once, then re-apply
it with a click via the chips shown above the table. The sidebar's search
box searches orders, inventory, and suppliers at once from anywhere in the
app. Inventory supports bulk **CSV import/export** (import upserts by part
name, one bad row doesn't fail the whole file). A light/dark theme toggle
lives at the bottom of the sidebar and remembers your choice. The app is
also an installable **PWA** — look for your browser's "Install app" option.

Approving/canceling purchase orders and taking payments require logging
in as admin or operator; creating/editing users, resetting passwords, and
reading the audit log require the admin role specifically. Everything
else — chat, dashboard, inventory, production, notifications, integrations,
RFID, quality logging, reports, and editing or cancelling a customer order
from the dashboard — works without an account, same as before this round
of features.

---

## Project Structure

```
orderflow/
├── app.py                  ← Flask backend + Ollama-based intent parsing
├── database.py              ← SQLite schema + connection helper
├── auth.py                  ← Login, sessions, roles, audit trail
├── procurement.py           ← Purchase orders, supplier comparison, approvals
├── payments.py               ← Stripe (or demo-mode) payments
├── quality.py                ← Structured quality checks + certificates
├── reports.py                ← Valuation/ABC, scorecards, fulfillment, CSV/PDF
├── production.py             ← BOM, work orders, machines & downtime
├── notifications.py          ← Email/SMS/Slack alert routing + log
├── warehouses.py             ← Multi-location inventory & transfers
├── integrations.py           ← Barcode labels, webhooks, scheduled backups
├── search.py                 ← Global search across orders/inventory/suppliers
├── saved_views.py            ← Named, reusable table filters
├── inventory_io.py           ← CSV bulk import/export for inventory
├── requirements.txt
├── README.md
├── templates/
│   └── index.html           ← App shell (sidebar + views)
└── static/
    ├── css/style.css        ← Design system — dark (default) + light theme
    ├── js/app.js            ← Frontend logic
    ├── manifest.json        ← PWA manifest
    ├── service-worker.js    ← PWA service worker (app-shell caching only)
    ├── icons/               ← PWA app icons
    └── vendor/              ← Self-hosted fonts, icon font, and QR/barcode
                                camera library (see below)
```

## No external CDN dependency

Fonts (Inter, IBM Plex Mono), the icon set (Tabler Icons), and the camera-based
barcode/QR library (`html5-qrcode`) are all vendored under `static/vendor/` and
served by Flask itself, rather than loaded from Google Fonts / jsdelivr / unpkg
at runtime. The app has **no external network dependency** once it's running —
it works on an offline or firewalled factory-floor network, and nothing breaks
if a CDN has an outage. Only Ollama needs to be reachable (see step 2 above).

## Dashboard: Demand & Forecasting

The dashboard's **Demand & Forecasting** section is one tabbed panel (it used
to be two separately-bolted-on accordions with overlapping purposes):

- **Forecast** — quick per-item glance: average daily usage, a 14-day
  sparkline, trend, and days-until-stockout.
- **Predictions** — the deep-dive version: linear-regression trend fitting,
  confidence score, multi-horizon demand ranges, a stockout date with a
  confidence band, and a reorder recommendation (safety stock, reorder point,
  suggested quantity) factoring in your suppliers' lead times. Loads on first
  visit since it recomputes a regression for every item.
- **Gap Analysis** — stock vs. projected demand over your chosen horizon.
- **Production Plan** / **Suppliers** — simple CRUD lists your team can use to
  track planned production runs and supplier lead times / reliability, which
  feed into the Predictions tab's reorder math.

## Architecture

- **Intent classification**: each chat message is sent to your Ollama model
  along with the last few turns of conversation (kept in the signed session
  cookie, capped at 12 entries / 6 exchanges) so follow-up questions like
  "and the one before that?" resolve correctly. Click **New Conversation**
  to clear that memory and start fresh.
- **Structured extraction**: the model returns JSON with parsed fields
  (part, qty, deadline, etc.).
- **Deterministic fast paths**: natural-language reporting questions ("what's
  my inventory value?") are matched by keyword and answered directly from
  the same SQL views behind the Reports tab, *before* the message ever
  reaches the LLM classifier — so they're instant and still work if Ollama
  is unreachable.
- **SQLite**: orders, inventory, quality logs, scan events, production plans,
  suppliers, BOM/work orders/machines, warehouses/locations, notification
  settings/log, webhooks/log, and saved views all persist in `orders.db`
  (created automatically on first run).
- **Disambiguation without the LLM**: when your message matches more than one
  inventory item, or matches none, the UI shows a clickable picker that places
  the order directly via `/api/chat/confirm-order` — no second model call
  needed, so it still works even if Ollama is briefly unreachable.
- **Business events, not just data**: placing an order, changing its status,
  and hitting a reorder point all fan out to `notify()` (email/SMS/Slack) and
  `dispatch_webhook()` (outbound HTTP) — both fire-and-log, so a slow or
  unconfigured downstream channel never blocks the action that triggered it.
