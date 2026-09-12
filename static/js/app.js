// ═══════════════════════════════════════════════════════
// STATE
// ═══════════════════════════════════════════════════════

let allOrders    = [];
let allInventory = [];

// ═══════════════════════════════════════════════════════
// AI CONNECTION STATUS  (sidebar footer)
// Previously a hardcoded "AI connected" label regardless of whether the
// Ollama backend was actually reachable — now polls the real health check.
// ═══════════════════════════════════════════════════════

async function pollSystemStatus() {
    const dot   = document.getElementById("aiStatusDot");
    const text  = document.getElementById("aiStatusText");
    const model = document.getElementById("aiStatusModel");
    if (!dot || !text) return;

    try {
        const res  = await fetch("/api/system/status");
        const data = await res.json();
        if (data.ollama_connected) {
            dot.className = "dot green pulse";
            text.textContent = "AI connected";
        } else {
            dot.className = "dot red";
            text.textContent = "AI offline";
        }
        if (model) {
            model.textContent = data.model
                ? `model: ${data.model}`
                : " ";
            model.title = data.url || "";
        }
    } catch (err) {
        dot.className = "dot red";
        text.textContent = "AI offline";
    }
}

// ═══════════════════════════════════════════════════════
// CHAT
// ═══════════════════════════════════════════════════════

async function sendMessage() {
    const input   = document.getElementById("chatInput");
    const message = input.value.trim();
    if (!message) return;

    addMessage(escapeHtml(message), "user");
    input.value = "";
    input.style.height = "auto";

    const typingId = addTyping();

    try {
        const response = await fetch("/api/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ message }),
        });
        const data = await response.json();
        removeTyping(typingId);

        // Handle HTTP errors (e.g. 400 insufficient stock)
        if (!response.ok) {
            addMessage(data.message || "An error occurred.", "ai");
            if (data.insufficient_stock) renderStockWarning(data);
            return;
        }

        if (data.ambiguous && Array.isArray(data.matches)) {
            renderAmbiguousPicker(data.message, data.matches, data.pending_qty);
        } else if (data.inventory && Array.isArray(data.inventory)) {
            renderInventoryPicker(data.message, data.inventory);
        } else if (data.orders) {
            renderOrderList(data.orders);
        } else if (data.order) {
            renderOrderCard(data.order);
            loadDashboard();
            if (document.getElementById("view-inventory")?.classList.contains("active")) {
                loadInventoryView();
            }
        } else if (data.inventory_item || data.item) {
            renderInventoryCard(data.inventory_item || data.item);
        } else {
            addMessage(data.message || JSON.stringify(data), "ai");
            if (data.message && (
                data.message.includes("Consumed") ||
                data.message.includes("Stock") ||
                data.message.includes("Reorder") ||
                data.message.includes("status set")
            )) {
                loadDashboard();
                if (document.getElementById("view-inventory")?.classList.contains("active")) {
                    loadInventoryView();
                }
            }
        }
    } catch (err) {
        removeTyping(typingId);
        addMessage("⚠️ Error: " + err.message, "ai");
    }
}

function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
}

function renderStockWarning(data) {
    const container = document.getElementById("chatMessages");
    const div = document.createElement("div");
    div.className = "msg-row ai";
    div.innerHTML = `
        <div class="msg-content">
            <div class="msg-bubble stock-warning-card">
                <div class="stock-warning-header">
                    <span>⚠️</span>
                    <strong>Insufficient Stock</strong>
                </div>
                <div class="stock-warning-body">
                    <div class="stock-warning-row">
                        <span class="stock-label">Part</span>
                        <span class="stock-val">${data.part_name || "—"}</span>
                    </div>
                    <div class="stock-warning-row">
                        <span class="stock-label">Requested</span>
                        <span class="stock-val qty-low">${data.requested} ${data.unit || "pcs"}</span>
                    </div>
                    <div class="stock-warning-row">
                        <span class="stock-label">Available</span>
                        <span class="stock-val qty-ok">${data.available} ${data.unit || "pcs"}</span>
                    </div>
                </div>
                <div class="stock-warning-hint">
                    Try ordering ${data.available} or fewer units, or wait for a restock.
                </div>
            </div>
        </div>`;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
}

function addMessage(text, sender) {
    const container = document.getElementById("chatMessages");
    const div = document.createElement("div");
    div.className = `msg-row ${sender}`;
    div.innerHTML = `
        <div class="msg-content">
            <div class="msg-bubble">${text.replace(/\n/g, "<br>")}</div>
        </div>`;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
    return div;
}

function addTyping() {
    const id        = "typing-" + Date.now();
    const container = document.getElementById("chatMessages");
    const div = document.createElement("div");
    div.className = "msg-row ai";
    div.id = id;
    div.innerHTML = `
        <div class="msg-content">
            <div class="msg-bubble">
                <div class="typing-indicator"><span></span><span></span><span></span></div>
            </div>
        </div>`;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
    return id;
}

function removeTyping(id) {
    const el = document.getElementById(id);
    if (el) el.remove();
}

// Ambiguous match picker — shown when multiple inventory items match what
// the user typed (e.g. "order steel rods" matches 3 different steel rods).
// Uses /api/chat/confirm-order directly (no LLM round-trip) since we already
// know both the exact item and the quantity the user asked for.
function renderAmbiguousPicker(msg, matches, pendingQty) {
    const container = document.getElementById("chatMessages");
    const qty = parseInt(pendingQty) || 1;

    const rows = matches.map(it => {
        const isLow = it.quantity <= (it.reorder_at || 0);
        return `
        <tr class="inv-pick-row" onclick="confirmOrderFromPicker(${it.id}, '${(it.part_name || "").replace(/'/g, "\\'")}', ${it.quantity}, ${qty})">
            <td class="inv-pick-id">#${it.id}</td>
            <td class="inv-pick-name">${it.part_name}</td>
            <td>${it.material || "—"}</td>
            <td class="inv-pick-qty ${isLow ? "low" : ""}">${it.quantity} ${it.unit}${isLow ? " ⚠" : ""}</td>
        </tr>`;
    }).join("");

    const div = document.createElement("div");
    div.className = "msg-row ai";
    div.innerHTML = `
        <div class="msg-content" style="max-width:90%">
            <div class="msg-bubble">
                <p>${msg}</p>
                <div class="inv-picker-wrap">
                    <div class="inv-picker-label"><i class="ti ti-list-search"></i> Ordering ${qty} — click the item you meant</div>
                    <table class="inv-picker-table">
                        <thead><tr><th>ID</th><th>Part</th><th>Material</th><th>Stock</th></tr></thead>
                        <tbody>${rows}</tbody>
                    </table>
                </div>
            </div>
        </div>`;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
}

// Fallback picker — shown when nothing in inventory matched at all, so the
// whole catalog is offered instead.
function renderInventoryPicker(msg, items) {
    const container = document.getElementById("chatMessages");

    if (!items.length) {
        const div = document.createElement("div");
        div.className = "msg-row ai";
        div.innerHTML = `
            <div class="msg-content">
                <div class="msg-bubble">
                    <p>${msg}</p>
                    <p style="color:var(--warn);margin-top:8px">
                        ⚠️ Your inventory is empty.
                        <a href="#" onclick="showView('inventory');return false;" style="color:var(--brand)">
                            Go to Inventory tab
                        </a> to add items first.
                    </p>
                </div>
            </div>`;
        container.appendChild(div);
        container.scrollTop = container.scrollHeight;
        return;
    }

    const rows = items.map(it => {
        const isLow = it.quantity <= (it.reorder_at || 0);
        return `
        <tr class="inv-pick-row" onclick="confirmOrderFromPicker(${it.id}, '${(it.part_name || "").replace(/'/g, "\\'")}', ${it.quantity}, null)">
            <td class="inv-pick-id">#${it.id}</td>
            <td class="inv-pick-name">${it.part_name}</td>
            <td>${it.material || "—"}</td>
            <td class="inv-pick-qty ${isLow ? "low" : ""}">${it.quantity} ${it.unit}${isLow ? " ⚠" : ""}</td>
        </tr>`;
    }).join("");

    const div = document.createElement("div");
    div.className = "msg-row ai";
    div.innerHTML = `
        <div class="msg-content" style="max-width:90%">
            <div class="msg-bubble">
                <p>${msg}</p>
                <div class="inv-picker-wrap">
                    <div class="inv-picker-label"><i class="ti ti-package"></i> Available Inventory — click a row to order</div>
                    <table class="inv-picker-table">
                        <thead><tr><th>ID</th><th>Part</th><th>Material</th><th>Stock</th></tr></thead>
                        <tbody>${rows}</tbody>
                    </table>
                </div>
            </div>
        </div>`;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
}

// Places an order directly via /api/chat/confirm-order — no LLM round-trip.
// Used by both picker tables and the dashboard/inventory "Order" buttons.
// knownQty is set when we already know the intended quantity (disambiguation
// flow); otherwise the user is prompted.
async function confirmOrderFromPicker(invId, partName, availableStock, knownQty) {
    if (availableStock === 0) {
        alert(`❌ "${partName}" is out of stock. No orders can be placed until restocked.`);
        return;
    }

    let qty = knownQty ? parseInt(knownQty) : null;
    if (!qty) {
        const input = prompt(`How many "${partName}" to order?\n(Available stock: ${availableStock})`, Math.min(50, availableStock));
        if (!input || isNaN(parseInt(input))) return;
        qty = parseInt(input);
    }

    if (qty <= 0) { alert("❌ Order quantity must be greater than 0."); return; }
    if (qty > availableStock) {
        alert(`❌ Insufficient stock for "${partName}".\nRequested: ${qty} | Available: ${availableStock}\n\nPlease enter a quantity of ${availableStock} or less.`);
        return;
    }

    try {
        const res  = await fetch("/api/chat/confirm-order", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ inventory_id: invId, quantity: qty }),
        });
        const data = await res.json();

        if (!res.ok || data.insufficient_stock) {
            addMessage(data.message || "❌ Insufficient stock. Order not placed.", "ai");
            return;
        }

        if (data.order) renderOrderCard(data.order);
        else addMessage(data.message || "✅ Order placed!", "ai");

        loadDashboard();
        if (document.getElementById("view-inventory")?.classList.contains("active")) {
            loadInventoryView();
        }
    } catch (err) {
        addMessage("⚠️ Error: " + err.message, "ai");
    }
}

function renderOrderList(orders) {
    if (!orders.length) { addMessage("No orders found.", "ai"); return; }
    const rows = orders.map(o => `
        <tr>
            <td class="order-id-cell">#${o.id}</td>
            <td>${o.part_name || "—"}</td>
            <td>${o.quantity ?? "—"}</td>
            <td>${o.deadline || "—"}</td>
            <td>${statusBadge(o.status)}</td>
        </tr>`).join("");

    const container = document.getElementById("chatMessages");
    const div = document.createElement("div");
    div.className = "msg-row ai";
    div.innerHTML = `
        <div class="msg-content" style="max-width:90%">
            <div class="msg-bubble">
                <div class="inline-table-wrap">
                    <table class="inline-table">
                        <thead><tr><th>ID</th><th>Part</th><th>Qty</th><th>Deadline</th><th>Status</th></tr></thead>
                        <tbody>${rows}</tbody>
                    </table>
                </div>
            </div>
        </div>`;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
}

function renderOrderCard(o) {
    const stockNote = (o.new_stock !== undefined)
        ? `<div class="order-field"><div class="field-key">Stock Remaining</div><div class="field-val ${o.new_stock <= 10 ? 'qty-low' : 'qty-ok'}">${o.new_stock}</div></div>`
        : "";

    addMessage(`
        <div class="order-card">
            <div class="order-card-header">
                <span>Order #${o.id}</span>
                ${statusBadge(o.status)}
            </div>
            <div class="order-card-grid">
                <div class="order-field"><div class="field-key">Part</div><div class="field-val">${o.part_name || "—"}</div></div>
                <div class="order-field"><div class="field-key">Material</div><div class="field-val">${o.material || "—"}</div></div>
                <div class="order-field"><div class="field-key">Quantity Ordered</div><div class="field-val">${o.quantity ?? "—"}</div></div>
                <div class="order-field"><div class="field-key">Deadline</div><div class="field-val">${o.deadline || "—"}</div></div>
                <div class="order-field"><div class="field-key">Status</div><div class="field-val">${statusBadge(o.status)}</div></div>
                ${stockNote}
            </div>
        </div>`, "ai");
}

function renderInventoryCard(item) {
    const pct     = item.reorder_at ? Math.round((item.quantity / (item.reorder_at * 10)) * 100) : 0;
    const lowFlag = item.quantity <= item.reorder_at;
    addMessage(`
        <div class="order-card">
            <div class="order-card-header">
                <span><i class="ti ti-package"></i> Inventory #${item.id} — ${item.part_name}</span>
                ${lowFlag ? '<span class="badge badge-review"><span class="badge-dot"></span>Low Stock</span>' : '<span class="badge badge-accepted"><span class="badge-dot"></span>In Stock</span>'}
            </div>
            <div class="order-card-grid">
                <div class="order-field"><div class="field-key">Material</div><div class="field-val">${item.material || "—"}</div></div>
                <div class="order-field"><div class="field-key">Quantity</div><div class="field-val">${item.quantity} ${item.unit}</div></div>
                <div class="order-field"><div class="field-key">Reorder At</div><div class="field-val">${item.reorder_at} ${item.unit}</div></div>
                <div class="order-field" style="grid-column:span 3">
                    <div class="field-key">Stock Level</div>
                    <div class="stock-bar-wrap"><div class="stock-bar" style="width:${Math.min(100,pct)}%;background:${lowFlag?'var(--warn)':'var(--good)'}"></div></div>
                </div>
            </div>
        </div>`, "ai");
}

function handleKey(event) {
    if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        sendMessage();
    }
}

function autoResize(el) {
    el.style.height = "auto";
    el.style.height = el.scrollHeight + "px";
}

function injectCmd(text) {
    showView("chat");
    const input = document.getElementById("chatInput");
    input.value = text;
    input.focus();
}

// ═══════════════════════════════════════════════════════
// VIEW SWITCHING
// ═══════════════════════════════════════════════════════

function showView(view) {
    document.querySelectorAll(".view").forEach(v => v.classList.remove("active"));
    document.querySelectorAll(".nav-item").forEach(b => b.classList.remove("active"));

    const target = document.getElementById("view-" + view);
    if (target) target.classList.add("active");

    const navBtn = document.querySelector(`.nav-item[data-view="${view}"]`);
    if (navBtn) navBtn.classList.add("active");

    // Keep the top bar's second line in step, and close the drawer so a
    // nav tap doesn't leave it covering the view it just opened.
    updateTopBarView(view, navBtn);
    closeNav();

    if (view === "dashboard") loadDashboard();
    if (view === "inventory") loadInventoryView();
    if (view === "purchasing") loadPurchasing();
    if (view === "quality") loadQuality();
    if (view === "reports") loadReports();
    if (view === "production") loadProduction();
    if (view === "notifications") loadNotifications();
    if (view === "integrations") loadIntegrations();
    if (view === "profile") loadProfile();
    if (view === "admin") { loadUsers(); loadAuditLog(); }
}

// ═══════════════════════════════════════════════════════
// DASHBOARD
// ═══════════════════════════════════════════════════════

async function loadDashboard() {
    try {
        const [ordRes, invRes] = await Promise.all([
            fetch("/api/orders"),
            fetch("/api/inventory"),
        ]);
        allOrders    = await ordRes.json();
        allInventory = await invRes.json();

        // Null-safe: the bento dashboard renders only some of these tiles,
        // and other views reuse the same ids.
        const setStat = (id, val) => {
            const node = document.getElementById(id);
            if (node) node.innerText = val;
        };
        setStat("statTotal",    allOrders.length);
        setStat("statReceived", allOrders.filter(o => o.status === "Received").length);
        setStat("statReview",   allOrders.filter(o => o.status === "In Review").length);
        setStat("statAccepted", allOrders.filter(o => o.status === "Accepted").length);
        setStat("statLowStock", allInventory.filter(i => i.quantity <= i.reorder_at).length);

        renderOrders(allOrders);
        renderInventorySummary(allInventory);
        renderOpsPanels(allOrders, allInventory);   // no-ops unless those tiles exist
        renderBento(allOrders, allInventory);
        renderHeroDate();

        // Refresh whichever demand tabs are already loaded, so the section
        // doesn't show stale numbers after an order/scan changes stock.
        if (demandOpen) {
            loadForecast();
            loadGapAnalysis();
            if (predLoaded) loadPredictions();
        }
    } catch (err) {
        console.error("Dashboard error:", err);
    }
}

// ═══════════════════════════════════════════════════════════════════
// OPERATIONS PANELS — stock gauges + order pipeline
// Both read the data loadDashboard() has already fetched; neither
// makes its own request.
// ═══════════════════════════════════════════════════════════════════

// ═══════════════════════════════════════════════════════════════════
// SLIDE-OUT NAVIGATION
// The sidebar is a drawer: hidden by default, slid in by the hamburger,
// dismissed by the scrim, Escape, or picking a destination.
// ═══════════════════════════════════════════════════════════════════

function navIsOpen() { return document.body.classList.contains("nav-open"); }

function setNav(open) {
    document.body.classList.toggle("nav-open", open);
    const btn = document.getElementById("navToggle");
    if (btn) {
        btn.setAttribute("aria-expanded", open ? "true" : "false");
        btn.setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
    }
}

function toggleNav() { setNav(!navIsOpen()); }
function closeNav()  { if (navIsOpen()) setNav(false); }

document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeNav();
});

/* Second line of the top bar — the current destination. Reads the label
   straight off the nav button so it can never drift from the menu. */
function updateTopBarView(view, navBtn) {
    const el = document.getElementById("topBarView");
    if (!el) return;
    const btn = navBtn || document.querySelector(`.nav-item[data-view="${view}"]`);
    const label = btn ? (btn.textContent || "").trim() : "";
    el.textContent = label || (view ? view.charAt(0).toUpperCase() + view.slice(1) : "");
}

const PIPELINE_STEPS = ["Received", "In Review", "Accepted"];

// ═══════════════════════════════════════════════════════════════════
// BENTO DASHBOARD
// Nine tiles, all populated from the orders + inventory already in
// memory. Each helper bails out quietly if its tile isn't on the page,
// so other views can reuse this file unchanged.
// ═══════════════════════════════════════════════════════════════════

function renderBento(orders, inventory) {
    orders = orders || [];
    inventory = inventory || [];
    bentoFeature(inventory);
    bentoDonut(inventory);
    bentoRole();
    bentoStreak(orders);
    bentoCircles(inventory);
    bentoActivity(orders);
    bentoTrend(orders, inventory);
    const sub = document.getElementById("bOrdersSub");
    if (sub) {
        const open = orders.filter(o => o.status !== "Cancelled" && o.status !== "Accepted").length;
        sub.textContent = `${open} still open`;
    }
}

const setTxt = (id, val) => { const n = document.getElementById(id); if (n) n.textContent = val; };

/* Feature tile — whichever item is furthest into trouble. */
function bentoFeature(inventory) {
    if (!document.getElementById("bStockPart")) return;
    if (!inventory.length) {
        setTxt("bStockPart", "No inventory yet");
        setTxt("bStockMat", "Add an item to get started");
        setTxt("bStockQty", "—");
        setTxt("bStockState", "empty");
        return;
    }
    const worst = inventory.slice().sort((a, b) =>
        (a.quantity / Math.max(a.reorder_at || 1, 1)) - (b.quantity / Math.max(b.reorder_at || 1, 1))
    )[0];

    const reorder = Math.max(Number(worst.reorder_at) || 1, 1);
    const qty     = Number(worst.quantity) || 0;
    const state   = qty <= reorder ? "Reorder now" : qty <= reorder * 1.5 ? "Running low" : "Healthy";

    setTxt("bStockPart", worst.part_name || "—");
    setTxt("bStockMat", worst.material || " ");
    setTxt("bStockQty", qty);
    setTxt("bStockUnit", `${worst.unit || "pcs"} in stock`);
    setTxt("bStockState", state);
    setTxt("bStockReorder", `reorder at ${reorder}`);
    setTxt("bStockCount", `${inventory.length} items tracked`);
}

/* Dark donut — share of items sitting above their reorder point. */
function bentoDonut(inventory) {
    const host = document.getElementById("bDonut");
    if (!host) return;
    const total = inventory.length;
    const ok    = inventory.filter(i => (Number(i.quantity) || 0) > (Number(i.reorder_at) || 0)).length;
    const pct   = total ? Math.round((ok / total) * 100) : 0;

    const R = 46, C = 2 * Math.PI * R;
    const stroke = pct >= 80 ? "var(--good)" : pct >= 50 ? "var(--warn)" : "var(--critical)";

    host.innerHTML = `
      <svg width="120" height="120" viewBox="0 0 120 120" role="img"
           aria-label="${pct}% of items above their reorder point">
        <circle cx="60" cy="60" r="${R}" fill="none" stroke="rgba(255,255,255,0.12)" stroke-width="11"></circle>
        <circle cx="60" cy="60" r="${R}" fill="none" stroke="${stroke}" stroke-width="11"
                stroke-linecap="round" transform="rotate(-90 60 60)"
                stroke-dasharray="${(C * pct / 100).toFixed(1)} ${C.toFixed(1)}"></circle>
        <text class="donut-pct" x="60" y="58" text-anchor="middle">${pct}%</text>
        <text class="donut-sub" x="60" y="74" text-anchor="middle">${ok} of ${total} ok</text>
      </svg>`;
}

/* Access tile — mirrors whatever the session currently is. */
function bentoRole() {
    if (!document.getElementById("bRoleName")) return;
    if (!authState || !authState.authenticated) {
        setTxt("bRoleName", "Not signed in");
        setTxt("bRoleSub", "Log in to approve and administer");
        return;
    }
    setTxt("bRoleName", authState.username);
    setTxt("bRoleSub", authState.acting_as
        ? `Acting as ${authState.role} · account is ${authState.account_role}`
        : `Signed in as ${authState.role}`);
}

/* Activity streak — a dot per day for the last 30, filled where an
   order was created. */
function bentoStreak(orders) {
    const dots = document.getElementById("bStreakDots");
    if (!dots) return;
    const DAYS = 30;
    const days = new Set(
        orders.map(o => (o.created_at || "").slice(0, 10)).filter(Boolean)
    );

    let html = "", active = 0;
    const today = new Date();
    for (let i = DAYS - 1; i >= 0; i--) {
        const d = new Date(today);
        d.setDate(d.getDate() - i);
        const key = d.toISOString().slice(0, 10);
        const on = days.has(key);
        if (on) active++;
        html += `<i class="${on ? "on" : ""}" title="${key}"></i>`;
    }
    dots.innerHTML = html;
    setTxt("bStreakDays", active);
    setTxt("bStreakSub", `days with activity · last ${DAYS}`);
}

/* Nested circles — the four largest holdings, area proportional to
   units held, sharing a baseline so they read as nested bands. */
function bentoCircles(inventory) {
    const host = document.getElementById("bCircles");
    if (!host) return;
    const top = inventory.slice()
        .sort((a, b) => (Number(b.quantity) || 0) - (Number(a.quantity) || 0))
        .slice(0, 4);
    if (!top.length) { host.innerHTML = `<div class="act-empty">No inventory yet.</div>`; return; }

    const max = Number(top[0].quantity) || 1;
    const RMAX = 62, BASE = 140;
    const shades = ["#fbe3dd", "#f6bcae", "#ef8e78", "#e8563f"];

    // Radius stays proportional to the square root of the quantity, so area
    // encodes the value honestly. Two items with similar quantities therefore
    // produce nearly identical circles — which is correct, but means a label
    // printed inside each band would overlap. Names and values go in a legend
    // underneath instead, and only the largest keeps an inline figure.
    let svg = `<svg width="200" height="150" viewBox="0 0 200 150" role="img"
                    aria-label="Largest stock holdings by units">`;
    top.forEach((it, i) => {
        const q = Number(it.quantity) || 0;
        const r = Math.max(RMAX * Math.sqrt(q / max), 14);
        svg += `<circle cx="100" cy="${(BASE - r).toFixed(1)}" r="${r.toFixed(1)}"
                        fill="${shades[Math.min(i, shades.length - 1)]}"></circle>`;
    });
    const rTop = RMAX;
    svg += `<text class="circle-label" x="100" y="${(BASE - 2 * rTop + 16).toFixed(1)}"
                  text-anchor="middle">${Number(top[0].quantity) || 0}</text></svg>`;

    const legend = top.map((it, i) => `
      <li>
        <span class="lg-dot" style="background:${shades[Math.min(i, shades.length - 1)]}"></span>
        <span class="lg-name" title="${escapeHtml(it.part_name)}">${escapeHtml(it.part_name)}</span>
        <span class="lg-val">${Number(it.quantity) || 0}</span>
      </li>`).join("");

    host.innerHTML = svg + `<ul class="circle-legend">${legend}</ul>`;
}

/* Activity manager — status chips with live counts, plus recent orders. */
let bentoFilter = "All";

function bentoActivity(orders) {
    const chips = document.getElementById("bActChips");
    const list  = document.getElementById("bActList");
    if (!chips || !list) return;

    const counts = {
        All: orders.length,
        Received: orders.filter(o => o.status === "Received").length,
        "In Review": orders.filter(o => o.status === "In Review").length,
        Accepted: orders.filter(o => o.status === "Accepted").length,
    };

    chips.innerHTML = Object.keys(counts).map(k =>
        `<button class="b-chip ${bentoFilter === k ? "on" : ""}" onclick="setBentoFilter('${k.replace(/'/g, "\\'")}')">
           <span class="dot"></span>${k} ${counts[k]}
         </button>`).join("");

    const rows = (bentoFilter === "All" ? orders : orders.filter(o => o.status === bentoFilter)).slice(0, 4);
    setTxt("bActCount", `${rows.length} shown`);

    list.innerHTML = rows.length ? rows.map(o => `
      <div class="act-row">
        <span class="act-ico"><i class="ti ti-package"></i></span>
        <span class="act-main">
          <span class="act-name">#${o.id} · ${escapeHtml(o.part_name || "—")}</span>
          <span class="act-meta">${o.quantity ?? "—"} units${o.deadline ? " · due " + escapeHtml(o.deadline) : ""}</span>
        </span>
        <span class="act-status">${escapeHtml(o.status || "—")}</span>
      </div>`).join("")
      : `<div class="act-empty">No ${bentoFilter === "All" ? "" : bentoFilter.toLowerCase() + " "}orders.</div>`;
}

function setBentoFilter(name) {
    bentoFilter = name;
    bentoActivity(allOrders || []);
}

/* Trend tile — total units held, with a sparkline of order volume. */
function bentoTrend(orders, inventory) {
    const spark = document.getElementById("bTrendSpark");
    if (!spark) return;

    const units = inventory.reduce((sum, i) => sum + (Number(i.quantity) || 0), 0);
    setTxt("bTrendValue", units.toLocaleString());
    setTxt("bTrendSub", `across ${inventory.length} item${inventory.length === 1 ? "" : "s"}`);

    // Orders per day across the last 30 days.
    const DAYS = 30, series = [];
    const today = new Date();
    for (let i = DAYS - 1; i >= 0; i--) {
        const d = new Date(today);
        d.setDate(d.getDate() - i);
        const key = d.toISOString().slice(0, 10);
        series.push(orders.filter(o => (o.created_at || "").slice(0, 10) === key).length);
    }

    const total  = series.reduce((a, b) => a + b, 0);
    const half   = Math.floor(DAYS / 2);
    const recent = series.slice(-half).reduce((a, b) => a + b, 0);
    const prior  = series.slice(0, half).reduce((a, b) => a + b, 0);
    const dEl = document.getElementById("bTrendDelta");
    if (dEl) {
        if (!total) {                          // nothing in the window at all
            dEl.textContent = "no recent orders";
            dEl.className = "b-delta";
        } else {
            const delta = prior === 0 ? 100 : Math.round(((recent - prior) / prior) * 100);
            dEl.textContent = `${delta >= 0 ? "+" : ""}${delta}%`;
            dEl.className = `b-delta ${delta >= 0 ? "up" : "down"}`;
        }
    }

    // A flat line pinned to the baseline reads as a broken chart, so when
    // there is genuinely no order volume in the window, say so instead.
    if (!total) {
        spark.innerHTML = `<div class="spark-empty">No orders in the last ${DAYS} days</div>`;
        return;
    }

    const W = 240, H = 54, max = Math.max(...series, 1);
    const pts = series.map((v, i) => {
        const x = (i / (series.length - 1)) * W;
        const y = H - (v / max) * (H - 8) - 4;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
    });

    spark.innerHTML = `
      <svg width="100%" height="${H}" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"
           role="img" aria-label="Order volume over the last ${DAYS} days">
        <polygon points="0,${H} ${pts.join(" ")} ${W},${H}" fill="var(--brand-dim)"></polygon>
        <polyline points="${pts.join(" ")}" fill="none" stroke="var(--brand)"
                  stroke-width="2" stroke-linejoin="round" stroke-linecap="round"></polyline>
      </svg>`;
}

function renderOpsPanels(orders, inventory) {
    renderStockGauges(inventory);
    renderOrderPipeline(orders);
}

/* A dial per item, showing stock against its reorder point. The arc is
   scaled so the reorder level sits at the halfway mark — that way "needle
   below the middle" always means "at or under reorder", whatever the
   absolute quantities are. */
function renderStockGauges(inventory) {
    const host = document.getElementById("stockGauges");
    if (!host) return;

    const items = (inventory || []).slice().sort((a, b) => {
        const ra = (a.reorder_at || 1), rb = (b.reorder_at || 1);
        return (a.quantity / ra) - (b.quantity / rb);      // worst health first
    }).slice(0, 4);

    if (!items.length) {
        host.innerHTML = `<div class="ops-empty">No inventory items yet.</div>`;
        renderStockAlert([]);
        return;
    }

    const R = 38, C = 2 * Math.PI * R, TRACK = C * 0.75;   // 270° dial

    host.innerHTML = items.map((it) => {
        const reorder = Math.max(Number(it.reorder_at) || 1, 1);
        const qty     = Number(it.quantity) || 0;
        const pct     = Math.max(0, Math.min(qty / (reorder * 2), 1));

        // Always semantic, never the brand hue — the accent colour differs
        // per theme (lime in one, coral in another), and a coral "healthy"
        // dial next to a green HEALTHY chip reads as a contradiction.
        let state = "ok", stroke = "var(--good)";
        if (qty <= reorder)            { state = "low";  stroke = "var(--critical)"; }
        else if (qty <= reorder * 1.5) { state = "warn"; stroke = "var(--warn)"; }

        const label = state === "low" ? "Reorder" : state === "warn" ? "Low" : "Healthy";

        return `
      <div class="gauge">
        <svg width="96" height="96" viewBox="0 0 96 96" role="img"
             aria-label="${escapeHtml(it.part_name)}: ${qty} in stock, reorder at ${reorder}">
          <g transform="rotate(135 48 48)">
            <circle class="gauge-track" cx="48" cy="48" r="${R}" fill="none"
                    stroke-width="8" stroke-linecap="round"
                    stroke-dasharray="${TRACK.toFixed(1)} ${C.toFixed(1)}"></circle>
            <circle class="gauge-fill" cx="48" cy="48" r="${R}" fill="none"
                    stroke="${stroke}" stroke-width="8" stroke-linecap="round"
                    stroke-dasharray="${(TRACK * pct).toFixed(1)} ${C.toFixed(1)}"></circle>
          </g>
          <text class="gauge-value" x="48" y="46" text-anchor="middle">${qty}</text>
          <text class="gauge-unit"  x="48" y="61" text-anchor="middle">${escapeHtml(it.unit || "pcs")}</text>
        </svg>
        <div class="gauge-name" title="${escapeHtml(it.part_name)}">${escapeHtml(it.part_name)}</div>
        <div class="gauge-meta">reorder at ${reorder}</div>
        <span class="gauge-chip ${state}">${label}</span>
      </div>`;
    }).join("");

    renderStockAlert(inventory.filter(i => (Number(i.quantity) || 0) <= (Number(i.reorder_at) || 0)));
}

function renderStockAlert(lowItems) {
    const box = document.getElementById("stockAlert");
    if (!box) return;
    if (!lowItems.length) {
        box.innerHTML = `<div class="ops-alert ok">
            <i class="ti ti-circle-check"></i>
            <span>All items are above their reorder point.</span>
          </div>`;
        return;
    }
    const names = lowItems.slice(0, 3).map(i => escapeHtml(i.part_name)).join(", ");
    const more  = lowItems.length > 3 ? ` and ${lowItems.length - 3} more` : "";
    box.innerHTML = `<div class="ops-alert">
        <i class="ti ti-alert-triangle"></i>
        <span><b>${lowItems.length} item${lowItems.length > 1 ? "s" : ""}</b> ${lowItems.length > 1 ? "need" : "needs"} reordering: ${names}${more}</span>
      </div>`;
}

/* One row per open order, showing how far it has moved through the
   pipeline, with its deadline on the right. */
function renderOrderPipeline(orders) {
    const host = document.getElementById("orderPipeline");
    if (!host) return;

    const open = (orders || []).filter(o => o.status !== "Cancelled").slice(0, 5);
    if (!open.length) {
        host.innerHTML = `<div class="ops-empty">No active orders.</div>`;
        return;
    }

    host.innerHTML = open.map((o) => {
        const idx  = PIPELINE_STEPS.indexOf(o.status);
        const step = idx < 0 ? 0 : idx;
        const pct  = ((step + 1) / PIPELINE_STEPS.length) * 100;
        const done = o.status === "Accepted";

        // Deadlines are free text ("next Friday"), so only flag it as late
        // when the value actually parses to a date that has passed.
        const parsed = Date.parse(o.deadline);
        const late   = !done && !isNaN(parsed) && parsed < Date.now();

        const steps = PIPELINE_STEPS.map((s, i) => {
            const cls = i < step ? "done" : i === step ? "at" : "";
            return `<span class="${cls}">${s}</span>`;
        }).join("");

        return `
      <div class="pipe">
        <div class="pipe-top">
          <span class="pipe-name">#${o.id} &middot; ${escapeHtml(o.part_name || "—")}</span>
          <span class="pipe-qty">${o.quantity ?? "—"}${o.material ? " · " + escapeHtml(o.material) : ""}</span>
        </div>
        <div class="pipe-route">
          <span>Status <b>${escapeHtml(o.status || "—")}</b></span>
          <span>${late ? "Overdue" : "Due"} <b>${escapeHtml(o.deadline || "—")}</b></span>
        </div>
        <div class="pipe-bar ${done ? "is-done" : late ? "is-late" : ""}">
          <span style="width:${pct}%"></span>
        </div>
        <div class="pipe-steps">${steps}</div>
      </div>`;
    }).join("");
}

// ═══════════════════════════════════════════════════════
// INVENTORY VIEW
// ═══════════════════════════════════════════════════════

async function loadInventoryView() {
    try {
        const res    = await fetch("/api/inventory");
        allInventory = await res.json();
        renderInventoryTable(allInventory);
    } catch (err) {
        console.error("Inventory error:", err);
    }
}

function renderInventorySummary(items) {
    const tbody = document.getElementById("inventorySummaryBody");
    if (!tbody) return;

    if (!items.length) {
        tbody.innerHTML = `<tr><td colspan="7" class="table-empty">No inventory items yet — go to Inventory tab to add some.</td></tr>`;
        return;
    }

    tbody.innerHTML = items.map(item => {
        const low = item.quantity <= item.reorder_at;
        return `
        <tr>
            <td class="order-id-cell">#${item.id}</td>
            <td class="part-name-cell">${item.part_name}</td>
            <td>${item.material || "—"}</td>
            <td class="${low ? "qty-low" : "qty-ok"}">${item.quantity} ${item.unit}</td>
            <td>${item.reorder_at}</td>
            <td>${low ? stockBadge("low") : stockBadge("ok")}</td>
            <td>
                <button class="table-action-btn ${item.quantity === 0 ? 'btn-disabled' : ''}"
                    onclick="quickOrder(${item.id}, '${item.part_name.replace(/'/g,"\\'")}', ${item.quantity})"
                    ${item.quantity === 0 ? 'disabled title="No stock available"' : ''}>
                    <i class="ti ti-plus"></i> Order
                </button>
            </td>
        </tr>`;
    }).join("");
}

function renderInventoryTable(items) {
    const tbody = document.getElementById("inventoryTableBody");
    if (!tbody) return;

    if (!items.length) {
        tbody.innerHTML = `<tr><td colspan="10" class="table-empty">No inventory items yet. Use the form above to add your first item.</td></tr>`;
        return;
    }

    tbody.innerHTML = items.map(item => {
        const low = item.quantity <= item.reorder_at;
        const pct = item.reorder_at
            ? Math.min(100, Math.round((item.quantity / (item.reorder_at * 8)) * 100))
            : 50;
        return `
        <tr>
            <td class="order-id-cell">#${item.id}</td>
            <td class="part-name-cell">${item.part_name}</td>
            <td>${item.material || "—"}</td>
            <td>${item.unit}</td>
            <td class="${low ? "qty-low" : "qty-ok"}">${item.quantity}</td>
            <td>${item.reorder_at}</td>
            <td>
                <div class="mini-bar-wrap">
                    <div class="mini-bar" style="width:${pct}%;background:${low ? 'var(--warn)' : 'var(--good)'}"></div>
                </div>
            </td>
            <td>${low ? stockBadge("low") : stockBadge("ok")}</td>
            <td>
                <button class="table-action-btn ${item.quantity === 0 ? 'btn-disabled' : ''}"
                    onclick="quickOrder(${item.id}, '${item.part_name.replace(/'/g,"\\'")}', ${item.quantity})"
                    ${item.quantity === 0 ? 'disabled title="No stock available"' : ''}>
                    <i class="ti ti-plus"></i> Order
                </button>
            </td>
            <td>
                <button class="table-action-btn delete-btn" onclick="deleteInventoryItem(${item.id}, '${item.part_name.replace(/'/g,"\\'")}')">
                    <i class="ti ti-trash"></i>
                </button>
            </td>
        </tr>`;
    }).join("");
}

function stockBadge(type) {
    if (type === "low") return `<span class="badge badge-review"><span class="badge-dot"></span>Low Stock</span>`;
    return `<span class="badge badge-accepted"><span class="badge-dot"></span>In Stock</span>`;
}

function quickOrder(inventoryId, partName, availableStock) {
    confirmOrderFromPicker(inventoryId, partName, availableStock, null);
}

// ═══════════════════════════════════════════════════════
// ORDERS TABLE
// ═══════════════════════════════════════════════════════

function renderOrders(orders) {
    const tbody = document.getElementById("ordersTableBody");
    if (!tbody) return;
    if (!orders.length) {
        tbody.innerHTML = `<tr><td colspan="9" class="table-empty">No orders yet.</td></tr>`;
        return;
    }
    tbody.innerHTML = orders.map(o => {
        const paymentCell = o.status === "Cancelled"
            ? `<span style="color:var(--text-muted);font-size:11.5px">—</span>`
            : o.paid
                ? `<span class="badge badge-accepted"><span class="badge-dot"></span>Paid</span>`
                : `<button class="po-action-btn primary" onclick="payOrder(${o.id})" title="Requires admin/operator login"><i class="ti ti-credit-card"></i> Collect</button>`;

        const actionsCell = o.status === "Cancelled"
            ? `<span style="color:var(--text-muted);font-size:11.5px">—</span>`
            : `<div style="display:flex;gap:6px">
                 <button class="icon-btn" onclick="openEditOrderModal(${o.id})" title="Edit order"><i class="ti ti-edit"></i></button>
                 <button class="icon-btn" onclick="cancelOrderRow(${o.id})" title="Cancel order"><i class="ti ti-x"></i></button>
               </div>`;

        return `
        <tr data-status="${o.status}">
            <td class="order-id-cell">#${o.id}</td>
            <td class="part-name-cell">${o.part_name || "—"}</td>
            <td>${o.material || "—"}</td>
            <td>${o.quantity ?? "—"}</td>
            <td>${o.deadline || "—"}</td>
            <td>${statusBadge(o.status)}</td>
            <td>
                <div class="status-btn-group">
                    <button class="status-step-btn ${o.status === 'Received'  ? 'active' : ''}" onclick="setOrderStatus(${o.id}, 'Received',  this)">Received</button>
                    <button class="status-step-btn ${o.status === 'In Review' ? 'active' : ''}" onclick="setOrderStatus(${o.id}, 'In Review', this)">In Review</button>
                    <button class="status-step-btn ${o.status === 'Accepted'  ? 'active' : ''}" onclick="setOrderStatus(${o.id}, 'Accepted',  this)">Accepted</button>
                </div>
            </td>
            <td>${paymentCell}</td>
            <td>${actionsCell}</td>
        </tr>`;
    }).join("");
}

async function setOrderStatus(orderId, status, btn) {
    try {
        const res  = await fetch(`/api/orders/${orderId}/status`, {
            method:  "PATCH",
            headers: { "Content-Type": "application/json" },
            body:    JSON.stringify({ status }),
        });
        const data = await res.json();
        if (res.ok) {
            loadDashboard();
        } else {
            alert(data.message);
        }
    } catch (err) {
        alert("Error: " + err.message);
    }
}

function statusBadge(status) {
    const map = { Received: "badge-received", "In Review": "badge-review", Accepted: "badge-accepted", Cancelled: "badge-cancelled" };
    return `<span class="badge ${map[status] || "badge-received"}"><span class="badge-dot"></span>${status}</span>`;
}

function filterTable(status, btn) {
    document.querySelectorAll("#filterGroup .filter-btn").forEach(b => b.classList.remove("active"));
    if (btn) btn.classList.add("active");
    document.querySelectorAll("#ordersTableBody tr").forEach(row => {
        row.style.display = (status === "all" || row.dataset.status === status) ? "" : "none";
    });
}

// ── Edit / cancel an existing order (previously built on the backend
// with no UI ever wired to it — see openEditOrderModal / cancelOrderRow) ──
let editingOrderId = null;

function openEditOrderModal(id) {
    const order = allOrders.find(o => o.id === id);
    if (!order) return;
    editingOrderId = id;
    document.getElementById("editOrderIdLabel").textContent = "#" + id;
    document.getElementById("edit-order-qty").value = order.quantity ?? "";
    document.getElementById("edit-order-deadline").value = order.deadline || "";
    document.getElementById("edit-order-specs").value = order.specs || "";
    document.getElementById("editOrderMsg").textContent = "";
    document.getElementById("editOrderModal").style.display = "flex";
}

function closeEditOrderModal() {
    document.getElementById("editOrderModal").style.display = "none";
    editingOrderId = null;
}

async function saveOrderEdit() {
    if (!editingOrderId) return;
    const msg = document.getElementById("editOrderMsg");
    const qtyVal = document.getElementById("edit-order-qty").value;
    const payload = {
        quantity: qtyVal ? parseInt(qtyVal) : undefined,
        deadline: document.getElementById("edit-order-deadline").value,
        specs:    document.getElementById("edit-order-specs").value,
    };
    try {
        const res  = await fetch(`/api/orders/${editingOrderId}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (res.ok) {
            closeEditOrderModal();
            loadDashboard();
        } else {
            msg.style.color = "var(--critical-text)"; msg.textContent = data.message;
        }
    } catch (err) {
        msg.style.color = "var(--critical-text)"; msg.textContent = "Error: " + err.message;
    }
}

async function cancelOrderRow(id) {
    if (!confirm(`Cancel order #${id}? This can't be undone.`)) return;
    try {
        const res  = await fetch(`/api/orders/${id}/cancel`, { method: "POST" });
        const data = await res.json();
        if (!res.ok) { alert(data.message); return; }
        loadDashboard();
    } catch (err) {
        alert("Error: " + err.message);
    }
}

// ═══════════════════════════════════════════════════════
// ADD / DELETE INVENTORY ITEM
// ═══════════════════════════════════════════════════════

async function addInventoryItem() {
    const part_name = document.getElementById("inv-part-name").value.trim();
    const resultEl  = document.getElementById("addInventoryResult");

    if (!part_name) {
        resultEl.style.color = "var(--critical-text)";
        resultEl.textContent = "❌ Part name is required";
        return;
    }

    const payload = {
        part_name,
        material:   document.getElementById("inv-material").value.trim(),
        unit:       document.getElementById("inv-unit").value.trim() || "pcs",
        quantity:   parseInt(document.getElementById("inv-qty").value) || 0,
        reorder_at: parseInt(document.getElementById("inv-reorder").value) || 10,
        barcode:    document.getElementById("inv-barcode").value.trim() || null,
        rfid_tag:   document.getElementById("inv-rfid").value.trim() || null,
    };

    try {
        const res  = await fetch("/api/inventory", {
            method:  "POST",
            headers: { "Content-Type": "application/json" },
            body:    JSON.stringify(payload),
        });
        const data = await res.json();

        resultEl.style.color = res.ok ? "var(--good-text)" : "var(--critical-text)";
        resultEl.textContent = data.message;

        if (res.ok) {
            clearAddForm();
            loadInventoryView();
            loadDashboard();
        }
    } catch (err) {
        resultEl.style.color = "var(--critical-text)";
        resultEl.textContent = "Error: " + err.message;
    }
}

function clearAddForm() {
    ["inv-part-name", "inv-material", "inv-barcode", "inv-rfid"].forEach(
        id => { document.getElementById(id).value = ""; }
    );
    document.getElementById("inv-qty").value     = "0";
    document.getElementById("inv-reorder").value = "10";
    document.getElementById("inv-unit").value    = "pcs";
    document.getElementById("addInventoryResult").textContent = "";
}

async function deleteInventoryItem(id, name) {
    if (!confirm(`Delete "${name}" from inventory?\nThis will fail if orders reference this item.`)) return;

    try {
        const res  = await fetch(`/api/inventory/${id}`, { method: "DELETE" });
        const data = await res.json();
        alert(data.message);
        if (res.ok) {
            loadInventoryView();
            loadDashboard();
        }
    } catch (err) {
        alert("Error: " + err.message);
    }
}

// ═══════════════════════════════════════════════════════
// AUDIO
// ═══════════════════════════════════════════════════════

function playBeep(type) {
    try {
        const ctx  = new (window.AudioContext || window.webkitAudioContext)();
        const osc  = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.connect(gain);
        gain.connect(ctx.destination);
        if (type === "success") {
            osc.type = "sine";
            osc.frequency.setValueAtTime(880, ctx.currentTime);
            osc.frequency.setValueAtTime(1320, ctx.currentTime + 0.1);
            gain.gain.setValueAtTime(0.35, ctx.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.35);
            osc.start(ctx.currentTime); osc.stop(ctx.currentTime + 0.35);
        } else {
            osc.type = "sawtooth";
            osc.frequency.setValueAtTime(220, ctx.currentTime);
            gain.gain.setValueAtTime(0.25, ctx.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.4);
            osc.start(ctx.currentTime); osc.stop(ctx.currentTime + 0.4);
        }
    } catch {}
}

// ═══════════════════════════════════════════════════════
// USB RFID SCANNER ENGINE
// Listens for rapid keystrokes anywhere on the page (how a USB HID RFID
// reader "types" a tag) as long as focus isn't in a text field.
// ═══════════════════════════════════════════════════════

const RFID = (() => {
    const SCAN_GAP   = 80;
    const MIN_LENGTH = 4;
    let buffer = "", lastTime = 0, timer = null, enabled = true;

    function reset() { buffer = ""; if (timer) { clearTimeout(timer); timer = null; } }
    function flush() { const tag = buffer.trim(); reset(); if (tag.length >= MIN_LENGTH) handleRFIDScan(tag); }

    document.addEventListener("keydown", (e) => {
        if (!enabled) return;
        const active   = document.activeElement;
        const isTyping = active && (
            active.tagName === "TEXTAREA" ||
            (active.tagName === "INPUT" && active.id !== "rfidInput")
        );
        if (isTyping) return;

        const now = Date.now();
        if (now - lastTime > SCAN_GAP && buffer.length > 0) reset();
        lastTime = now;

        if (e.key === "Enter") { if (buffer.length >= MIN_LENGTH) flush(); return; }
        if (e.key.length === 1) {
            buffer += e.key;
            if (timer) clearTimeout(timer);
            timer = setTimeout(flush, SCAN_GAP + 20);
        }
    });

    return {
        enable()    { enabled = true;  updateRFIDStatus("listening"); },
        disable()   { enabled = false; updateRFIDStatus("paused");    },
        toggle()    { enabled ? RFID.disable() : RFID.enable();       },
        isEnabled() { return enabled;                                  },
    };
})();

// ═══════════════════════════════════════════════════════
// RFID SCAN → consume inventory + maybe trigger reorder
// ═══════════════════════════════════════════════════════

async function handleRFIDScan(tag) {
    setRFIDScanning(true);
    updateRFIDLastTag(tag);

    const rfidInput = document.getElementById("rfidInput");
    if (rfidInput) rfidInput.value = tag;

    try {
        const res  = await fetch(`/api/rfid/${encodeURIComponent(tag)}/scan`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ qty: 1 }),
        });
        const data = await res.json();

        if (!res.ok) {
            playBeep("error");
            const reason = data.insufficient_stock
                ? `Insufficient stock (${data.available} available)`
                : (data.message || "Tag not found");
            showRFIDResult(null, tag, null, reason);
            addRFIDLog(tag, null, reason);
            setRFIDScanning(false);
            return;
        }

        if (data.message?.includes("not found")) {
            playBeep("error");
            showRFIDResult(null, tag, null, "Tag not found");
            addRFIDLog(tag, null, "Tag not found");
            setRFIDScanning(false);
            return;
        }

        playBeep("success");
        showRFIDResult(data.item, tag, data);
        addRFIDLog(tag, data.item, data.message);

        if (data.reorder?.triggered) showReorderAlert(data.item, data.reorder);

        if (document.getElementById("view-dashboard")?.classList.contains("active")) loadDashboard();
        if (document.getElementById("view-inventory")?.classList.contains("active"))  loadInventoryView();

    } catch (err) {
        playBeep("error");
        showRFIDResult(null, tag, null, "Error: " + err.message);
        addRFIDLog(tag, null, "Error: " + err.message);
    }

    setRFIDScanning(false);
}

function showReorderAlert(item, reorder) {
    const log = document.getElementById("rfidEventLog");
    if (!log) return;
    const div = document.createElement("div");
    div.className = "rfid-reorder-alert";
    div.innerHTML = `
        <i class="ti ti-alert-triangle"></i>
        <strong>Auto-Reorder Triggered!</strong>
        ${item.part_name} — ${reorder.reorder_qty} units ordered (stock: ${reorder.current_stock})`;
    log.prepend(div);
}

async function lookupRFID() {
    const tag = document.getElementById("rfidInput").value.trim();
    if (!tag) { alert("Please scan or enter an RFID tag"); return; }
    await handleRFIDScan(tag);
}

function updateRFIDStatus(state) {
    const dot  = document.getElementById("rfidStatusDot");
    const text = document.getElementById("rfidStatusText");
    const btn  = document.getElementById("rfidToggleBtn");
    if (!dot || !text) return;
    const states = {
        listening: { cls: "rfid-dot--active",  label: "Listening for RFID tag…", btn: '<i class="ti ti-player-pause"></i> Pause'  },
        scanning:  { cls: "rfid-dot--scanning", label: "Reading tag…",            btn: null        },
        paused:    { cls: "rfid-dot--paused",   label: "Scanner paused",          btn: '<i class="ti ti-player-play"></i> Resume'  },
    };
    const s = states[state] || states.paused;
    dot.className = `rfid-dot ${s.cls}`;
    text.textContent = s.label;
    if (btn && s.btn) btn.innerHTML = s.btn;
}

function setRFIDScanning(active) {
    const panel = document.getElementById("rfidScanPanel");
    if (panel) panel.classList.toggle("rfid-panel--scanning", active);
    updateRFIDStatus(active ? "scanning" : (RFID.isEnabled() ? "listening" : "paused"));
}

function updateRFIDLastTag(tag) {
    const el = document.getElementById("rfidLastTag");
    if (el) el.textContent = tag;
}

function showRFIDResult(item, tag, data, errorReason) {
    const result = document.getElementById("rfidScanResult");
    if (!result) return;

    if (!item) {
        result.className = "rfid-result rfid-result--error";
        result.innerHTML = `
            <div class="rfid-result__icon">✗</div>
            <div class="rfid-result__body">
                <div class="rfid-result__title">${errorReason || "Tag not found"}</div>
                <div class="rfid-result__sub">${tag}</div>
            </div>`;
        return;
    }

    const low = item.quantity <= item.reorder_at;
    result.className = `rfid-result ${low ? "rfid-result--warning" : "rfid-result--success"}`;
    result.innerHTML = `
        <div class="rfid-result__icon">${low ? "⚠" : "✓"}</div>
        <div class="rfid-result__body">
            <div class="rfid-result__title">${item.part_name}</div>
            <div class="rfid-result__sub">
                ${item.material || "—"} · Consumed 1 · Stock now: <strong>${item.quantity} ${item.unit}</strong>
            </div>
            <div class="rfid-result__status">
                ${low ? stockBadge("low") : stockBadge("ok")}
                ${data?.reorder?.triggered
                    ? `<span class="badge badge-review" style="margin-left:6px"><span class="badge-dot"></span>Auto-Reorder Triggered</span>`
                    : ""}
            </div>
        </div>`;
}

function addRFIDLog(tag, item, note) {
    const log = document.getElementById("rfidEventLog");
    if (!log) return;
    const empty = log.querySelector(".rfid-log-empty");
    if (empty) empty.remove();

    const time  = new Date().toLocaleTimeString();
    const entry = document.createElement("div");
    entry.className = "rfid-log-entry";
    entry.innerHTML = `
        <span class="rfid-log__time">${time}</span>
        <span class="rfid-log__tag">${tag}</span>
        <span class="rfid-log__note">${item ? item.part_name + " · " + note : note}</span>`;
    log.prepend(entry);
    while (log.children.length > 30) log.removeChild(log.lastChild);
}

function toggleRFIDScanner() { RFID.toggle(); }

function clearRFIDLog() {
    const log = document.getElementById("rfidEventLog");
    if (log) log.innerHTML = `<div class="rfid-log-empty">Log cleared — waiting for tags…</div>`;
}

// ═══════════════════════════════════════════════════════
// BARCODE
// ═══════════════════════════════════════════════════════

async function lookupBarcode() {
    const code = document.getElementById("barcodeInput").value.trim();
    if (!code) { alert("Please scan or enter a barcode"); return; }

    try {
        const res  = await fetch(`/api/barcode/${encodeURIComponent(code)}/scan`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ qty: 1 }),
        });
        const data = await res.json();

        if (!res.ok) {
            document.getElementById("scanResult").innerHTML = `
                <div class="scan-card scan-card--low">
                    <h3>❌ ${data.insufficient_stock ? "Insufficient Stock" : "Error"}</h3>
                    <p>${data.message}</p>
                    ${data.insufficient_stock ? `<p><strong>Available:</strong> ${data.available} | <strong>Requested:</strong> ${data.requested}</p>` : ""}
                </div>`;
            return;
        }

        showScanResult(data.item || data, data);
        loadDashboard();
        if (document.getElementById("view-inventory")?.classList.contains("active")) {
            loadInventoryView();
        }
    } catch (err) {
        document.getElementById("scanResult").innerHTML =
            `<div class="scan-card"><p>Error: ${err.message}</p></div>`;
    }
}

let qrScanner = null;

function startBarcodeCamera() {
    if (typeof Html5Qrcode === "undefined") {
        alert("Camera scanner library is still loading — try again in a moment.");
        return;
    }
    const reader = document.getElementById("reader");
    reader.style.display = "block";
    if (qrScanner) qrScanner.stop().catch(() => {});
    qrScanner = new Html5Qrcode("reader");
    qrScanner.start(
        { facingMode: "environment" },
        { fps: 10, qrbox: 250 },
        (decodedText) => {
            document.getElementById("barcodeInput").value = decodedText;
            lookupBarcode();
            qrScanner.stop().then(() => { reader.style.display = "none"; });
        }
    ).catch(err => {
        reader.style.display = "none";
        alert("Could not access camera: " + err);
    });
}

function showScanResult(item, data) {
    const result = document.getElementById("scanResult");
    if (!item || item.message) {
        result.innerHTML = `
            <div class="scan-card">
                <h3>Not Found</h3>
                <p>${item ? item.message : "Unknown error"}</p>
            </div>`;
        return;
    }
    const low = item.quantity <= item.reorder_at;
    result.innerHTML = `
        <div class="scan-card ${low ? "scan-card--low" : ""}">
            <h3>📦 ${item.part_name}</h3>
            <p><strong>Material:</strong> ${item.material || "—"}</p>
            <p><strong>Stock after scan:</strong> <span class="${low ? "qty-low" : "qty-ok"}">${item.quantity} ${item.unit}</span></p>
            <p><strong>Reorder threshold:</strong> ${item.reorder_at} ${item.unit}</p>
            ${data?.reorder?.triggered
                ? `<div class="reorder-pill">🔔 Auto-Reorder triggered — ${data.reorder.reorder_qty} units ordered!</div>`
                : ""}
        </div>`;
}

document.addEventListener("DOMContentLoaded", () => {
    const rfidInput = document.getElementById("rfidInput");
    const barcodeInput = document.getElementById("barcodeInput");
    if (rfidInput) rfidInput.addEventListener("keypress", e => { if (e.key === "Enter") lookupRFID(); });
    if (barcodeInput) barcodeInput.addEventListener("keypress", e => { if (e.key === "Enter") lookupBarcode(); });
});

// ═══════════════════════════════════════════════════════════════════
// DEMAND & FORECASTING  (unified tabbed section)
// Replaces two separate, overlapping accordions ("Management to Meet
// Demand" and "Future Predictions") with one section: a shared horizon
// control for the cheap glance-able views (Forecast, Gap Analysis), plus
// Production Plan / Suppliers / Predictions as their own tabs. Predictions
// (regression across every item) stays lazy-loaded on first visit since
// it's the heaviest computation; the rest loads once when the section
// first opens.
// ═══════════════════════════════════════════════════════════════════

let demandOpen      = false;
let demandLoaded    = false;
let demandHorizon   = 30;   // days — shared by Forecast + Gap Analysis
let activeDemandTab = "forecast";

let predHorizon  = 30;
let predLoaded   = false;
let predData     = [];
let predExpanded = new Set();

// ── Section open/close ──────────────────────────────────────────────
function toggleDemandSection() {
    demandOpen = !demandOpen;
    document.getElementById("demandContent").classList.toggle("open", demandOpen);
    document.getElementById("demandHeader").classList.toggle("open", demandOpen);
    if (demandOpen && !demandLoaded) {
        loadAllDemand();
        demandLoaded = true;
    }
}

// ── Tab switching ────────────────────────────────────────────────────
function setDemandTab(tab, btn) {
    activeDemandTab = tab;
    document.querySelectorAll("#demandTabs .demand-tab").forEach(b => b.classList.remove("active"));
    if (btn) btn.classList.add("active");
    document.querySelectorAll("#demandContent .demand-tab-panel").forEach(p => p.classList.remove("active"));
    document.getElementById("panel-" + tab)?.classList.add("active");

    // The shared horizon row only makes sense for Forecast / Gap Analysis
    const horizonRow = document.getElementById("sharedHorizonRow");
    if (horizonRow) horizonRow.style.display = (tab === "forecast" || tab === "gap") ? "flex" : "none";

    if (tab === "predictions" && !predLoaded) {
        loadPredictions();
        predLoaded = true;
    }
}

// ── Horizon pickers ──────────────────────────────────────────────────
function setHorizon(days, btn) {
    demandHorizon = days;
    document.querySelectorAll("#sharedHorizonRow .horizon-btn").forEach(b => b.classList.remove("active"));
    if (btn) btn.classList.add("active");
    if (demandOpen) {
        loadForecast();
        loadGapAnalysis();
    }
}

function setPredHorizon(days, btn) {
    predHorizon = days;
    document.querySelectorAll(".pred-horizon-btn").forEach(b => b.classList.remove("active"));
    if (btn) btn.classList.add("active");
    loadPredictions();
}

// ── Master loader for the eager tabs ────────────────────────────────
async function loadAllDemand() {
    await Promise.all([
        loadForecast(),
        loadGapAnalysis(),
        loadProductionPlans(),
        loadSuppliers(),
    ]);
}

// ── ① FORECAST ───────────────────────────────────────────────────────
async function loadForecast() {
    const el = document.getElementById("forecastList");
    if (!el) return;
    el.innerHTML = `<div class="demand-loading"><i class="ti ti-loader"></i> Loading…</div>`;

    try {
        const res  = await fetch("/api/demand/forecast");
        const data = await res.json();

        if (!data.length) {
            el.innerHTML = `<div class="demand-empty"><i class="ti ti-package-off"></i>No inventory items yet.</div>`;
            updateDemandBadge([]);
            return;
        }

        el.innerHTML = data.map(item => {
            const need     = demandHorizon === 7  ? item.forecast_7d
                           : demandHorizon === 30 ? item.forecast_30d
                           : Math.round(item.avg_daily_use * demandHorizon);
            const hasCover = item.days_until_stockout !== null;
            const coverCls = !hasCover                        ? "forecast-num ok"
                           : item.days_until_stockout <  7    ? "forecast-num critical"
                           : item.days_until_stockout < 30    ? "forecast-num warn"
                           :                                    "forecast-num ok";
            const coverTxt = hasCover
                ? `${item.days_until_stockout}d until stockout`
                : "No consumption data";

            const trendCls = { rising: "trend-rising", falling: "trend-falling",
                                stable: "trend-stable",  "no data": "trend-nodata" }[item.trend] || "trend-nodata";
            const trendIcon = item.trend === "rising"  ? "↑"
                            : item.trend === "falling" ? "↓"
                            : item.trend === "stable"  ? "→" : "—";

            const maxH = Math.max(...(item.history.map(h => h.consumed)), 1);
            const bars = item.history.length
                ? item.history.slice(-14).map(h => {
                    const pct = Math.round((h.consumed / maxH) * 100);
                    return `<div class="spark-bar" style="height:${Math.max(pct,8)}%" title="${h.day}: ${h.consumed}"></div>`;
                  }).join("")
                : `<div style="color:var(--text-muted);font-size:11px;width:100%;text-align:center;padding-top:6px">No scan history</div>`;

            return `
            <div class="forecast-item">
                <div>
                    <div class="forecast-item-name">${item.part_name}</div>
                    <div class="forecast-item-sub">
                        ${item.material || "—"} &nbsp;·&nbsp;
                        <strong>${item.avg_daily_use}</strong>/day avg
                    </div>
                    <div class="sparkline-wrap">${bars}</div>
                </div>
                <div class="forecast-item-nums">
                    <div class="${coverCls}">${need} ${item.unit}</div>
                    <div style="font-size:10.5px;color:var(--text-muted)">${demandHorizon}d need</div>
                    <span class="trend-pill ${trendCls}">${trendIcon} ${item.trend}</span>
                    <div style="font-size:10.5px;color:var(--text-muted);margin-top:3px">${coverTxt}</div>
                </div>
            </div>`;
        }).join("");

        updateDemandBadge(data);
    } catch (err) {
        el.innerHTML = `<div class="demand-empty">Error: ${err.message}</div>`;
    }
}

// Drives the section header badge from the cheap avg-based forecast — but
// only until the richer regression-based Predictions data has loaded, since
// the two methods can disagree (e.g. a recent consumption spike shows up in
// the regression trend well before it moves a 30-day average) and showing
// "All clear" right above a Predictions tab that says "1 High Risk" would
// read as a contradiction rather than two different lenses on the same data.
function updateDemandBadge(forecastData) {
    if (predLoaded) return; // predictions already own the badge — see updatePredBadge
    const badge = document.getElementById("demandBadge");
    if (!badge) return;
    const critical = forecastData.filter(
        i => i.days_until_stockout !== null && i.days_until_stockout < 7
    ).length;
    if (critical > 0) {
        badge.textContent = `⚠ ${critical} at risk`;
        badge.classList.add("at-risk");
    } else {
        badge.textContent = forecastData.length ? "All clear" : "No data";
        badge.classList.remove("at-risk");
    }
}

// ── ② GAP ANALYSIS ────────────────────────────────────────────────
async function loadGapAnalysis() {
    const tbody = document.getElementById("gapTableBody");
    if (!tbody) return;
    tbody.innerHTML = `<tr><td colspan="6" class="demand-loading"><i class="ti ti-loader"></i> Analysing…</td></tr>`;

    try {
        const res  = await fetch(`/api/demand/gap-analysis?days=${demandHorizon}`);
        const data = await res.json();

        if (!data.length) {
            tbody.innerHTML = `<tr><td colspan="6" class="demand-empty">No inventory data.</td></tr>`;
            return;
        }

        tbody.innerHTML = data.map(item => {
            const gapCls  = item.gap < 0               ? "gap-num gap-neg"
                          : item.gap < item.reorder_at  ? "gap-num gap-warn"
                          :                               "gap-num gap-pos";
            const gapPfx  = item.gap >= 0 ? "+" : "";
            const covTxt  = item.coverage_days !== null ? `${item.coverage_days}d` : "∞";
            const badgeCls = { ok: "gap-status-badge gap-ok",
                                warning: "gap-status-badge gap-warning",
                                critical: "gap-status-badge gap-critical" }[item.status];
            const icon    = { ok: "✓", warning: "⚠", critical: "✗" }[item.status];
            const pending = item.pending_orders > 0
                ? `<span style="color:var(--brand);font-size:10px;margin-left:4px">+${item.pending_orders} on order</span>` : "";

            return `
            <tr>
                <td>
                    <div style="font-weight:600;color:var(--text-primary);font-size:12.5px">${item.part_name}</div>
                    <div style="font-size:10.5px;color:var(--text-muted)">${item.material || "—"}</div>
                </td>
                <td class="gap-num">${item.current_stock} <span style="font-size:10px;color:var(--text-muted)">${item.unit}</span>${pending}</td>
                <td class="gap-num">${item.projected_demand} <span style="font-size:10px;color:var(--text-muted)">${item.unit}</span></td>
                <td class="${gapCls}">${gapPfx}${item.gap}</td>
                <td>
                    <div class="coverage-wrap">
                        <span class="coverage-days ${item.coverage_days !== null && item.coverage_days < 14 ? 'gap-warn' : ''}">${covTxt}</span>
                        ${item.supplier ? `<span style="font-size:10px;color:var(--text-muted)">${item.supplier.lead_days}d lead</span>` : ""}
                    </div>
                </td>
                <td><span class="${badgeCls}">${icon} ${item.status}</span></td>
            </tr>`;
        }).join("");
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="6" class="demand-empty">Error: ${err.message}</td></tr>`;
    }
}

// ── ③ PRODUCTION PLANNING ─────────────────────────────────────────
async function loadProductionPlans() {
    const el = document.getElementById("prodPlanList");
    if (!el) return;
    el.innerHTML = `<div class="demand-loading"><i class="ti ti-loader"></i> Loading…</div>`;

    try {
        const res  = await fetch("/api/demand/production-plan");
        const data = await res.json();

        if (!data.length) {
            el.innerHTML = `<div class="demand-empty"><i class="ti ti-calendar-off"></i>No production plans yet.</div>`;
            return;
        }

        el.innerHTML = data.map(p => {
            const dateRange = [p.start_date, p.end_date].filter(Boolean).join(" → ") || "No dates set";
            return `
            <div class="plan-item" id="plan-${p.id}">
                <div>
                    <div class="plan-item-name">${p.part_name}</div>
                    <div class="plan-item-dates">${dateRange}${p.notes ? " · " + p.notes : ""}</div>
                </div>
                <div class="plan-item-qty">${p.target_qty} ${p.unit || "pcs"}</div>
                <select class="plan-status-sel" onchange="updatePlanStatus(${p.id}, this.value)">
                    ${["Planned","In Progress","Complete","On Hold"].map(s =>
                        `<option ${p.status === s ? "selected" : ""}>${s}</option>`
                    ).join("")}
                </select>
                <button class="plan-del-btn" onclick="deletePlan(${p.id})" title="Delete">
                    <i class="ti ti-trash"></i>
                </button>
            </div>`;
        }).join("");
    } catch (err) {
        el.innerHTML = `<div class="demand-empty">Error: ${err.message}</div>`;
    }
}

async function addProductionPlan() {
    const part  = document.getElementById("pp-part").value.trim();
    const qty   = document.getElementById("pp-qty").value.trim();
    const msg   = document.getElementById("ppMsg");

    if (!part || !qty) { msg.style.color = "var(--critical-text)"; msg.textContent = "❌ Part & qty required"; return; }

    try {
        const res  = await fetch("/api/demand/production-plan", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                part_name:  part,
                target_qty: parseInt(qty),
                start_date: document.getElementById("pp-start").value,
                end_date:   document.getElementById("pp-end").value,
            }),
        });
        const data = await res.json();
        msg.style.color   = res.ok ? "var(--good-text)" : "var(--critical-text)";
        msg.textContent   = data.message;
        if (res.ok) {
            ["pp-part","pp-qty","pp-start","pp-end"].forEach(id => { document.getElementById(id).value = ""; });
            loadProductionPlans();
        }
    } catch (err) {
        msg.style.color = "var(--critical-text)"; msg.textContent = "Error: " + err.message;
    }
}

async function updatePlanStatus(id, status) {
    await fetch(`/api/demand/production-plan/${id}`, {
        method:  "PATCH",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ status }),
    });
}

async function deletePlan(id) {
    if (!confirm("Delete this production plan?")) return;
    await fetch(`/api/demand/production-plan/${id}`, { method: "DELETE" });
    loadProductionPlans();
}

// ── ④ SUPPLIER LEAD TIMES ─────────────────────────────────────────
async function loadSuppliers() {
    const el = document.getElementById("supplierList");
    if (!el) return;
    el.innerHTML = `<div class="demand-loading"><i class="ti ti-loader"></i> Loading…</div>`;

    try {
        const res  = await fetch("/api/demand/suppliers");
        const data = await res.json();

        if (!data.length) {
            el.innerHTML = `<div class="demand-empty"><i class="ti ti-truck-off"></i>No suppliers yet.</div>`;
            return;
        }

        el.innerHTML = data.map(s => {
            const leadCls = s.lead_days <= 5  ? "lead-fast"
                          : s.lead_days <= 14 ? "lead-medium"
                          :                     "lead-slow";
            const relPct  = Math.min(100, s.reliability);
            const relCol  = relPct >= 90 ? "var(--good)"
                          : relPct >= 70 ? "var(--warn)"
                          :                "var(--critical)";
            return `
            <div class="supplier-item">
                <div>
                    <div class="supplier-name">${s.name}</div>
                    <div class="supplier-part">${s.part_name || s.inv_part_name || "—"}</div>
                    <div class="reliability-bar-wrap" title="${relPct}% reliability">
                        <div class="reliability-bar" style="width:${relPct}%;background:${relCol}"></div>
                    </div>
                    <div style="font-size:10px;color:var(--text-muted);margin-top:2px">${relPct}% reliability</div>
                </div>
                <span class="lead-pill ${leadCls}">
                    <i class="ti ti-clock"></i> ${s.lead_days}d lead
                </span>
                <span class="lead-pill lead-neutral" title="Unit cost">
                    <i class="ti ti-currency-dollar"></i> $${Number(s.unit_cost || 0).toFixed(2)}/unit
                </span>
                <span style="font-size:11px;color:var(--text-muted)">${s.contact || ""}</span>
                <button class="plan-del-btn" onclick="deleteSupplier(${s.id})" title="Remove">
                    <i class="ti ti-trash"></i>
                </button>
            </div>`;
        }).join("");
    } catch (err) {
        el.innerHTML = `<div class="demand-empty">Error: ${err.message}</div>`;
    }
}

async function addSupplier() {
    const name = document.getElementById("sup-name").value.trim();
    const msg  = document.getElementById("supMsg");

    if (!name) { msg.style.color = "var(--critical-text)"; msg.textContent = "❌ Supplier name required"; return; }

    try {
        const res  = await fetch("/api/demand/suppliers", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                name,
                part_name:   document.getElementById("sup-part").value.trim(),
                lead_days:   parseInt(document.getElementById("sup-lead").value) || 7,
                reliability: parseInt(document.getElementById("sup-rel").value)  || 90,
                unit_cost:   parseFloat(document.getElementById("sup-cost").value) || 0,
            }),
        });
        const data = await res.json();
        msg.style.color = res.ok ? "var(--good-text)" : "var(--critical-text)";
        msg.textContent = data.message;
        if (res.ok) {
            ["sup-name","sup-part"].forEach(id => { document.getElementById(id).value = ""; });
            document.getElementById("sup-lead").value = "7";
            document.getElementById("sup-rel").value  = "90";
            document.getElementById("sup-cost").value = "";
            loadSuppliers();
        }
    } catch (err) {
        msg.style.color = "var(--critical-text)"; msg.textContent = "Error: " + err.message;
    }
}

async function deleteSupplier(id) {
    if (!confirm("Remove this supplier?")) return;
    await fetch(`/api/demand/suppliers/${id}`, { method: "DELETE" });
    loadSuppliers();
}

// ── ⑤ PREDICTIONS ─────────────────────────────────────────────────
async function loadPredictions() {
    const grid = document.getElementById("predGrid");
    if (!grid) return;

    grid.innerHTML = `
        <div class="pred-loading-state">
            <div class="loader-ring"></div>
            <span>Running regression models…</span>
        </div>`;

    try {
        const res  = await fetch(`/api/demand/predictions?days=${predHorizon}`);
        predData   = await res.json();

        if (!predData.length) {
            grid.innerHTML = `
                <div class="pred-empty">
                    <i class="ti ti-chart-bar-off"></i>
                    Add inventory items to enable predictions.
                </div>`;
            updatePredBadge([]);
            return;
        }

        updatePredBadge(predData);
        renderPredGrid(predData);

    } catch (err) {
        grid.innerHTML = `<div class="pred-empty" style="color:var(--critical-text)">Error: ${err.message}</div>`;
    }
}

function renderPredGrid(items) {
    const grid = document.getElementById("predGrid");
    if (!grid) return;

    const critical = items.filter(i => i.reorder_rec.urgency === "critical").length;
    const high     = items.filter(i => i.reorder_rec.urgency === "high").length;
    const noData   = items.filter(i => !i.has_data).length;

    grid.innerHTML = `
        <div class="pred-summary-row">
            <div class="pred-summary-chip pred-chip-critical">
                <i class="ti ti-alert-circle"></i>
                <strong>${critical}</strong> Critical
            </div>
            <div class="pred-summary-chip pred-chip-high">
                <i class="ti ti-alert-triangle"></i>
                <strong>${high}</strong> High Risk
            </div>
            <div class="pred-summary-chip pred-chip-ok">
                <i class="ti ti-circle-check"></i>
                <strong>${items.length - critical - high}</strong> Stable
            </div>
            <div class="pred-summary-chip pred-chip-nodata" style="${noData ? '' : 'opacity:.45'}">
                <i class="ti ti-database-off"></i>
                <strong>${noData}</strong> No scan data
            </div>
        </div>
        <div class="pred-cards" id="predCards">
            ${items.map(item => renderPredCard(item)).join("")}
        </div>`;
}

function renderPredCard(item) {
    const urg   = item.reorder_rec.urgency;
    const urgCls = { critical: "pred-urg-critical", high: "pred-urg-high",
                     medium: "pred-urg-medium",    low: "pred-urg-low" }[urg] || "pred-urg-low";

    const urgLabel  = { critical: "🔴 Critical", high: "🟠 High Risk",
                        medium: "🟡 Watch",     low: "🟢 Stable" }[urg] || "🟢 Stable";

    const stockCol  = item.current_stock === 0             ? "var(--critical-text)"
                    : item.current_stock <= item.reorder_at ? "var(--warn)"
                    : "var(--good-text)";

    const fk      = predHorizon <= 7 ? "7d" : predHorizon <= 14 ? "14d" : predHorizon <= 30 ? "30d" : predHorizon <= 60 ? "60d" : "90d";
    const fc      = item.forecast[fk] || item.forecast["30d"];
    const maxVal  = Math.max(item.current_stock, fc.hi, 1);
    const stockW  = Math.round((item.current_stock / maxVal) * 100);
    const demandW = Math.round((fc.demand / maxVal) * 100);
    const hiW     = Math.round((fc.hi / maxVal) * 100);

    const stockoutTxt = item.stockout.days
        ? `<span class="${item.stockout.days <= 14 ? 'pred-stockout-warn' : 'pred-stockout-ok'}">
               Stockout in <strong>${item.stockout.days}d</strong>
               (${item.stockout.days_lo || "?"}–${item.stockout.days_hi || "∞"}d range)
           </span>`
        : `<span class="pred-stockout-ok">No stockout within ${item.has_data ? "365d" : "—"}</span>`;

    const trendIcon = { rising: "↑", falling: "↓", stable: "→",
                        accelerating: "⬆", decelerating: "⬇", "no data": "—" }[item.trend] || "—";
    const trendCls  = { rising: "pred-trend-up", accelerating: "pred-trend-up-fast",
                        falling: "pred-trend-down", decelerating: "pred-trend-down-fast",
                        stable: "pred-trend-stable", "no data": "pred-trend-nodata" }[item.trend] || "pred-trend-nodata";

    const confBar  = item.has_data
        ? `<div class="pred-conf-bar-wrap" title="${item.confidence}% model confidence (R²)">
               <div class="pred-conf-bar" style="width:${item.confidence}%;background:${item.confidence >= 70 ? 'var(--good)' : item.confidence >= 40 ? 'var(--warn)' : 'var(--critical)'}"></div>
           </div>`
        : `<span style="color:var(--text-muted);font-size:10.5px">No scan history</span>`;

    const isExpanded = predExpanded.has(item.id);
    const chartHtml  = isExpanded ? buildMiniChart(item) : "";

    return `
    <div class="pred-card ${urgCls}" id="pred-card-${item.id}">

        <div class="pred-card-head" onclick="togglePredCard(${item.id})">
            <div class="pred-card-head-left">
                <span class="pred-urg-badge ${urgCls}">${urgLabel}</span>
                <div class="pred-card-name">${item.part_name}</div>
                <div class="pred-card-sub">${item.material || "—"} · ${item.unit}</div>
            </div>
            <div class="pred-card-head-right">
                <div class="pred-stock-display">
                    <span class="pred-stock-num" style="color:${stockCol}">${item.current_stock}</span>
                    <span class="pred-stock-unit">${item.unit} in stock</span>
                </div>
                <i class="ti ti-chevron-${isExpanded ? 'up' : 'down'} pred-chevron-icon"></i>
            </div>
        </div>

        <div class="pred-card-stats">
            <div class="pred-stat">
                <div class="pred-stat-label">Demand (${predHorizon}d)</div>
                <div class="pred-stat-val">${fc.demand} <span class="pred-stat-range">(${fc.lo}–${fc.hi})</span></div>
            </div>
            <div class="pred-stat">
                <div class="pred-stat-label">Trend</div>
                <div class="pred-stat-val ${trendCls}">${trendIcon} ${item.trend} <span class="pred-stat-range">${item.velocity}</span></div>
            </div>
            <div class="pred-stat">
                <div class="pred-stat-label">Stockout</div>
                <div class="pred-stat-val">${stockoutTxt}</div>
            </div>
            <div class="pred-stat">
                <div class="pred-stat-label">Model Fit</div>
                <div class="pred-stat-val pred-conf-wrap">
                    ${confBar}
                    <span style="font-size:10.5px;color:var(--text-muted)">${item.confidence}%</span>
                </div>
            </div>
        </div>

        <div class="pred-bar-section">
            <div class="pred-bar-labels">
                <span>Current Stock</span>
                <span>${predHorizon}d Forecast</span>
            </div>
            <div class="pred-bar-track">
                <div class="pred-bar-fill pred-bar-stock" style="width:${stockW}%" title="Stock: ${item.current_stock}"></div>
            </div>
            <div class="pred-bar-track">
                <div class="pred-bar-fill pred-bar-demand-hi" style="width:${hiW}%" title="High estimate: ${fc.hi}"></div>
                <div class="pred-bar-fill pred-bar-demand" style="width:${demandW}%" title="Predicted demand: ${fc.demand}"></div>
            </div>
        </div>

        <div class="pred-card-detail ${isExpanded ? 'open' : ''}">

            <div class="pred-chart-section">
                <div class="pred-chart-title">${predHorizon >= 30 ? 30 : predHorizon}-Day Stock Forecast</div>
                ${chartHtml}
            </div>

            <div class="pred-horizons">
                ${["7d","14d","30d","60d","90d"].map(hk => {
                    const f = item.forecast[hk];
                    const cover = f.demand > 0 ? Math.round((item.current_stock / f.demand) * 100) : 999;
                    const coverCls = cover >= 100 ? "pred-cover-ok" : cover >= 70 ? "pred-cover-warn" : "pred-cover-bad";
                    return `
                    <div class="pred-horizon-row">
                        <span class="pred-horizon-label">${hk}</span>
                        <span class="pred-horizon-demand">${f.demand} ${item.unit}</span>
                        <span class="pred-horizon-range">(${f.lo}–${f.hi})</span>
                        <span class="pred-horizon-cover ${coverCls}">${Math.min(999, cover)}% covered</span>
                    </div>`;
                }).join("")}
            </div>

            ${item.reorder_rec.suggested_qty > 0 || urg === "critical" || urg === "high" ? `
            <div class="pred-reorder-box pred-reorder-${urg}">
                <div class="pred-reorder-title">
                    <i class="ti ti-truck"></i>
                    Reorder Recommendation
                </div>
                <div class="pred-reorder-grid">
                    <div><span class="pred-rlabel">Suggested Qty</span><span class="pred-rval">${item.reorder_rec.suggested_qty} ${item.unit}</span></div>
                    <div><span class="pred-rlabel">Reorder Point</span><span class="pred-rval">${item.reorder_rec.reorder_point} ${item.unit}</span></div>
                    <div><span class="pred-rlabel">Safety Stock</span><span class="pred-rval">${item.reorder_rec.safety_stock} ${item.unit}</span></div>
                    <div><span class="pred-rlabel">Lead Time</span><span class="pred-rval">${item.reorder_rec.lead_days} days</span></div>
                    ${item.reorder_rec.supplier ? `<div style="grid-column:span 2"><span class="pred-rlabel">Supplier</span><span class="pred-rval">${item.reorder_rec.supplier}</span></div>` : ""}
                </div>
                <button class="pred-order-btn" onclick="quickOrder(${item.id}, '${item.part_name.replace(/'/g,"\\'")}', ${item.current_stock}); event.stopPropagation()">
                    <i class="ti ti-shopping-cart"></i>
                    Place Order Now
                </button>
            </div>` : ""}

        </div>
    </div>`;
}

function buildMiniChart(item) {
    const days = item.daily_forecast;
    if (!days || !days.length) return `<div class="pred-chart-nodata">No forecast data available</div>`;

    const W = 440, H = 90, PAD = 6;
    const maxStock = Math.max(item.current_stock, ...days.map(d => d.stock_eod), 1);
    const minStock = 0;

    const toX = i => PAD + (i / days.length) * (W - PAD * 2);
    const toY = v => H - PAD - ((v - minStock) / (maxStock - minStock)) * (H - PAD * 2);

    const stockPts   = days.map((d, i) => `${toX(i)},${toY(d.stock_eod)}`).join(" ");
    const stockArea  = `${toX(0)},${toY(0)} ` + stockPts + ` ${toX(days.length - 1)},${toY(0)}`;

    const hiPts  = days.map((d, i) => `${toX(i)},${toY(Math.max(0, d.stock_eod - d.consume_hi + d.consumption))}`).join(" ");
    const loPts  = [...days].reverse().map((d, i) => `${toX(days.length - 1 - i)},${toY(Math.max(0, d.stock_eod - d.consume_lo + d.consumption))}`).join(" ");

    const rY = toY(item.reorder_at);

    const stockoutDay = item.stockout.days;
    const stockoutX   = stockoutDay && stockoutDay <= 30 ? toX(stockoutDay) : null;

    return `
    <div class="pred-chart-wrap">
        <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="pred-svg-chart">
            <defs>
                <linearGradient id="stockGrad-${item.id}" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stop-color="var(--brand)" stop-opacity="0.35"/>
                    <stop offset="100%" stop-color="var(--brand)" stop-opacity="0.03"/>
                </linearGradient>
            </defs>

            <polygon points="${hiPts} ${loPts}" fill="var(--brand)" opacity="0.09"/>
            <polygon points="${stockArea}" fill="url(#stockGrad-${item.id})"/>
            <polyline points="${stockPts}" fill="none" stroke="var(--brand)" stroke-width="2" stroke-linejoin="round"/>

            <line x1="${PAD}" y1="${rY}" x2="${W - PAD}" y2="${rY}"
                  stroke="var(--warn)" stroke-width="1" stroke-dasharray="4,3" opacity="0.8"/>
            <text x="${W - PAD - 2}" y="${rY - 3}" font-size="8" fill="var(--warn)" text-anchor="end" opacity="0.9">reorder</text>

            ${stockoutX ? `
            <line x1="${stockoutX}" y1="${PAD}" x2="${stockoutX}" y2="${H - PAD}"
                  stroke="var(--critical-text)" stroke-width="1.5" stroke-dasharray="3,3" opacity="0.9"/>
            <text x="${stockoutX + 3}" y="${PAD + 9}" font-size="8" fill="var(--critical-text)">stockout</text>
            ` : ""}

            <line x1="${PAD}" y1="${PAD}" x2="${PAD}" y2="${H - PAD}"
                  stroke="var(--text-muted)" stroke-width="1" opacity="0.3"/>
        </svg>

        <div class="pred-chart-legend">
            <span><svg width="12" height="4"><line x1="0" y1="2" x2="12" y2="2" stroke="var(--brand)" stroke-width="2"/></svg> Stock projection</span>
            <span><svg width="12" height="4"><line x1="0" y1="2" x2="12" y2="2" stroke="var(--warn)" stroke-width="1.5" stroke-dasharray="4,2"/></svg> Reorder threshold</span>
            ${stockoutX ? `<span><svg width="12" height="4"><line x1="0" y1="2" x2="12" y2="2" stroke="var(--critical-text)" stroke-width="1.5" stroke-dasharray="3,2"/></svg> Stockout</span>` : ""}
        </div>
    </div>`;
}

function togglePredCard(id) {
    if (predExpanded.has(id)) predExpanded.delete(id);
    else predExpanded.add(id);
    const item = predData.find(i => i.id === id);
    if (!item) return;
    const card = document.getElementById(`pred-card-${id}`);
    if (!card) return;
    card.outerHTML = renderPredCard(item);
}

function updatePredBadge(items) {
    const critical = items.filter(i => i.reorder_rec?.urgency === "critical").length;
    const high     = items.filter(i => i.reorder_rec?.urgency === "high").length;
    const total    = critical + high;

    const tabCount = document.getElementById("predTabCount");
    if (tabCount) {
        if (total > 0) {
            tabCount.style.display = "inline-flex";
            tabCount.textContent = total;
        } else {
            tabCount.style.display = "none";
        }
    }

    // Predictions are the richer, regression-based signal — once loaded they
    // take over the section header badge from the plain-average forecast.
    const badge = document.getElementById("demandBadge");
    if (!badge) return;
    if (total > 0) {
        badge.textContent = `⚠ ${total} at risk`;
        badge.classList.add("at-risk");
    } else {
        badge.textContent = items.length ? "All clear" : "No data";
        badge.classList.remove("at-risk");
    }
}

// ═══════════════════════════════════════════════════════
// AUTH — sessions are optional for most of the app; only needed for
// approving/paying purchase orders and admin actions (see auth.py).
// ═══════════════════════════════════════════════════════

let authState = { authenticated: false, username: null, role: null };

async function checkAuth() {
    try {
        const res = await fetch("/api/auth/me");
        authState = await res.json();
    } catch (err) {
        authState = { authenticated: false };
    }
    renderAuthWidget();
    return authState;
}

function renderAuthWidget() {
    const loginBtn = document.getElementById("authLoginBtn");
    const userRow  = document.getElementById("authUserRow");
    if (!loginBtn || !userRow) return;

    if (authState.authenticated) {
        loginBtn.style.display = "none";
        userRow.style.display = "flex";
        document.getElementById("authUserName").textContent = authState.username;
        // When an admin is acting as a lower role, say so — otherwise the
        // sidebar just reads "viewer" with no hint of how to get back.
        document.getElementById("authUserRole").textContent = authState.acting_as
            ? authState.role + " · acting"
            : authState.role;
    } else {
        loginBtn.style.display = "flex";
        userRow.style.display = "none";
    }

    const accountSection = document.getElementById("accountSection");
    if (accountSection) accountSection.style.display = authState.authenticated ? "block" : "none";
    const navAdmin = document.getElementById("navAdmin");
    if (navAdmin) navAdmin.style.display = (authState.role === "admin") ? "flex" : "none";

    // Logging out (or losing admin) while sitting on a now-hidden view —
    // bounce back somewhere sane instead of stranding the user on a blank page.
    const onProfile = document.getElementById("view-profile")?.classList.contains("active");
    const onAdmin   = document.getElementById("view-admin")?.classList.contains("active");
    if (!authState.authenticated && (onProfile || onAdmin)) showView("dashboard");
    else if (authState.role !== "admin" && onAdmin) showView("dashboard");
}

function openLoginModal() {
    const modal = document.getElementById("loginModal");
    if (!modal) return;
    modal.style.display = "flex";
    document.getElementById("login-username").value = "";
    document.getElementById("login-password").value = "";
    document.getElementById("loginMsg").textContent = "";
    document.getElementById("login-username")?.focus();
}

function closeLoginModal() {
    const modal = document.getElementById("loginModal");
    if (modal) modal.style.display = "none";
}

async function doLogin() {
    const username = document.getElementById("login-username").value.trim();
    const password = document.getElementById("login-password").value;
    const msg = document.getElementById("loginMsg");

    if (!username || !password) {
        msg.style.color = "var(--critical-text)";
        msg.textContent = "Enter username & password";
        return;
    }
    try {
        const res  = await fetch("/api/auth/login", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ username, password }),
        });
        const data = await res.json();
        if (res.ok) {
            closeLoginModal();
            await checkAuth();
            if (document.getElementById("view-purchasing")?.classList.contains("active")) loadPurchasing();
        } else {
            msg.style.color = "var(--critical-text)";
            msg.textContent = data.message;
        }
    } catch (err) {
        msg.style.color = "var(--critical-text)";
        msg.textContent = "Error: " + err.message;
    }
}

async function logout() {
    await fetch("/api/auth/logout", { method: "POST" });
    await checkAuth();
    if (document.getElementById("view-purchasing")?.classList.contains("active")) loadPurchasing();
}

// ── SIGN UP — public self-service account creation (always role=viewer;
// the server enforces this regardless of what's sent) ──────────────────
function openSignupModal() {
    const modal = document.getElementById("signupModal");
    if (!modal) return;
    modal.style.display = "flex";
    document.getElementById("signup-username").value = "";
    document.getElementById("signup-password").value = "";
    document.getElementById("signup-password-confirm").value = "";
    document.getElementById("signupMsg").textContent = "";
    document.getElementById("signup-username")?.focus();
}

function closeSignupModal() {
    const modal = document.getElementById("signupModal");
    if (modal) modal.style.display = "none";
}

async function doSignup() {
    const username = document.getElementById("signup-username").value.trim();
    const password = document.getElementById("signup-password").value;
    const confirmPw = document.getElementById("signup-password-confirm").value;
    const msg = document.getElementById("signupMsg");

    if (!username || !password) {
        msg.style.color = "var(--critical-text)"; msg.textContent = "Enter a username & password";
        return;
    }
    if (password !== confirmPw) {
        msg.style.color = "var(--critical-text)"; msg.textContent = "❌ Passwords don't match";
        return;
    }
    try {
        const res  = await fetch("/api/auth/register", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ username, password }),
        });
        const data = await res.json();
        if (res.ok) {
            closeSignupModal();
            await checkAuth();
            if (document.getElementById("view-purchasing")?.classList.contains("active")) loadPurchasing();
        } else {
            msg.style.color = "var(--critical-text)"; msg.textContent = data.message;
        }
    } catch (err) {
        msg.style.color = "var(--critical-text)"; msg.textContent = "Error: " + err.message;
    }
}

// ── PROFILE — any logged-in user's own account page ─────────────────
async function loadProfile() {
    try {
        const res  = await fetch("/api/auth/me");
        const data = await res.json();
        if (!data.authenticated) { showView("dashboard"); openLoginModal(); return; }
        document.getElementById("profileUsername").textContent  = data.username;
        document.getElementById("profileRole").textContent      = data.acting_as
            ? `${data.role} (acting — account is ${data.account_role})`
            : data.role;
        document.getElementById("profileCreated").textContent   = (data.created_at || "").slice(0, 10) || "—";
        document.getElementById("profileLastLogin").textContent = data.last_login
            ? data.last_login.replace("T", " ").slice(0, 16)
            : "This is your first login";
        document.getElementById("pw-old").value = "";
        document.getElementById("pw-new").value = "";
        document.getElementById("changePwMsg").textContent = "";
        renderRoleSwitch(data);
    } catch (err) {
        console.error("Profile load error:", err);
    }
}

// ── ROLE SWITCHER — admin accounts only ─────────────────────────────
// Lets an admin act as operator or viewer without logging out, so the
// difference between the roles can be demonstrated in one session. The
// server decides whether this is allowed (it checks the role stored in the
// database, not the session), so hiding the block here is convenience, not
// the security boundary.
function renderRoleSwitch(data) {
    const block = document.getElementById("roleSwitchBlock");
    if (!block) return;

    if (!data.can_switch_role) { block.style.display = "none"; return; }
    block.style.display = "block";

    ["admin", "operator", "viewer"].forEach((r) => {
        const btn = document.getElementById("roleBtn-" + r);
        if (!btn) return;
        const active = data.role === r;
        btn.classList.toggle("btn-primary", active);
        btn.classList.toggle("btn-secondary", !active);
        btn.disabled = active;
        btn.style.opacity = active ? "1" : "";
    });

    const msg = document.getElementById("roleSwitchMsg");
    if (msg) {
        msg.style.color = data.acting_as ? "var(--warning-text, #9a6700)" : "var(--muted-text, #6b7280)";
        msg.textContent = data.acting_as
            ? `Acting as ${data.role}. Admin pages and actions are blocked until you switch back.`
            : "You are using full admin access.";
    }
}

async function switchRole(role) {
    const msg = document.getElementById("roleSwitchMsg");
    try {
        const res  = await fetch("/api/auth/switch-role", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ role }),
        });
        const data = await res.json();
        if (!res.ok) {
            if (msg) { msg.style.color = "var(--critical-text)"; msg.textContent = data.message || "Could not switch role"; }
            return;
        }
        // Refresh the nav (Admin tab appears/disappears) and the profile page.
        await checkAuth();
        await loadProfile();
    } catch (err) {
        console.error("Role switch error:", err);
        if (msg) { msg.style.color = "var(--critical-text)"; msg.textContent = "Could not reach the server"; }
    }
}

async function changeMyPassword() {
    const old_password = document.getElementById("pw-old").value;
    const new_password = document.getElementById("pw-new").value;
    const msg = document.getElementById("changePwMsg");
    try {
        const res  = await fetch("/api/auth/change-password", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ old_password, new_password }),
        });
        const data = await res.json();
        msg.style.color = res.ok ? "var(--good-text)" : "var(--critical-text)";
        msg.textContent = data.message;
        if (res.ok) {
            document.getElementById("pw-old").value = "";
            document.getElementById("pw-new").value = "";
        }
    } catch (err) {
        msg.style.color = "var(--critical-text)"; msg.textContent = "Error: " + err.message;
    }
}

// ═══════════════════════════════════════════════════════
// PURCHASING — purchase orders TO suppliers (distinct from the customer/
// production `orders` table). Covers procurement automation + payments.
// ═══════════════════════════════════════════════════════

let allPurchaseOrders = [];

async function loadPurchasing() {
    try {
        const [poRes, invRes, payRes] = await Promise.all([
            fetch("/api/purchase-orders"),
            fetch("/api/inventory"),
            fetch("/api/payments/status"),
        ]);
        allPurchaseOrders = await poRes.json();
        const inv        = await invRes.json();
        const payStatus  = await payRes.json();

        const sel = document.getElementById("po-inventory");
        if (sel) {
            const current = sel.value;
            sel.innerHTML = '<option value="">Select item…</option>' +
                inv.map(i => `<option value="${i.id}">${escapeHtml(i.part_name)} (stock: ${i.quantity})</option>`).join("");
            if (current) sel.value = current;
        }

        renderPOTable(allPurchaseOrders);

        const navCount = document.getElementById("poNavCount");
        const needsApproval = allPurchaseOrders.filter(p => p.status === "pending_approval").length;
        if (navCount) {
            if (needsApproval > 0) { navCount.style.display = "inline-flex"; navCount.textContent = needsApproval; }
            else navCount.style.display = "none";
        }

        const pillDot  = document.querySelector("#paymentModePill .dot");
        const pillText = document.getElementById("paymentModeText");
        if (pillDot && pillText) {
            if (payStatus.configured) {
                pillDot.className = "dot green";
                pillText.textContent = `Stripe (${payStatus.mode})`;
            } else {
                pillDot.className = "dot amber";
                pillText.textContent = "Demo payments — no Stripe key set";
            }
        }

        loadPaymentHistory();
    } catch (err) {
        console.error("Purchasing load error:", err);
    }
}

function poStatusBadge(status) {
    const map = {
        pending_approval:   ["badge-pending",   "Needs Approval"],
        approved:           ["badge-received",  "Approved"],
        sent:               ["badge-sent",      "Sent"],
        partially_received: ["badge-review",    "Partial"],
        received:           ["badge-accepted",  "Received"],
        cancelled:          ["badge-cancelled", "Cancelled"],
    };
    const [cls, label] = map[status] || ["badge-received", status];
    return `<span class="badge ${cls}"><span class="badge-dot"></span>${label}</span>`;
}

function renderPOTable(pos) {
    const tbody = document.getElementById("poTableBody");
    if (!tbody) return;
    if (!pos.length) {
        tbody.innerHTML = `<tr><td colspan="7" class="table-empty">No purchase orders yet — create one above, or let auto-reorder generate one when stock runs low.</td></tr>`;
        return;
    }
    tbody.innerHTML = pos.map(po => {
        const canApprove = po.status === "pending_approval";
        const canSend    = po.status === "approved";
        const canReceive = po.status === "sent" || po.status === "partially_received";
        const canPay     = ["approved", "sent", "partially_received", "received"].includes(po.status);
        const canCancel  = po.status !== "received" && po.status !== "cancelled";
        const remaining  = po.quantity - po.received_qty;

        let actions = "";
        if (canApprove) actions += `<button class="po-action-btn primary" onclick="approvePO(${po.id})" title="Requires admin/operator login"><i class="ti ti-check"></i> Approve</button>`;
        if (canSend)    actions += `<button class="po-action-btn" onclick="sendPO(${po.id})"><i class="ti ti-send"></i> Send</button>`;
        if (canReceive) actions += `<button class="po-action-btn good" onclick="receivePO(${po.id}, ${remaining})"><i class="ti ti-package"></i> Receive</button>`;
        if (canPay)     actions += `<button class="po-action-btn primary" onclick="payPurchaseOrder(${po.id}, ${po.total_cost || 0})" title="Requires admin/operator login"><i class="ti ti-credit-card"></i> Pay $${(po.total_cost || 0).toFixed(2)}</button>`;
        if (canCancel)  actions += `<button class="po-action-btn danger" onclick="cancelPO(${po.id})" title="Requires admin/operator login"><i class="ti ti-x"></i> Cancel</button>`;
        if (!actions) actions = `<span style="color:var(--text-muted);font-size:11.5px">—</span>`;

        return `<tr data-po-status="${po.status}">
            <td class="order-id-cell">#${po.id}</td>
            <td class="part-name-cell">${escapeHtml(po.part_name)}${po.auto_generated ? ' <i class="ti ti-robot" title="Auto-generated by reorder trigger" style="color:var(--text-muted);vertical-align:-2px" aria-hidden="true"></i>' : ""}</td>
            <td>${po.received_qty}/${po.quantity}</td>
            <td>${po.supplier_name ? escapeHtml(po.supplier_name) : "—"}</td>
            <td class="mono">$${(po.total_cost || 0).toFixed(2)}</td>
            <td>${poStatusBadge(po.status)}</td>
            <td><div class="po-actions">${actions}</div></td>
        </tr>`;
    }).join("");
}

function filterPOTable(status, btn) {
    document.querySelectorAll("#poFilterGroup .filter-btn").forEach(b => b.classList.remove("active"));
    if (btn) btn.classList.add("active");
    document.querySelectorAll("#poTableBody tr").forEach(row => {
        row.style.display = (status === "all" || row.dataset.poStatus === status) ? "" : "none";
    });
}

async function loadSupplierCompare() {
    const invId = document.getElementById("po-inventory")?.value;
    const el = document.getElementById("poSupplierCompare");
    if (!el) return;
    if (!invId) { el.innerHTML = ""; return; }

    el.innerHTML = `<div class="demand-loading"><i class="ti ti-loader"></i> Comparing suppliers…</div>`;
    try {
        const res  = await fetch(`/api/suppliers/compare?inventory_id=${invId}`);
        const data = await res.json();
        if (!data.length) {
            el.innerHTML = `<div class="compare-empty"><i class="ti ti-truck-off"></i> No supplier on file for this part yet — add one in the Suppliers tab (Dashboard → Demand &amp; Forecasting) first.</div>`;
            return;
        }
        el.innerHTML = data.map((s, idx) => `
            <div class="compare-row" onclick="createPOWithSupplier(${s.id})" title="Click to create a PO with this supplier">
                <span class="compare-name">${idx === 0 ? "⭐ " : ""}${escapeHtml(s.name)}</span>
                <span class="compare-stat"><i class="ti ti-currency-dollar"></i> $${(s.unit_cost || 0).toFixed(2)}/unit</span>
                <span class="compare-stat"><i class="ti ti-clock"></i> ${s.lead_days}d lead</span>
                <span class="compare-stat"><i class="ti ti-shield-check"></i> ${s.reliability}% reliable</span>
                <span class="compare-score">${s.score}</span>
            </div>`).join("");
    } catch (err) {
        el.innerHTML = `<div class="compare-empty">Error: ${err.message}</div>`;
    }
}

async function createPOFromForm() {
    const invId = document.getElementById("po-inventory")?.value;
    const qty   = parseInt(document.getElementById("po-qty")?.value);
    const msg   = document.getElementById("poCreateMsg");

    if (!invId) { msg.style.color = "var(--critical-text)"; msg.textContent = "❌ Select an inventory item"; return; }
    if (!qty || qty <= 0) { msg.style.color = "var(--critical-text)"; msg.textContent = "❌ Enter a quantity"; return; }

    try {
        const res  = await fetch("/api/purchase-orders/from-recommendation", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ inventory_id: parseInt(invId), quantity: qty }),
        });
        const data = await res.json();
        msg.style.color = res.ok ? "var(--good-text)" : "var(--critical-text)";
        msg.textContent = data.message;
        if (res.ok) { document.getElementById("po-qty").value = ""; loadPurchasing(); }
    } catch (err) {
        msg.style.color = "var(--critical-text)"; msg.textContent = "Error: " + err.message;
    }
}

async function createPOWithSupplier(supplierId) {
    const invId = document.getElementById("po-inventory")?.value;
    const qty   = parseInt(document.getElementById("po-qty")?.value);
    const msg   = document.getElementById("poCreateMsg");

    if (!qty || qty <= 0) {
        alert("Enter a quantity first, then click a supplier row.");
        return;
    }
    try {
        const res  = await fetch("/api/purchase-orders", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ inventory_id: parseInt(invId), supplier_id: supplierId, quantity: qty }),
        });
        const data = await res.json();
        msg.style.color = res.ok ? "var(--good-text)" : "var(--critical-text)";
        msg.textContent = data.message;
        if (res.ok) { document.getElementById("po-qty").value = ""; loadPurchasing(); }
    } catch (err) {
        msg.style.color = "var(--critical-text)"; msg.textContent = "Error: " + err.message;
    }
}

async function approvePO(id) {
    const res  = await fetch(`/api/purchase-orders/${id}/approve`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) { alert(data.message); if (res.status === 401) openLoginModal(); return; }
    loadPurchasing();
}

async function sendPO(id) {
    const res  = await fetch(`/api/purchase-orders/${id}/send`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) { alert(data.message); return; }
    loadPurchasing();
}

async function receivePO(id, suggestedQty) {
    const input = prompt(`How many units arrived?`, suggestedQty);
    if (!input || isNaN(parseInt(input))) return;
    const res  = await fetch(`/api/purchase-orders/${id}/receive`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ quantity: parseInt(input) }),
    });
    const data = await res.json();
    if (!res.ok) { alert(data.message); return; }
    loadPurchasing();
    if (document.getElementById("view-inventory")?.classList.contains("active")) loadInventoryView();
    if (document.getElementById("view-dashboard")?.classList.contains("active")) loadDashboard();
}

async function cancelPO(id) {
    if (!confirm(`Cancel purchase order #${id}?`)) return;
    const res  = await fetch(`/api/purchase-orders/${id}/cancel`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) { alert(data.message); if (res.status === 401) openLoginModal(); return; }
    loadPurchasing();
}

async function payPurchaseOrder(id, amount) {
    if (!confirm(`Pay $${amount.toFixed(2)} to the supplier for PO #${id}?`)) return;
    const res  = await fetch(`/api/payments/purchase-order/${id}`, { method: "POST" });
    const data = await res.json();
    alert(data.message);
    if (res.status === 401) { openLoginModal(); return; }
    loadPurchasing();
}

async function payOrder(id) {
    const input = prompt("Amount to collect from the customer for this order ($):", "");
    if (!input || isNaN(parseFloat(input)) || parseFloat(input) <= 0) return;
    const res  = await fetch(`/api/payments/order/${id}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ amount: parseFloat(input) }),
    });
    const data = await res.json();
    alert(data.message);
    if (res.status === 401) { openLoginModal(); return; }
    loadDashboard();
}

async function loadPaymentHistory() {
    const tbody = document.getElementById("paymentsTableBody");
    if (!tbody) return;
    try {
        const res  = await fetch("/api/payments");
        const rows = await res.json();
        if (!rows.length) { tbody.innerHTML = `<tr><td colspan="7" class="table-empty">No payments recorded yet.</td></tr>`; return; }
        tbody.innerHTML = rows.map(p => `
            <tr>
                <td class="mono">#${p.id}</td>
                <td>${p.direction === "incoming" ? "⬇️ Incoming" : "⬆️ Outgoing"}</td>
                <td>${escapeHtml(p.reference_type)} #${p.reference_id}</td>
                <td>$${(p.amount || 0).toFixed(2)}</td>
                <td>${escapeHtml(p.provider)}</td>
                <td>${p.status === "succeeded"
                    ? `<span class="badge badge-accepted"><span class="badge-dot"></span>Succeeded</span>`
                    : `<span class="badge badge-critical"><span class="badge-dot"></span>${escapeHtml(p.status)}</span>`}</td>
                <td class="mono" style="font-size:11px">${(p.created_at || "").replace("T", " ").slice(0, 19)}</td>
            </tr>`).join("");
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="7" class="table-empty">Error loading payments.</td></tr>`;
    }
}

// ═══════════════════════════════════════════════════════
// ADMIN — users + audit log (visible/usable only when logged in as admin;
// the backend enforces this too, the UI just hides what you can't use)
// ═══════════════════════════════════════════════════════

async function loadUsers() {
    const tbody = document.getElementById("usersTableBody");
    if (!tbody) return;
    try {
        const res  = await fetch("/api/users");
        if (!res.ok) { tbody.innerHTML = `<tr><td colspan="5" class="table-empty">Log in as admin to manage users.</td></tr>`; return; }
        const data = await res.json();
        tbody.innerHTML = data.map(u => `
            <tr>
                <td>${escapeHtml(u.username)}</td>
                <td>
                    <div style="display:flex;align-items:center;gap:6px">
                        <select class="scanner-input" id="role-${u.id}" style="width:auto;display:inline-block;padding:6px 8px;font-size:12.5px"
                                onchange="document.getElementById('roleSave-${u.id}').style.display='inline-flex'">
                            <option value="admin"    ${u.role === "admin"    ? "selected" : ""}>admin</option>
                            <option value="operator" ${u.role === "operator" ? "selected" : ""}>operator</option>
                            <option value="viewer"   ${u.role === "viewer"   ? "selected" : ""}>viewer</option>
                        </select>
                        <button class="icon-btn" id="roleSave-${u.id}" style="display:none" title="Save role" onclick="updateUserRole(${u.id})"><i class="ti ti-check"></i></button>
                    </div>
                </td>
                <td>${(u.created_at || "").slice(0, 10) || "—"}</td>
                <td>${u.last_login ? u.last_login.replace("T", " ").slice(0, 16) : "—"}</td>
                <td>
                    <div style="display:flex;gap:6px">
                        <button class="icon-btn" onclick="openResetPasswordModal(${u.id}, '${escapeHtml(u.username)}')" title="Reset password"><i class="ti ti-key"></i></button>
                        <button class="icon-btn" onclick="deleteUserRow(${u.id}, '${escapeHtml(u.username)}')" title="Delete"><i class="ti ti-trash"></i></button>
                    </div>
                </td>
            </tr>`).join("");
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="5" class="table-empty">Error: ${err.message}</td></tr>`;
    }
}

async function updateUserRole(id) {
    const role = document.getElementById(`role-${id}`)?.value;
    if (!role) return;
    try {
        const res  = await fetch(`/api/users/${id}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ role }),
        });
        const data = await res.json();
        if (!res.ok) { alert(data.message); }
        loadUsers();
    } catch (err) {
        alert("Error: " + err.message);
    }
}

let resetPwUserId = null;

function openResetPasswordModal(id, username) {
    resetPwUserId = id;
    document.getElementById("resetPwUsername").textContent = username;
    document.getElementById("reset-pw-new").value = "";
    document.getElementById("resetPwMsg").textContent = "";
    document.getElementById("resetPasswordModal").style.display = "flex";
    document.getElementById("reset-pw-new")?.focus();
}

function closeResetPasswordModal() {
    document.getElementById("resetPasswordModal").style.display = "none";
    resetPwUserId = null;
}

async function doResetPassword() {
    if (!resetPwUserId) return;
    const new_password = document.getElementById("reset-pw-new").value;
    const msg = document.getElementById("resetPwMsg");
    try {
        const res  = await fetch(`/api/users/${resetPwUserId}/reset-password`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ new_password }),
        });
        const data = await res.json();
        if (res.ok) {
            closeResetPasswordModal();
        } else {
            msg.style.color = "var(--critical-text)"; msg.textContent = data.message;
        }
    } catch (err) {
        msg.style.color = "var(--critical-text)"; msg.textContent = "Error: " + err.message;
    }
}

async function createUser() {
    const username = document.getElementById("user-username").value.trim();
    const password = document.getElementById("user-password").value;
    const role     = document.getElementById("user-role").value;
    const msg      = document.getElementById("userCreateMsg");

    try {
        const res  = await fetch("/api/users", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ username, password, role }),
        });
        const data = await res.json();
        msg.style.color = res.ok ? "var(--good-text)" : "var(--critical-text)";
        msg.textContent = data.message;
        if (res.ok) {
            document.getElementById("user-username").value = "";
            document.getElementById("user-password").value = "";
            loadUsers();
        }
    } catch (err) {
        msg.style.color = "var(--critical-text)"; msg.textContent = "Error: " + err.message;
    }
}

async function deleteUserRow(id, username) {
    if (!confirm(`Delete user '${username}'?`)) return;
    const res  = await fetch(`/api/users/${id}`, { method: "DELETE" });
    const data = await res.json();
    if (!res.ok) { alert(data.message); return; }
    loadUsers();
}

async function loadAuditLog() {
    const tbody = document.getElementById("auditLogBody");
    if (!tbody) return;
    try {
        const res = await fetch("/api/audit-log");
        if (!res.ok) { tbody.innerHTML = `<tr><td colspan="5" class="table-empty">Log in as admin to view the audit log.</td></tr>`; return; }
        const data = await res.json();
        if (!data.length) { tbody.innerHTML = `<tr><td colspan="5" class="table-empty">No actions logged yet.</td></tr>`; return; }
        tbody.innerHTML = data.map(a => `
            <tr>
                <td class="mono" style="font-size:11px">${(a.created_at || "").replace("T", " ").slice(0, 19)}</td>
                <td>${escapeHtml(a.username || "guest")}</td>
                <td>${escapeHtml(a.action)}</td>
                <td>${a.entity_type ? escapeHtml(a.entity_type) + (a.entity_id ? " #" + a.entity_id : "") : "—"}</td>
                <td style="color:var(--text-muted);font-size:11.5px">${escapeHtml(a.details || "")}</td>
            </tr>`).join("");
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="5" class="table-empty">Error: ${err.message}</td></tr>`;
    }
}

// ═══════════════════════════════════════════════════════
// QUALITY — structured pass/fail checks on top of the quality_logs
// table the chat command "Log quality for order #42: ..." already
// wrote to (note + log_time only). Not login-gated, same reasoning
// as chat/dashboard/inventory: this isn't the higher-stakes stuff
// (money, users, audit) that this round of features locked down.
// ═══════════════════════════════════════════════════════

let allQualityLogs = [];

async function loadQuality() {
    try {
        const [qRes, ordRes] = await Promise.all([
            fetch("/api/quality-logs"),
            fetch("/api/orders"),
        ]);
        allQualityLogs = await qRes.json();
        const orders = await ordRes.json();

        for (const selId of ["qc-order", "cert-order"]) {
            const sel = document.getElementById(selId);
            if (!sel) continue;
            const current = sel.value;
            sel.innerHTML = '<option value="">Select order…</option>' +
                orders.map(o => `<option value="${o.id}">#${o.id} — ${escapeHtml(o.part_name)} (${escapeHtml(o.status)})</option>`).join("");
            if (current) sel.value = current;
        }

        renderQCTable(allQualityLogs);
    } catch (err) {
        console.error("Quality load error:", err);
    }
}

function resultBadge(result) {
    const map = {
        pass:        ["badge-accepted", "✅ Pass"],
        fail:        ["badge-critical", "❌ Fail"],
        conditional: ["badge-review",   "⚠️ Conditional"],
    };
    const [cls, label] = map[result] || ["badge-neutral", result || "—"];
    return `<span class="badge ${cls}"><span class="badge-dot"></span>${label}</span>`;
}

function renderQCTable(logs) {
    const tbody = document.getElementById("qcTableBody");
    if (!tbody) return;
    if (!logs.length) {
        tbody.innerHTML = `<tr><td colspan="7" class="table-empty">No quality checks logged yet — log one above, or use the chat command "Log quality for order #1: ...".</td></tr>`;
        return;
    }
    tbody.innerHTML = logs.map(l => `
        <tr data-qc-result="${l.result || ''}">
            <td class="order-id-cell">#${l.order_id}</td>
            <td class="part-name-cell">${escapeHtml(l.part_name || "—")}</td>
            <td>${l.result ? resultBadge(l.result) : `<span style="color:var(--text-muted);font-size:11.5px">note only</span>`}</td>
            <td>${escapeHtml(l.defect_category || "—")}</td>
            <td>${escapeHtml(l.corrective_action || "—")}</td>
            <td>${escapeHtml(l.logged_by || "—")}</td>
            <td class="mono" style="font-size:11px">${(l.log_time || "").replace("T", " ").slice(0, 19)}</td>
        </tr>`).join("");
}

function filterQCTable(result, btn) {
    document.querySelectorAll("#qcFilterGroup .filter-btn").forEach(b => b.classList.remove("active"));
    if (btn) btn.classList.add("active");
    document.querySelectorAll("#qcTableBody tr").forEach(row => {
        row.style.display = (result === "all" || row.dataset.qcResult === result) ? "" : "none";
    });
}

async function createQualityLog() {
    const orderId = document.getElementById("qc-order")?.value;
    const result  = document.getElementById("qc-result")?.value;
    const defect  = document.getElementById("qc-defect")?.value.trim();
    const action  = document.getElementById("qc-action")?.value.trim();
    const note    = document.getElementById("qc-note")?.value.trim();
    const msg     = document.getElementById("qcCreateMsg");

    if (!orderId) { msg.style.color = "var(--critical-text)"; msg.textContent = "❌ Select an order"; return; }
    if (result === "fail" && !defect) { msg.style.color = "var(--critical-text)"; msg.textContent = "❌ A failed check needs a defect category"; return; }

    try {
        const res  = await fetch("/api/quality-logs", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                order_id: parseInt(orderId), result,
                defect_category: defect, corrective_action: action, note,
            }),
        });
        const data = await res.json();
        msg.style.color = res.ok ? "var(--good-text)" : "var(--critical-text)";
        msg.textContent = data.message;
        if (res.ok) {
            document.getElementById("qc-defect").value = "";
            document.getElementById("qc-action").value = "";
            document.getElementById("qc-note").value = "";
            loadQuality();
        }
    } catch (err) {
        msg.style.color = "var(--critical-text)"; msg.textContent = "Error: " + err.message;
    }
}

async function viewCertificate() {
    const orderId = document.getElementById("cert-order")?.value;
    if (!orderId) { alert("Select an order first."); return; }

    const body = document.getElementById("certModalBody");
    body.innerHTML = `<div class="demand-loading"><i class="ti ti-loader"></i> Loading…</div>`;
    document.getElementById("certModal").style.display = "flex";

    try {
        const res  = await fetch(`/api/orders/${orderId}/quality-summary`);
        const data = await res.json();
        if (!res.ok) { body.innerHTML = `<p class="modal-hint">${escapeHtml(data.message || "Not found")}</p>`; return; }
        body.innerHTML = renderCertificate(data);
    } catch (err) {
        body.innerHTML = `<p class="modal-hint">Error: ${escapeHtml(err.message)}</p>`;
    }
}

function closeCertModal() {
    document.getElementById("certModal").style.display = "none";
}

function renderCertificate(data) {
    const o = data.order;
    const overallMap = {
        pass:               ["badge-accepted", "✅ PASS"],
        fail:                ["badge-critical", "❌ FAIL"],
        conditional:         ["badge-review",   "⚠️ CONDITIONAL"],
        "no checks logged":  ["badge-neutral",  "NO CHECKS LOGGED"],
    };
    const [cls, label] = overallMap[data.overall_result] || ["badge-neutral", data.overall_result];

    const rows = data.logs.length
        ? data.logs.map(l => `
            <tr>
                <td class="mono" style="font-size:11px">${(l.log_time || "").replace("T", " ").slice(0, 19)}</td>
                <td>${l.result ? resultBadge(l.result) : `<span style="color:var(--text-muted);font-size:11.5px">note only</span>`}</td>
                <td>${escapeHtml(l.defect_category || "—")}</td>
                <td>${escapeHtml(l.corrective_action || "—")}</td>
                <td style="color:var(--text-muted);font-size:11.5px">${escapeHtml(l.note || "—")}</td>
                <td>${escapeHtml(l.logged_by || "—")}</td>
            </tr>`).join("")
        : `<tr><td colspan="6" class="table-empty">No quality checks logged for this order yet.</td></tr>`;

    return `
        <div style="margin-bottom:16px">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px;gap:12px">
                <div>
                    <div style="font-size:11px;color:var(--text-muted);text-transform:uppercase;letter-spacing:.05em">Order</div>
                    <div style="font-size:18px;font-weight:700">#${o.id} — ${escapeHtml(o.part_name)}</div>
                </div>
                <span class="badge ${cls}" style="font-size:12px;padding:6px 12px;white-space:nowrap"><span class="badge-dot"></span>${label}</span>
            </div>
            <div class="cert-meta-grid">
                <div><span class="add-inv-label">Material</span><div>${escapeHtml(o.material || "—")}</div></div>
                <div><span class="add-inv-label">Quantity</span><div>${o.quantity ?? "—"}</div></div>
                <div><span class="add-inv-label">Order Status</span><div>${escapeHtml(o.status || "—")}</div></div>
                <div><span class="add-inv-label">Deadline</span><div>${escapeHtml(o.deadline || "—")}</div></div>
            </div>
        </div>
        <div class="table-wrap">
            <table>
                <thead><tr><th>When</th><th>Result</th><th>Defect Category</th><th>Corrective Action</th><th>Note</th><th>Logged By</th></tr></thead>
                <tbody>${rows}</tbody>
            </table>
        </div>
        <p class="modal-hint" style="margin-top:14px">Generated ${new Date().toLocaleString()} by OrderFlow AI — reflects all quality checks on file for this order at the time of printing.</p>
    `;
}

// ═══════════════════════════════════════════════════════
// REPORTS — read-only views over /api/reports/*. CSV/PDF export
// buttons are plain <a href> links in the HTML (the server sends
// Content-Disposition: attachment), so there's no JS for those.
// ═══════════════════════════════════════════════════════

function abcBadge(cls) {
    const map = { A: "badge-accepted", B: "badge-review", C: "badge-neutral" };
    return `<span class="badge ${map[cls] || "badge-cancelled"}"><span class="badge-dot"></span>${cls}</span>`;
}

async function loadReports() {
    try {
        const [valRes, scoreRes, fulRes] = await Promise.all([
            fetch("/api/reports/inventory-valuation"),
            fetch("/api/reports/supplier-scorecard"),
            fetch("/api/reports/fulfillment"),
        ]);
        const valuation  = await valRes.json();
        const scorecard  = await scoreRes.json();
        const fulfillment = await fulRes.json();

        document.getElementById("repStatValue").innerText =
            "$" + valuation.total_value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        document.getElementById("repStatAvgDays").innerText =
            fulfillment.avg_fulfillment_days ?? "—";
        document.getElementById("repStatMeasured").innerText = fulfillment.measured_orders;
        const openCount = Object.entries(fulfillment.status_counts)
            .filter(([status]) => status !== "Accepted" && status !== "Cancelled")
            .reduce((sum, [, c]) => sum + c, 0);
        document.getElementById("repStatOpen").innerText = openCount;

        renderValuationTable(valuation.items);
        renderScorecardTable(scorecard);
        renderOldestOpenTable(fulfillment.oldest_open_orders);
    } catch (err) {
        console.error("Reports load error:", err);
    }
}

function renderValuationTable(items) {
    const tbody = document.getElementById("valuationTableBody");
    if (!tbody) return;
    if (!items.length) {
        tbody.innerHTML = `<tr><td colspan="6" class="table-empty">No inventory items yet.</td></tr>`;
        return;
    }
    tbody.innerHTML = items.map(i => `
        <tr>
            <td class="part-name-cell">${escapeHtml(i.part_name)}</td>
            <td>${i.quantity}</td>
            <td class="mono">$${i.unit_cost.toFixed(2)}</td>
            <td class="mono">$${i.total_value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
            <td>${i.value_pct !== null ? i.value_pct + "%" : "—"}</td>
            <td>${abcBadge(i.abc_class)}</td>
        </tr>`).join("");
}

function renderScorecardTable(rows) {
    const tbody = document.getElementById("scorecardTableBody");
    if (!tbody) return;
    if (!rows.length) {
        tbody.innerHTML = `<tr><td colspan="8" class="table-empty">No suppliers on file yet.</td></tr>`;
        return;
    }
    tbody.innerHTML = rows.map(s => `
        <tr>
            <td class="part-name-cell">${escapeHtml(s.name)}</td>
            <td>${escapeHtml(s.part_name || "—")}</td>
            <td>${s.lead_days}d</td>
            <td>${s.avg_actual_lead_days !== null ? s.avg_actual_lead_days + "d" : "—"}</td>
            <td>${s.on_time_rate !== null ? s.on_time_rate + "%" : "—"}</td>
            <td>${s.reliability}%</td>
            <td>${s.completed_pos}</td>
            <td class="mono" style="font-weight:700">${s.scorecard_score}</td>
        </tr>`).join("");
}

function renderOldestOpenTable(rows) {
    const tbody = document.getElementById("oldestOpenTableBody");
    if (!tbody) return;
    if (!rows.length) {
        tbody.innerHTML = `<tr><td colspan="5" class="table-empty">No open orders — everything's Accepted or Cancelled.</td></tr>`;
        return;
    }
    tbody.innerHTML = rows.map(o => `
        <tr>
            <td class="order-id-cell">#${o.id}</td>
            <td class="part-name-cell">${escapeHtml(o.part_name)}</td>
            <td>${statusBadge(o.status)}</td>
            <td class="mono" style="font-size:11px">${(o.created_at || "").replace("T", " ").slice(0, 16)}</td>
            <td>${escapeHtml(o.deadline || "—")}</td>
        </tr>`).join("");
}

// ═══════════════════════════════════════════════════════════════════
// PRODUCTION — Bill of Materials / Work Orders / Machines
// ═══════════════════════════════════════════════════════════════════

function setProdTab(tab, btn) {
    document.querySelectorAll("#prodTabs .demand-tab").forEach(b => b.classList.remove("active"));
    if (btn) btn.classList.add("active");
    document.querySelectorAll("#view-production .demand-tab-panel").forEach(p => p.classList.remove("active"));
    document.getElementById("prod-panel-" + tab)?.classList.add("active");
}

async function loadProduction() {
    try {
        const [invRes, machRes] = await Promise.all([
            fetch("/api/inventory"),
            fetch("/api/machines"),
        ]);
        const inv = await invRes.json();
        const machines = await machRes.json();

        const itemOptions = '<option value="">Select item…</option>' +
            inv.map(i => `<option value="${i.id}">${escapeHtml(i.part_name)} (stock: ${i.quantity})</option>`).join("");
        ["bom-parent", "bom-component", "wo-inventory"].forEach(id => {
            const sel = document.getElementById(id);
            if (sel) { const cur = sel.value; sel.innerHTML = itemOptions; if (cur) sel.value = cur; }
        });

        const woMachineSel = document.getElementById("wo-machine");
        if (woMachineSel) {
            const cur = woMachineSel.value;
            woMachineSel.innerHTML = '<option value="">Unassigned</option>' +
                machines.map(m => `<option value="${m.id}">${escapeHtml(m.name)}${m.status === "down" ? " (down)" : ""}</option>`).join("");
            if (cur) woMachineSel.value = cur;
        }

        const bomFilter = document.getElementById("bom-filter");
        if (bomFilter) {
            const cur = bomFilter.value;
            bomFilter.innerHTML = '<option value="">All finished items</option>' +
                inv.map(i => `<option value="${i.id}">${escapeHtml(i.part_name)}</option>`).join("");
            if (cur) bomFilter.value = cur;
        }

        await Promise.all([loadBom(), loadWorkOrders(), loadMachines()]);
    } catch (err) {
        console.error("Production load error:", err);
    }
}

async function loadBom() {
    const tbody = document.getElementById("bomTableBody");
    if (!tbody) return;
    try {
        const filterSel = document.getElementById("bom-filter");
        const parentId = filterSel?.value;
        const parentName = parentId ? filterSel.options[filterSel.selectedIndex].textContent : null;
        const url = parentId ? `/api/bom?parent_inventory_id=${parentId}` : "/api/bom";
        const res  = await fetch(url);
        const rows = await res.json();
        if (!rows.length) {
            tbody.innerHTML = `<tr><td colspan="5" class="table-empty">No BOM lines yet — add one above.</td></tr>`;
            return;
        }
        tbody.innerHTML = rows.map(r => `
            <tr>
                <td class="part-name-cell">${escapeHtml(parentName || r.parent_name || ("#" + r.parent_inventory_id))}</td>
                <td>${escapeHtml(r.component_name)}</td>
                <td class="mono">${r.qty_per_unit}</td>
                <td>${r.component_stock !== undefined ? r.component_stock + " " + escapeHtml(r.component_unit || "pcs") : "—"}</td>
                <td><button class="table-action-btn" onclick="deleteBomLine(${r.id})" title="Remove"><i class="ti ti-trash"></i></button></td>
            </tr>`).join("");
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="5" class="table-empty">Error loading BOM.</td></tr>`;
    }
}

async function addBomLine() {
    const msg = document.getElementById("bomCreateMsg");
    const payload = {
        parent_inventory_id:    parseInt(document.getElementById("bom-parent").value) || null,
        component_inventory_id: parseInt(document.getElementById("bom-component").value) || null,
        qty_per_unit:            parseFloat(document.getElementById("bom-qty").value) || null,
    };
    if (!payload.parent_inventory_id || !payload.component_inventory_id || !payload.qty_per_unit) {
        msg.style.color = "var(--critical-text)";
        msg.textContent = "❌ Finished item, component, and qty per unit are all required";
        return;
    }
    try {
        const res  = await fetch("/api/bom", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
        const data = await res.json();
        msg.style.color = res.ok ? "var(--good-text)" : "var(--critical-text)";
        msg.textContent = data.message;
        if (res.ok) { document.getElementById("bom-qty").value = ""; loadBom(); }
    } catch (err) {
        msg.style.color = "var(--critical-text)"; msg.textContent = "Error: " + err.message;
    }
}

async function deleteBomLine(id) {
    try {
        await fetch(`/api/bom/${id}`, { method: "DELETE" });
        loadBom();
    } catch (err) { console.error(err); }
}

function woStatusBadge(status) {
    const map = {
        planned:     ["badge-neutral",   "Planned"],
        in_progress: ["badge-received",  "In Progress"],
        completed:   ["badge-accepted",  "Completed"],
        cancelled:   ["badge-cancelled", "Cancelled"],
    };
    const [cls, label] = map[status] || ["badge-neutral", status];
    return `<span class="badge ${cls}"><span class="badge-dot"></span>${label}</span>`;
}

async function loadWorkOrders() {
    const tbody = document.getElementById("workOrdersTableBody");
    if (!tbody) return;
    try {
        const res  = await fetch("/api/work-orders");
        const rows = await res.json();
        if (!rows.length) {
            tbody.innerHTML = `<tr><td colspan="6" class="table-empty">No work orders yet — plan one above.</td></tr>`;
            return;
        }
        tbody.innerHTML = rows.map(wo => {
            let actions = "";
            if (wo.status === "planned") {
                actions = `<button class="po-action-btn primary" onclick="startWorkOrder(${wo.id})"><i class="ti ti-player-play"></i> Start</button>
                           <button class="po-action-btn danger" onclick="cancelWorkOrder(${wo.id})"><i class="ti ti-x"></i> Cancel</button>`;
            } else if (wo.status === "in_progress") {
                actions = `<button class="po-action-btn good" onclick="completeWorkOrder(${wo.id})"><i class="ti ti-check"></i> Complete</button>
                           <button class="po-action-btn danger" onclick="cancelWorkOrder(${wo.id})"><i class="ti ti-x"></i> Cancel</button>`;
            } else {
                actions = `<span style="color:var(--text-muted);font-size:11.5px">—</span>`;
            }
            return `
            <tr>
                <td class="order-id-cell">#${wo.id}</td>
                <td class="part-name-cell">${escapeHtml(wo.part_name)}</td>
                <td>${wo.quantity}</td>
                <td>${escapeHtml(wo.machine_name || "—")}</td>
                <td>${woStatusBadge(wo.status)}</td>
                <td><div class="po-actions">${actions}</div></td>
            </tr>`;
        }).join("");
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="6" class="table-empty">Error loading work orders.</td></tr>`;
    }
}

async function addWorkOrder() {
    const msg = document.getElementById("woCreateMsg");
    const payload = {
        inventory_id: parseInt(document.getElementById("wo-inventory").value) || null,
        quantity:     parseInt(document.getElementById("wo-qty").value) || null,
        machine_id:   parseInt(document.getElementById("wo-machine").value) || null,
    };
    if (!payload.inventory_id || !payload.quantity) {
        msg.style.color = "var(--critical-text)"; msg.textContent = "❌ Item and quantity are required";
        return;
    }
    try {
        const res  = await fetch("/api/work-orders", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
        const data = await res.json();
        msg.style.color = res.ok ? "var(--good-text)" : "var(--critical-text)";
        msg.textContent = data.message;
        if (res.ok) { document.getElementById("wo-qty").value = ""; loadWorkOrders(); }
    } catch (err) {
        msg.style.color = "var(--critical-text)"; msg.textContent = "Error: " + err.message;
    }
}

async function startWorkOrder(id)    { await woAction(id, "start"); }
async function completeWorkOrder(id) { await woAction(id, "complete"); }
async function cancelWorkOrder(id)   { await woAction(id, "cancel"); }

async function woAction(id, action) {
    try {
        const res  = await fetch(`/api/work-orders/${id}/${action}`, { method: "POST" });
        const data = await res.json();
        if (!res.ok) alert(data.message);
        loadWorkOrders();
        if (action === "complete") { loadDashboard(); if (document.getElementById("view-inventory")?.classList.contains("active")) loadInventoryView(); }
    } catch (err) {
        alert("Error: " + err.message);
    }
}

async function loadMachines() {
    const grid = document.getElementById("machineGrid");
    if (!grid) return;
    try {
        const res = await fetch("/api/machines");
        const machines = await res.json();
        if (!machines.length) {
            grid.innerHTML = `<div class="table-empty">No machines yet — add one above.</div>`;
            return;
        }
        grid.innerHTML = machines.map(m => `
            <div class="machine-card ${m.status}">
                <div class="machine-card-header">
                    <span class="machine-card-name">${escapeHtml(m.name)}</span>
                    <span class="badge ${m.status === 'running' ? 'badge-accepted' : 'badge-critical'}">
                        <span class="badge-dot"></span>${m.status === 'running' ? 'Running' : 'Down'}
                    </span>
                </div>
                <div class="machine-card-loc">${escapeHtml(m.location || "No location set")}</div>
                ${m.open_downtime ? `<div class="machine-downtime-note"><i class="ti ti-alert-triangle"></i> ${escapeHtml(m.open_downtime.reason)}</div>` : ""}
                <div class="machine-card-actions">
                    ${m.status === "running"
                        ? `<button class="po-action-btn danger" onclick="markMachineDown(${m.id})"><i class="ti ti-player-pause"></i> Mark Down</button>`
                        : `<button class="po-action-btn good" onclick="resolveMachineDowntime(${m.open_downtime ? m.open_downtime.id : 0})"><i class="ti ti-player-play"></i> Resolve</button>`}
                    <button class="po-action-btn danger" onclick="deleteMachine(${m.id})"><i class="ti ti-trash"></i></button>
                </div>
            </div>`).join("");
    } catch (err) {
        grid.innerHTML = `<div class="table-empty">Error loading machines.</div>`;
    }
}

async function addMachine() {
    const msg = document.getElementById("machCreateMsg");
    const name = document.getElementById("mach-name").value.trim();
    if (!name) { msg.style.color = "var(--critical-text)"; msg.textContent = "❌ Name is required"; return; }
    const payload = { name, location: document.getElementById("mach-location").value.trim() };
    try {
        const res  = await fetch("/api/machines", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
        const data = await res.json();
        msg.style.color = res.ok ? "var(--good-text)" : "var(--critical-text)";
        msg.textContent = data.message;
        if (res.ok) {
            document.getElementById("mach-name").value = "";
            document.getElementById("mach-location").value = "";
            loadMachines();
            loadProduction(); // refresh wo-machine dropdown too
        }
    } catch (err) {
        msg.style.color = "var(--critical-text)"; msg.textContent = "Error: " + err.message;
    }
}

async function deleteMachine(id) {
    if (!confirm("Delete this machine?")) return;
    try {
        const res = await fetch(`/api/machines/${id}`, { method: "DELETE" });
        const data = await res.json();
        if (!res.ok) alert(data.message);
        loadMachines();
    } catch (err) { alert("Error: " + err.message); }
}

async function markMachineDown(id) {
    const reason = prompt("Reason for downtime?") || "Unspecified";
    try {
        const res = await fetch(`/api/machines/${id}/downtime`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reason }) });
        const data = await res.json();
        if (!res.ok) alert(data.message);
        loadMachines();
    } catch (err) { alert("Error: " + err.message); }
}

async function resolveMachineDowntime(downtimeId) {
    if (!downtimeId) { loadMachines(); return; }
    try {
        const res = await fetch(`/api/machine-downtime/${downtimeId}/resolve`, { method: "POST" });
        const data = await res.json();
        if (!res.ok) alert(data.message);
        loadMachines();
    } catch (err) { alert("Error: " + err.message); }
}

// ═══════════════════════════════════════════════════════════════════
// NOTIFICATIONS
// ═══════════════════════════════════════════════════════════════════

async function loadNotifications() {
    try {
        await Promise.all([loadChannelStatus(), loadNotificationSettings(), loadNotificationLog()]);
    } catch (err) {
        console.error("Notifications load error:", err);
    }
}

async function loadChannelStatus() {
    const grid = document.getElementById("channelStatusGrid");
    const hint = document.getElementById("channelConfigHint");
    if (!grid) return;
    try {
        const res    = await fetch("/api/notifications/channel-status");
        const status = await res.json();
        const icons  = { email: "ti-mail", sms: "ti-message", slack: "ti-brand-slack" };
        grid.innerHTML = Object.keys(status).map(ch => `
            <div class="channel-status-card">
                <i class="ti ${icons[ch] || 'ti-plug'}"></i>
                <div>
                    <div class="channel-status-name">${ch}</div>
                    <span class="badge ${status[ch] ? 'badge-accepted' : 'badge-cancelled'}">
                        <span class="badge-dot"></span>${status[ch] ? "Configured" : "Not configured"}
                    </span>
                </div>
            </div>`).join("");
        if (hint) hint.style.display = Object.values(status).some(Boolean) ? "none" : "flex";
    } catch (err) {
        grid.innerHTML = `<div class="table-empty">Error loading channel status.</div>`;
    }
}

async function loadNotificationSettings() {
    const tbody = document.getElementById("notifSettingsTableBody");
    if (!tbody) return;
    try {
        const res  = await fetch("/api/notifications/settings");
        const rows = await res.json();
        if (!rows.length) {
            tbody.innerHTML = `<tr><td colspan="5" class="table-empty">No rules yet — add one above (alerts fire but go nowhere without at least one rule).</td></tr>`;
            return;
        }
        tbody.innerHTML = rows.map(r => `
            <tr>
                <td>${escapeHtml(r.event_type)}</td>
                <td style="text-transform:capitalize">${escapeHtml(r.channel)}</td>
                <td>${escapeHtml(r.target)}</td>
                <td>${r.enabled ? "✅" : "—"}</td>
                <td><button class="table-action-btn" onclick="deleteNotificationSetting(${r.id})"><i class="ti ti-trash"></i></button></td>
            </tr>`).join("");
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="5" class="table-empty">Error loading settings.</td></tr>`;
    }
}

async function addNotificationRule() {
    const msg = document.getElementById("notifCreateMsg");
    const payload = {
        event_type: document.getElementById("notif-event").value,
        channel:    document.getElementById("notif-channel").value,
        target:     document.getElementById("notif-target").value.trim(),
    };
    if (!payload.target) { msg.style.color = "var(--critical-text)"; msg.textContent = "❌ Target is required"; return; }
    try {
        const res  = await fetch("/api/notifications/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
        const data = await res.json();
        msg.style.color = res.ok ? "var(--good-text)" : "var(--critical-text)";
        msg.textContent = data.message;
        if (res.ok) { document.getElementById("notif-target").value = ""; loadNotificationSettings(); }
    } catch (err) {
        msg.style.color = "var(--critical-text)"; msg.textContent = "Error: " + err.message;
    }
}

async function deleteNotificationSetting(id) {
    try {
        await fetch(`/api/notifications/settings/${id}`, { method: "DELETE" });
        loadNotificationSettings();
    } catch (err) { console.error(err); }
}

async function testNotification() {
    const msg     = document.getElementById("notifCreateMsg");
    const channel = document.getElementById("notif-channel").value;
    const target  = document.getElementById("notif-target").value.trim();
    if (!target) { msg.style.color = "var(--critical-text)"; msg.textContent = "❌ Enter a target first"; return; }
    try {
        const res  = await fetch("/api/notifications/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ channel, target }) });
        const data = await res.json();
        msg.style.color = res.ok ? "var(--good-text)" : "var(--critical-text)";
        msg.textContent = data.message;
        loadNotificationLog();
    } catch (err) {
        msg.style.color = "var(--critical-text)"; msg.textContent = "Error: " + err.message;
    }
}

async function loadNotificationLog() {
    const tbody = document.getElementById("notifLogTableBody");
    if (!tbody) return;
    try {
        const res  = await fetch("/api/notifications/log");
        const rows = await res.json();
        if (!rows.length) {
            tbody.innerHTML = `<tr><td colspan="6" class="table-empty">No notifications logged yet.</td></tr>`;
            return;
        }
        tbody.innerHTML = rows.map(r => {
            const okStatus = r.status === "sent";
            const skipped  = r.status === "skipped_not_configured";
            const cls = okStatus ? "badge-accepted" : skipped ? "badge-neutral" : "badge-critical";
            const label = okStatus ? "Sent" : skipped ? "Skipped" : "Failed";
            return `
            <tr>
                <td class="mono" style="font-size:11px">${(r.created_at || "").replace("T", " ").slice(0, 16)}</td>
                <td>${escapeHtml(r.event_type)}</td>
                <td style="text-transform:capitalize">${escapeHtml(r.channel)}</td>
                <td>${escapeHtml(r.recipient || "—")}</td>
                <td><span class="badge ${cls}"><span class="badge-dot"></span>${label}</span></td>
                <td style="max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(r.message || '')}">${escapeHtml(r.message || "—")}</td>
            </tr>`;
        }).join("");
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="6" class="table-empty">Error loading log.</td></tr>`;
    }
}

async function checkLateSuppliers() {
    try {
        const res  = await fetch("/api/notifications/check-late-suppliers", { method: "POST" });
        const data = await res.json();
        alert(data.late.length
            ? `⚠️ ${data.late.length} of ${data.checked} open PO(s) are running late — alerts sent to any configured channels. See the log below.`
            : `✅ Checked ${data.checked} open PO(s) — none are late.`);
        loadNotificationLog();
    } catch (err) {
        alert("Error: " + err.message);
    }
}

// ═══════════════════════════════════════════════════════════════════
// WAREHOUSES & LOCATIONS  (collapsible section inside Inventory view)
// ═══════════════════════════════════════════════════════════════════

let warehouseSectionOpen   = false;
let warehouseSectionLoaded = false;

function toggleWarehouseSection() {
    warehouseSectionOpen = !warehouseSectionOpen;
    document.getElementById("warehouseContent").classList.toggle("open", warehouseSectionOpen);
    document.getElementById("warehouseHeader").classList.toggle("open", warehouseSectionOpen);
    if (warehouseSectionOpen && !warehouseSectionLoaded) {
        loadWarehouseSection();
        warehouseSectionLoaded = true;
    }
}

async function loadWarehouseSection() {
    try {
        const [invRes, whRes] = await Promise.all([fetch("/api/inventory"), fetch("/api/warehouses")]);
        const inv = await invRes.json();
        const warehouses = await whRes.json();

        const itemOptions = '<option value="">Select item…</option>' +
            inv.map(i => `<option value="${i.id}">${escapeHtml(i.part_name)} (total: ${i.quantity})</option>`).join("");
        ["loc-inventory", "xfer-inventory"].forEach(id => {
            const sel = document.getElementById(id);
            if (sel) { const cur = sel.value; sel.innerHTML = itemOptions; if (cur) sel.value = cur; }
        });

        const whOptions = '<option value="">Select warehouse…</option>' +
            warehouses.map(w => `<option value="${w.id}">${escapeHtml(w.name)}</option>`).join("");
        ["loc-warehouse", "xfer-from", "xfer-to"].forEach(id => {
            const sel = document.getElementById(id);
            if (sel) { const cur = sel.value; sel.innerHTML = whOptions; if (cur) sel.value = cur; }
        });

        renderWarehousesTable(warehouses);
        await loadLocationsTable();
    } catch (err) {
        console.error("Warehouse section load error:", err);
    }
}

function renderWarehousesTable(warehouses) {
    const tbody = document.getElementById("warehousesTableBody");
    if (!tbody) return;
    if (!warehouses.length) {
        tbody.innerHTML = `<tr><td colspan="4" class="table-empty">No warehouses yet — add one above.</td></tr>`;
        return;
    }
    tbody.innerHTML = warehouses.map(w => `
        <tr>
            <td class="order-id-cell">#${w.id}</td>
            <td class="part-name-cell">${escapeHtml(w.name)}</td>
            <td>${escapeHtml(w.address || "—")}</td>
            <td><button class="table-action-btn" onclick="deleteWarehouse(${w.id})"><i class="ti ti-trash"></i></button></td>
        </tr>`).join("");
}

async function addWarehouse() {
    const msg  = document.getElementById("whCreateMsg");
    const name = document.getElementById("wh-name").value.trim();
    if (!name) { msg.style.color = "var(--critical-text)"; msg.textContent = "❌ Name is required"; return; }
    const payload = { name, address: document.getElementById("wh-address").value.trim() };
    try {
        const res  = await fetch("/api/warehouses", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
        const data = await res.json();
        msg.style.color = res.ok ? "var(--good-text)" : "var(--critical-text)";
        msg.textContent = data.message;
        if (res.ok) {
            document.getElementById("wh-name").value = "";
            document.getElementById("wh-address").value = "";
            loadWarehouseSection();
        }
    } catch (err) {
        msg.style.color = "var(--critical-text)"; msg.textContent = "Error: " + err.message;
    }
}

async function deleteWarehouse(id) {
    if (!confirm("Delete this warehouse? Stock still assigned there must be transferred out first.")) return;
    try {
        const res  = await fetch(`/api/warehouses/${id}`, { method: "DELETE" });
        const data = await res.json();
        if (!res.ok) { alert(data.message); return; }
        loadWarehouseSection();
    } catch (err) { alert("Error: " + err.message); }
}

async function loadLocationSummary() {
    const invId = document.getElementById("loc-inventory")?.value;
    const note  = document.getElementById("locSummaryNote");
    if (!note) return;
    if (!invId) { note.innerHTML = ""; return; }
    try {
        const res = await fetch(`/api/inventory-locations/summary/${invId}`);
        if (!res.ok) { note.innerHTML = ""; return; }
        const s = await res.json();
        note.innerHTML = `<div class="info-banner" style="margin:6px 0 0"><i class="ti ti-info-circle"></i>
            <div><strong>${s.unassigned}</strong> of ${s.total_quantity} units of ${escapeHtml(s.part_name)} are currently unassigned to a location.</div></div>`;
    } catch (err) { note.innerHTML = ""; }
}

async function assignLocation() {
    const msg = document.getElementById("locAssignMsg");
    const payload = {
        inventory_id: parseInt(document.getElementById("loc-inventory").value) || null,
        warehouse_id: parseInt(document.getElementById("loc-warehouse").value) || null,
        quantity:     parseInt(document.getElementById("loc-qty").value) || 0,
        reorder_at:   parseInt(document.getElementById("loc-reorder").value) || 10,
    };
    if (!payload.inventory_id || !payload.warehouse_id) {
        msg.style.color = "var(--critical-text)"; msg.textContent = "❌ Item and warehouse are required";
        return;
    }
    try {
        const res  = await fetch("/api/inventory-locations", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
        const data = await res.json();
        msg.style.color = res.ok ? "var(--good-text)" : "var(--critical-text)";
        msg.textContent = data.message;
        if (res.ok) { loadLocationSummary(); loadLocationsTable(); }
    } catch (err) {
        msg.style.color = "var(--critical-text)"; msg.textContent = "Error: " + err.message;
    }
}

async function transferLocation() {
    const msg = document.getElementById("xferMsg");
    const payload = {
        inventory_id:      parseInt(document.getElementById("xfer-inventory").value) || null,
        from_warehouse_id: parseInt(document.getElementById("xfer-from").value) || null,
        to_warehouse_id:   parseInt(document.getElementById("xfer-to").value) || null,
        quantity:          parseInt(document.getElementById("xfer-qty").value) || null,
    };
    if (!payload.inventory_id || !payload.from_warehouse_id || !payload.to_warehouse_id || !payload.quantity) {
        msg.style.color = "var(--critical-text)"; msg.textContent = "❌ All fields are required";
        return;
    }
    try {
        const res  = await fetch("/api/inventory-locations/transfer", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
        const data = await res.json();
        msg.style.color = res.ok ? "var(--good-text)" : "var(--critical-text)";
        msg.textContent = data.message;
        if (res.ok) { document.getElementById("xfer-qty").value = ""; loadLocationsTable(); }
    } catch (err) {
        msg.style.color = "var(--critical-text)"; msg.textContent = "Error: " + err.message;
    }
}

async function loadLocationsTable() {
    const tbody = document.getElementById("locationsTableBody");
    if (!tbody) return;
    try {
        const res  = await fetch("/api/inventory-locations");
        const rows = await res.json();
        if (!rows.length) {
            tbody.innerHTML = `<tr><td colspan="4" class="table-empty">No stock assigned to any location yet.</td></tr>`;
            return;
        }
        tbody.innerHTML = rows.map(r => `
            <tr>
                <td class="part-name-cell">${escapeHtml(r.part_name || "")}</td>
                <td>${escapeHtml(r.warehouse_name)}</td>
                <td>${r.quantity}</td>
                <td>${r.reorder_at}</td>
            </tr>`).join("");
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="4" class="table-empty">Error loading locations.</td></tr>`;
    }
}

// ═══════════════════════════════════════════════════════════════════
// GLOBAL SEARCH  (sidebar)
// ═══════════════════════════════════════════════════════════════════

let searchDebounceTimer = null;

function handleGlobalSearch(value) {
    const clearBtn = document.getElementById("searchClearBtn");
    if (clearBtn) clearBtn.style.display = value ? "block" : "none";

    clearTimeout(searchDebounceTimer);
    const q = value.trim();
    if (q.length < 2) {
        closeSearchDropdown();
        return;
    }
    searchDebounceTimer = setTimeout(() => runGlobalSearch(q), 250);
}

async function runGlobalSearch(q) {
    const dropdown = document.getElementById("searchResultsDropdown");
    if (!dropdown) return;
    try {
        const res  = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
        const data = await res.json();
        const total = (data.orders?.length || 0) + (data.inventory?.length || 0) + (data.suppliers?.length || 0);

        if (!total) {
            dropdown.innerHTML = `<div class="search-empty-state">No matches for "${escapeHtml(q)}"</div>`;
            dropdown.classList.add("open");
            return;
        }

        let html = "";
        if (data.inventory?.length) {
            html += `<div class="search-group-label">Inventory</div>` + data.inventory.map(i => `
                <div class="search-result-item" onclick="jumpToSearchResult('inventory', ${i.id})">
                    <span class="sr-name">${escapeHtml(i.part_name)}</span>
                    <span class="sr-meta">${i.quantity} in stock</span>
                </div>`).join("");
        }
        if (data.orders?.length) {
            html += `<div class="search-group-label">Orders</div>` + data.orders.map(o => `
                <div class="search-result-item" onclick="jumpToSearchResult('orders', ${o.id})">
                    <span class="sr-name">#${o.id} ${escapeHtml(o.part_name || "")}</span>
                    <span class="sr-meta">${escapeHtml(o.status || "")}</span>
                </div>`).join("");
        }
        if (data.suppliers?.length) {
            html += `<div class="search-group-label">Suppliers</div>` + data.suppliers.map(s => `
                <div class="search-result-item" onclick="jumpToSearchResult('suppliers', ${s.id})">
                    <span class="sr-name">${escapeHtml(s.name)}</span>
                    <span class="sr-meta">${escapeHtml(s.part_name || "")}</span>
                </div>`).join("");
        }
        dropdown.innerHTML = html;
        dropdown.classList.add("open");
    } catch (err) {
        dropdown.innerHTML = `<div class="search-empty-state">Search error.</div>`;
        dropdown.classList.add("open");
    }
}

function jumpToSearchResult(kind, id) {
    closeSearchDropdown();
    if (kind === "inventory") {
        showView("inventory");
    } else if (kind === "orders") {
        showView("dashboard");
    } else if (kind === "suppliers") {
        showView("dashboard");
        setTimeout(() => {
            if (!demandOpen) toggleDemandSection();
            setDemandTab("suppliers", document.querySelector('.demand-tab[data-tab="suppliers"]'));
        }, 50);
    }
}

function closeSearchDropdown() {
    const dropdown = document.getElementById("searchResultsDropdown");
    if (dropdown) dropdown.classList.remove("open");
}

function clearGlobalSearch() {
    const input = document.getElementById("globalSearchInput");
    if (input) input.value = "";
    document.getElementById("searchClearBtn").style.display = "none";
    closeSearchDropdown();
}

function initGlobalSearch() {
    document.addEventListener("click", (e) => {
        const wrap = document.querySelector(".sidebar-search");
        if (wrap && !wrap.contains(e.target)) closeSearchDropdown();
    });
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") closeSearchDropdown();
    });
}

// ═══════════════════════════════════════════════════════════════════
// SAVED VIEWS  (compact bar above a filterable table)
// ═══════════════════════════════════════════════════════════════════

async function loadSavedViewsBar(viewType, barId, filterGroupId, applyFn) {
    const bar = document.getElementById(barId);
    if (!bar) return;
    try {
        const res  = await fetch(`/api/saved-views?view_type=${viewType}`);
        const rows = await res.json();
        bar.innerHTML = rows.map(v => `
            <span class="saved-view-chip" onclick="applySavedView('${applyFn}', ${JSON.stringify(v.filter).replace(/"/g, '&quot;')})">
                <i class="ti ti-bookmark" style="font-size:12px"></i> ${escapeHtml(v.view_name)}
                <span class="svc-x" onclick="event.stopPropagation();deleteSavedView(${v.id}, '${viewType}', '${barId}', '${filterGroupId}', '${applyFn}')"><i class="ti ti-x" style="font-size:11px"></i></span>
            </span>`).join("") + `
            <span class="saved-view-add">
                <input type="text" placeholder="Name this view…" id="saveViewName-${viewType}" onkeydown="if(event.key==='Enter')saveCurrentView('${viewType}','${barId}','${filterGroupId}','${applyFn}')" />
                <button class="icon-btn" title="Save current filter as a view" onclick="saveCurrentView('${viewType}','${barId}','${filterGroupId}','${applyFn}')"><i class="ti ti-plus"></i></button>
            </span>`;
    } catch (err) {
        console.error("Saved views load error:", err);
    }
}

function applySavedView(applyFn, filter) {
    if (applyFn === "orders") {
        const status = filter.status || "all";
        filterTable(status, document.querySelector(`#filterGroup .filter-btn[data-status="${CSS.escape(status)}"]`));
    } else if (applyFn === "purchase_orders") {
        const status = filter.status || "all";
        filterPOTable(status, document.querySelector(`#poFilterGroup .filter-btn[data-status="${CSS.escape(status)}"]`));
    } else if (applyFn === "quality") {
        const status = filter.status || "all";
        filterQCTable(status, document.querySelector(`#qcFilterGroup .filter-btn[data-status="${CSS.escape(status)}"]`));
    }
}

async function saveCurrentView(viewType, barId, filterGroupId, applyFn) {
    const input = document.getElementById(`saveViewName-${viewType}`);
    const name  = input?.value.trim();
    if (!name) { input?.focus(); return; }
    const activeBtn = document.querySelector(`#${filterGroupId} .filter-btn.active`);
    const status = activeBtn?.dataset.status || "all";
    try {
        const res = await fetch("/api/saved-views", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ view_name: name, view_type: viewType, filter: { status } }),
        });
        if (res.ok) loadSavedViewsBar(viewType, barId, filterGroupId, applyFn);
    } catch (err) { console.error(err); }
}

async function deleteSavedView(id, viewType, barId, filterGroupId, applyFn) {
    try {
        await fetch(`/api/saved-views/${id}`, { method: "DELETE" });
        loadSavedViewsBar(viewType, barId, filterGroupId, applyFn);
    } catch (err) { console.error(err); }
}

// ═══════════════════════════════════════════════════════════════════
// CSV IMPORT  (export is a plain <a href>, no JS needed)
// ═══════════════════════════════════════════════════════════════════

async function importInventoryCsv(input) {
    const msg = document.getElementById("csvImportMsg");
    const file = input.files[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    msg.style.color = "var(--text-muted)";
    msg.textContent = "Importing…";
    try {
        const res  = await fetch("/api/inventory/import-csv", { method: "POST", body: form });
        const data = await res.json();
        msg.style.color = res.ok ? "var(--good-text)" : "var(--critical-text)";
        msg.textContent = data.message;
        if (res.ok) { loadInventoryView(); loadDashboard(); }
    } catch (err) {
        msg.style.color = "var(--critical-text)"; msg.textContent = "Error: " + err.message;
    } finally {
        input.value = "";
    }
}

// ═══════════════════════════════════════════════════════════════════
// INTEGRATIONS — Webhooks / Backups / Barcode Labels
// ═══════════════════════════════════════════════════════════════════

function setIntTab(tab, btn) {
    document.querySelectorAll("#intTabs .demand-tab").forEach(b => b.classList.remove("active"));
    if (btn) btn.classList.add("active");
    document.querySelectorAll("#view-integrations .demand-tab-panel").forEach(p => p.classList.remove("active"));
    document.getElementById("int-panel-" + tab)?.classList.add("active");
}

async function loadIntegrations() {
    try {
        await Promise.all([loadWebhooks(), loadWebhookLog(), loadBackups(), loadLabelSelect()]);
    } catch (err) {
        console.error("Integrations load error:", err);
    }
}

async function loadWebhooks() {
    const tbody = document.getElementById("webhooksTableBody");
    if (!tbody) return;
    try {
        const res  = await fetch("/api/webhooks");
        const rows = await res.json();
        if (!rows.length) {
            tbody.innerHTML = `<tr><td colspan="5" class="table-empty">No webhooks registered yet.</td></tr>`;
            return;
        }
        tbody.innerHTML = rows.map(w => `
            <tr>
                <td class="mono" style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(w.url)}">${escapeHtml(w.url)}</td>
                <td>${escapeHtml(w.event_type)}</td>
                <td>${w.has_secret ? "✅ Signed" : "—"}</td>
                <td><button class="btn-secondary" style="font-size:11px;padding:5px 10px" onclick="testWebhook(${w.id})"><i class="ti ti-send"></i></button></td>
                <td><button class="table-action-btn" onclick="deleteWebhook(${w.id})"><i class="ti ti-trash"></i></button></td>
            </tr>`).join("");
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="5" class="table-empty">Error loading webhooks.</td></tr>`;
    }
}

async function addWebhook() {
    const msg = document.getElementById("whAddMsg");
    const payload = {
        url:        document.getElementById("wh-url").value.trim(),
        event_type: document.getElementById("wh-event").value,
        secret:     document.getElementById("wh-secret").value.trim(),
    };
    if (!payload.url) { msg.style.color = "var(--critical-text)"; msg.textContent = "❌ URL is required"; return; }
    try {
        const res  = await fetch("/api/webhooks", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
        const data = await res.json();
        msg.style.color = res.ok ? "var(--good-text)" : "var(--critical-text)";
        msg.textContent = data.message;
        if (res.ok) {
            document.getElementById("wh-url").value = "";
            document.getElementById("wh-secret").value = "";
            loadWebhooks();
        }
    } catch (err) {
        msg.style.color = "var(--critical-text)"; msg.textContent = "Error: " + err.message;
    }
}

async function deleteWebhook(id) {
    try { await fetch(`/api/webhooks/${id}`, { method: "DELETE" }); loadWebhooks(); }
    catch (err) { console.error(err); }
}

async function testWebhook(id) {
    try {
        const res  = await fetch(`/api/webhooks/${id}/test`, { method: "POST" });
        const data = await res.json();
        alert(data.message);
        loadWebhookLog();
    } catch (err) { alert("Error: " + err.message); }
}

async function loadWebhookLog() {
    const tbody = document.getElementById("webhookLogTableBody");
    if (!tbody) return;
    try {
        const res  = await fetch("/api/webhooks/log");
        const rows = await res.json();
        if (!rows.length) {
            tbody.innerHTML = `<tr><td colspan="4" class="table-empty">No deliveries logged yet.</td></tr>`;
            return;
        }
        tbody.innerHTML = rows.map(r => {
            const ok = r.status_code && r.status_code < 300;
            const result = r.error
                ? `<span class="badge badge-critical"><span class="badge-dot"></span>Error</span>`
                : `<span class="badge ${ok ? 'badge-accepted' : 'badge-critical'}"><span class="badge-dot"></span>HTTP ${r.status_code ?? "—"}</span>`;
            return `
            <tr>
                <td class="mono" style="font-size:11px">${(r.created_at || "").replace("T", " ").slice(0, 16)}</td>
                <td>${escapeHtml(r.event_type)}</td>
                <td class="mono" style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(r.url || '')}">${escapeHtml(r.url || "—")}</td>
                <td title="${escapeHtml(r.error || '')}">${result}</td>
            </tr>`;
        }).join("");
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="4" class="table-empty">Error loading log.</td></tr>`;
    }
}

async function loadBackups() {
    const tbody = document.getElementById("backupsTableBody");
    if (!tbody) return;
    try {
        const res  = await fetch("/api/backups");
        const rows = await res.json();
        if (!rows.length) {
            tbody.innerHTML = `<tr><td colspan="4" class="table-empty">No backups yet — click "Back Up Now" or wait for the scheduled run.</td></tr>`;
            return;
        }
        tbody.innerHTML = rows.map(b => `
            <tr>
                <td class="mono">${escapeHtml(b.filename)}</td>
                <td class="mono" style="font-size:11px">${(b.created_at || "").replace("T", " ").slice(0, 16)}</td>
                <td>${(b.size_bytes / 1024).toFixed(1)} KB</td>
                <td><a class="btn-secondary" style="font-size:11px;padding:5px 10px" href="/api/backups/${encodeURIComponent(b.filename)}"><i class="ti ti-download"></i></a></td>
            </tr>`).join("");
    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="4" class="table-empty">Error loading backups.</td></tr>`;
    }
}

async function runBackupNow() {
    const msg = document.getElementById("backupRunMsg");
    msg.style.color = "var(--text-muted)"; msg.textContent = "Running…";
    try {
        const res  = await fetch("/api/backups/run", { method: "POST" });
        const data = await res.json();
        msg.style.color = res.ok ? "var(--good-text)" : "var(--critical-text)";
        msg.textContent = data.message;
        loadBackups();
    } catch (err) {
        msg.style.color = "var(--critical-text)"; msg.textContent = "Error: " + err.message;
    }
}

async function loadLabelSelect() {
    const grid = document.getElementById("labelSelectGrid");
    if (!grid) return;
    try {
        const res = await fetch("/api/inventory");
        const inv = await res.json();
        if (!inv.length) {
            grid.innerHTML = `<div class="table-empty">No inventory items yet.</div>`;
            return;
        }
        grid.innerHTML = inv.map(i => `
            <label class="label-select-item">
                <input type="checkbox" value="${i.id}" class="label-select-cb" />
                ${escapeHtml(i.part_name)}
            </label>`).join("");
    } catch (err) {
        grid.innerHTML = `<div class="table-empty">Error loading items.</div>`;
    }
}

function selectAllLabels(state) {
    document.querySelectorAll(".label-select-cb").forEach(cb => { cb.checked = state; });
}

function printSelectedLabels() {
    const msg = document.getElementById("labelPrintMsg");
    const ids = Array.from(document.querySelectorAll(".label-select-cb:checked")).map(cb => cb.value);
    if (!ids.length) {
        msg.style.color = "var(--critical-text)"; msg.textContent = "❌ Select at least one item";
        return;
    }
    msg.style.color = "var(--good-text)"; msg.textContent = `Opening PDF for ${ids.length} item(s)…`;
    window.open(`/api/labels/print?inventory_ids=${ids.join(",")}`, "_blank");
}

// ═══════════════════════════════════════════════════════════════════
// THEME TOGGLE  (dark default, light opt-in, persisted in localStorage)
// ═══════════════════════════════════════════════════════════════════

function applyTheme(theme) {
    const icon = document.getElementById("themeToggleIcon");
    const text = document.getElementById("themeToggleText");
    if (theme === "light") {
        document.documentElement.setAttribute("data-theme", "light");
        if (icon) icon.className = "ti ti-sun";
        if (text) text.textContent = "Light";
    } else {
        document.documentElement.removeAttribute("data-theme");
        if (icon) icon.className = "ti ti-moon";
        if (text) text.textContent = "Dark";
    }
}

function toggleTheme() {
    const isLight = document.documentElement.getAttribute("data-theme") === "light";
    const next = isLight ? "dark" : "light";
    applyTheme(next);
    try { localStorage.setItem("orderflow-theme", next); } catch (e) {}
}

function initTheme() {
    let saved = null;
    try { saved = localStorage.getItem("orderflow-theme"); } catch (e) {}
    // Light is the default look (the Zentra theme); dark is the violet one.
    // An explicit saved choice always wins.
    applyTheme(saved === "dark" ? "dark" : "light");
}

// ── Dashboard ask bar → the real chat assistant ─────────────────────
// Hands the typed question to the existing chat pipeline rather than
// duplicating any of it: switch to the Chat view, drop the text into the
// chat input, and send it exactly as if the user had typed it there.
function askFromDashboard(preset) {
    const bar = document.getElementById("dashAsk");
    const text = (preset || (bar ? bar.value : "")).trim();
    if (!text) return;

    showView("chat");
    const chatInput = document.getElementById("chatInput");
    if (chatInput) {
        chatInput.value = text;
        if (bar) bar.value = "";
        sendMessage();
    }
}

// The coral quick-action: routes through the same assistant pipeline, which
// answers this one from inventory data even when the AI server is offline.
function showLowStock() {
    askFromDashboard("What's running low?");
}

// ── Hero date badge ─────────────────────────────────────────────────
function renderHeroDate() {
    const dayEl  = document.getElementById("heroDay");
    const dateEl = document.getElementById("heroDate");
    if (!dayEl || !dateEl) return;
    const now = new Date();
    dayEl.textContent = now.getDate();
    dateEl.innerHTML =
        `${now.toLocaleDateString(undefined, { weekday: "short" })},` +
        `<span>${now.toLocaleDateString(undefined, { month: "long" })}</span>`;
}

// ── Hero mic ────────────────────────────────────────────────────────
// Its own recognition instance so it can drop the transcript into the
// dashboard ask bar without disturbing the chat view's mic.
let heroRecognition = null, heroRecognizing = false;

function toggleHeroVoice() {
    const btn = document.getElementById("heroMic");
    const bar = document.getElementById("dashAsk");
    if (!btn || !bar) return;

    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
        btn.classList.add("unsupported");
        btn.title = "Voice input isn't supported in this browser";
        return;
    }

    if (!heroRecognition) {
        heroRecognition = new SpeechRecognition();
        heroRecognition.continuous = false;
        heroRecognition.interimResults = false;
        heroRecognition.lang = "en-US";

        heroRecognition.onresult = (event) => {
            const transcript = event.results[0][0].transcript;
            bar.value = transcript;
            askFromDashboard();           // speak it, and it's asked
        };
        const stop = () => {
            heroRecognizing = false;
            btn.classList.remove("recording");
        };
        heroRecognition.onend = stop;
        heroRecognition.onerror = stop;
    }

    if (heroRecognizing) {
        heroRecognition.stop();
        heroRecognizing = false;
        btn.classList.remove("recording");
    } else {
        try {
            heroRecognition.start();
            heroRecognizing = true;
            btn.classList.add("recording");
        } catch (e) {
            heroRecognizing = false;
            btn.classList.remove("recording");
        }
    }
}

// ═══════════════════════════════════════════════════════════════════
// CHAT — voice input (Web Speech API) & new conversation
// ═══════════════════════════════════════════════════════════════════

let recognition = null;
let recognizing = false;
let initialChatHTML = "";

function initVoiceInput() {
    const micBtn = document.getElementById("micBtn");
    const hint   = document.getElementById("voiceHint");
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
        if (micBtn) { micBtn.classList.add("unsupported"); micBtn.title = "Voice input isn't supported in this browser"; }
        return;
    }
    recognition = new SpeechRecognition();
    recognition.continuous = false;
    recognition.interimResults = false;
    recognition.lang = "en-US";

    recognition.onresult = (event) => {
        const transcript = event.results[0][0].transcript;
        const input = document.getElementById("chatInput");
        if (input) {
            input.value = (input.value ? input.value + " " : "") + transcript;
            autoResize(input);
            input.focus();
        }
    };
    recognition.onend = () => {
        recognizing = false;
        micBtn?.classList.remove("recording");
        if (hint) hint.textContent = "";
    };
    recognition.onerror = () => {
        recognizing = false;
        micBtn?.classList.remove("recording");
        if (hint) hint.textContent = "";
    };
}

function toggleVoiceInput() {
    if (!recognition) return;
    const micBtn = document.getElementById("micBtn");
    const hint   = document.getElementById("voiceHint");
    if (recognizing) {
        recognition.stop();
        recognizing = false;
        micBtn?.classList.remove("recording");
        if (hint) hint.textContent = "";
    } else {
        try {
            recognition.start();
            recognizing = true;
            micBtn?.classList.add("recording");
            if (hint) hint.textContent = " · Listening…";
        } catch (e) { /* already started */ }
    }
}

async function startNewConversation() {
    try {
        await fetch("/api/chat/clear", { method: "POST" });
    } catch (err) { console.error(err); }
    const container = document.getElementById("chatMessages");
    if (container && initialChatHTML) container.innerHTML = initialChatHTML;
}

// ═══════════════════════════════════════════════════════
// INIT
// ═══════════════════════════════════════════════════════

window.onload = function () {
    loadDashboard();
    checkAuth();
    updateRFIDStatus("listening");
    pollSystemStatus();
    setInterval(pollSystemStatus, 15000);
    initTheme();
    initGlobalSearch();
    initVoiceInput();
    const chatContainer = document.getElementById("chatMessages");
    if (chatContainer) initialChatHTML = chatContainer.innerHTML;
    loadSavedViewsBar("orders", "ordersSavedViewsBar", "filterGroup", "orders");
    loadSavedViewsBar("purchase_orders", "poSavedViewsBar", "poFilterGroup", "purchase_orders");
    loadSavedViewsBar("quality", "qcSavedViewsBar", "qcFilterGroup", "quality");
    document.getElementById("chatInput")?.focus();
};
