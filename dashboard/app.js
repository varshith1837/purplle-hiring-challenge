/**
 * Store Intelligence — Live Dashboard
 * Polls the FastAPI backend every 2 seconds and renders real-time analytics.
 */

// ─── Configuration ───
const CONFIG = {
    API_BASE: window.SI_API_BASE || 'http://localhost:8000',
    POLL_INTERVAL: 2000,
    MAX_FEED_ITEMS: 50,
    STORE_ID: 'STORE_BLR_001',
};

// ─── State ───
const state = {
    connected: false,
    lastMetrics: null,
    lastFunnel: null,
    lastHeatmap: null,
    lastAnomalies: null,
    feedEvents: [],
    pollTimer: null,
};

// ─── DOM References ───
const $ = (id) => document.getElementById(id);

// ═══════════════════════════════════════════════════════════════════════════
// UTILITIES
// ═══════════════════════════════════════════════════════════════════════════

function formatPct(value) {
    if (value == null || isNaN(value)) return '0.0%';
    return (value * 100).toFixed(1) + '%';
}

function formatDwell(ms) {
    if (!ms || ms <= 0) return '0s';
    const totalSeconds = Math.round(ms / 1000);
    if (totalSeconds < 60) return `${totalSeconds}s`;
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    return seconds > 0 ? `${minutes}m ${seconds}s` : `${minutes}m`;
}

function formatNumber(n) {
    if (n == null) return '—';
    return Number(n).toLocaleString();
}

function formatTime(isoStr) {
    if (!isoStr) return '—';
    try {
        const d = new Date(isoStr.replace('Z', '+00:00'));
        return d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } catch {
        return isoStr;
    }
}

function formatUptime(seconds) {
    if (!seconds) return '—';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
}

function shortVisitorId(vid) {
    if (!vid) return '';
    return vid.length > 10 ? vid.slice(0, 8) + '…' : vid;
}

// Animate number transitions
function animateValue(el, newValue) {
    const currentText = el.textContent;
    if (currentText === newValue) return;
    el.textContent = newValue;
    el.classList.remove('number-updated');
    // Force reflow for animation restart
    void el.offsetWidth;
    el.classList.add('number-updated');
}

// ═══════════════════════════════════════════════════════════════════════════
// CONNECTION STATUS
// ═══════════════════════════════════════════════════════════════════════════

function setConnectionStatus(status, text) {
    const dot = $('status-dot');
    const label = $('status-text');
    dot.className = 'status-dot';
    if (status === 'connected') {
        dot.classList.add('connected');
        label.textContent = text || 'Connected';
    } else if (status === 'error') {
        dot.classList.add('error');
        label.textContent = text || 'Disconnected';
    } else {
        label.textContent = text || 'Connecting…';
    }
}

function updateLastRefresh() {
    $('update-time').textContent = new Date().toLocaleTimeString('en-IN', {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
    });
}

// ═══════════════════════════════════════════════════════════════════════════
// API CALLS
// ═══════════════════════════════════════════════════════════════════════════

async function apiFetch(path) {
    const resp = await fetch(`${CONFIG.API_BASE}${path}`, {
        headers: { 'Accept': 'application/json' },
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
}

async function fetchAll() {
    const storeId = CONFIG.STORE_ID;
    try {
        const [metrics, funnel, heatmap, anomalies, health] = await Promise.all([
            apiFetch(`/stores/${storeId}/metrics`),
            apiFetch(`/stores/${storeId}/funnel`),
            apiFetch(`/stores/${storeId}/heatmap`),
            apiFetch(`/stores/${storeId}/anomalies`),
            apiFetch('/health'),
        ]);

        state.connected = true;
        setConnectionStatus('connected');
        updateLastRefresh();

        renderMetrics(metrics);
        renderFunnel(funnel);
        renderHeatmap(heatmap);
        renderAnomalies(anomalies);
        renderHealth(health);

        state.lastMetrics = metrics;
        state.lastFunnel = funnel;
        state.lastHeatmap = heatmap;
        state.lastAnomalies = anomalies;

    } catch (err) {
        console.warn('Dashboard fetch error:', err.message);
        state.connected = false;
        setConnectionStatus('error', `Error: ${err.message}`);
    }
}

// Also fetch latest events for the feed
async function fetchEvents() {
    try {
        const storeId = CONFIG.STORE_ID;
        const events = await apiFetch(`/stores/${storeId}/events?limit=20`);
        if (Array.isArray(events)) {
            renderEventFeed(events);
        }
    } catch {
        // Event feed is best-effort; don't break dashboard
    }
}

// ═══════════════════════════════════════════════════════════════════════════
// RENDERERS
// ═══════════════════════════════════════════════════════════════════════════

function renderMetrics(m) {
    animateValue($('kpi-visitors-value'), formatNumber(m.unique_visitors));
    animateValue($('kpi-conversion-value'), formatPct(m.conversion_rate));
    animateValue($('kpi-queue-value'), formatNumber(m.current_queue_depth));
    animateValue($('kpi-abandonment-value'), formatPct(m.abandonment_rate));
    animateValue($('kpi-transactions-value'), formatNumber(m.total_transactions));
}

function renderFunnel(f) {
    $('funnel-sessions').textContent = `${formatNumber(f.total_sessions)} sessions`;

    const stages = f.stages || [];
    const maxCount = stages.length > 0 ? stages[0].count : 1;

    const stageMap = {
        'Entry': { fill: 'funnel-fill-entry', count: 'funnel-count-entry', drop: 'funnel-drop-entry' },
        'Zone Visit': { fill: 'funnel-fill-zone', count: 'funnel-count-zone', drop: 'funnel-drop-zone' },
        'Billing Queue': { fill: 'funnel-fill-billing', count: 'funnel-count-billing', drop: 'funnel-drop-billing' },
        'Purchase': { fill: 'funnel-fill-purchase', count: 'funnel-count-purchase', drop: 'funnel-drop-purchase' },
    };

    stages.forEach((stage) => {
        const ids = stageMap[stage.stage];
        if (!ids) return;
        const pct = maxCount > 0 ? Math.max(2, (stage.count / maxCount) * 100) : 2;
        const fillEl = $(ids.fill);
        const countEl = $(ids.count);
        const dropEl = $(ids.drop);
        if (fillEl) fillEl.style.width = `${pct}%`;
        if (countEl) animateValue(countEl, formatNumber(stage.count));
        if (dropEl) {
            if (stage.drop_off_pct > 0) {
                dropEl.textContent = `▼ ${stage.drop_off_pct.toFixed(1)}%`;
            } else {
                dropEl.textContent = '';
            }
        }
    });
}

function renderHeatmap(h) {
    const grid = $('heatmap-grid');
    const zones = h.zones || [];

    if (zones.length === 0) {
        grid.innerHTML = '<div class="anomaly-empty"><p>No zone data yet</p></div>';
        return;
    }

    grid.innerHTML = '';

    zones.forEach((zone) => {
        const cell = document.createElement('div');
        cell.className = 'heatmap-cell';

        // Color intensity based on normalised 0–100
        const intensity = zone.intensity || 0;
        const hue = intensity > 60 ? 0 : intensity > 30 ? 270 : 220;
        const saturation = 70 + intensity * 0.3;
        const lightness = 20 + intensity * 0.3;
        const alpha = 0.15 + (intensity / 100) * 0.45;

        cell.style.background = `hsla(${hue}, ${saturation}%, ${lightness}%, ${alpha})`;
        cell.style.borderColor = `hsla(${hue}, ${saturation}%, ${lightness + 20}%, 0.3)`;

        cell.innerHTML = `
            <span class="heatmap-confidence">${zone.data_confidence || ''}</span>
            <div class="heatmap-zone-name">${escapeHtml(zone.zone_name)}</div>
            <div class="heatmap-visits">${formatNumber(zone.visit_count)}</div>
            <div class="heatmap-dwell">⏱ ${formatDwell(zone.avg_dwell_ms)}</div>
        `;
        grid.appendChild(cell);
    });
}

function renderAnomalies(a) {
    const list = $('anomaly-list');
    const countBadge = $('anomaly-count');
    const anomalies = a.active_anomalies || [];

    countBadge.textContent = anomalies.length;
    countBadge.className = `panel-badge anomaly-count ${anomalies.length === 0 ? 'zero' : ''}`;

    if (anomalies.length === 0) {
        list.innerHTML = `
            <div class="anomaly-empty" id="anomaly-empty">
                <span class="empty-icon">✅</span>
                <p>No anomalies detected</p>
            </div>`;
        return;
    }

    list.innerHTML = '';
    anomalies.forEach((an) => {
        const card = document.createElement('div');
        card.className = `anomaly-card severity-${an.severity}`;
        card.innerHTML = `
            <div class="anomaly-header">
                <span class="anomaly-type">${escapeHtml(an.anomaly_type.replace(/_/g, ' '))}</span>
                <span class="anomaly-severity ${an.severity}">${an.severity}</span>
            </div>
            <div class="anomaly-detail">${escapeHtml(an.detail)}</div>
            <div class="anomaly-action">💡 ${escapeHtml(an.suggested_action)}</div>
            <div class="anomaly-time">${formatTime(an.detected_at)}</div>
        `;
        list.appendChild(card);
    });
}

function renderEventFeed(events) {
    const feed = $('event-feed');
    if (!events || events.length === 0) return;

    feed.innerHTML = '';
    const displayEvents = events.slice(-CONFIG.MAX_FEED_ITEMS).reverse();

    displayEvents.forEach((ev) => {
        const row = document.createElement('div');
        row.className = 'feed-row';
        row.innerHTML = `
            <span class="feed-time">${formatTime(ev.timestamp)}</span>
            <span class="feed-type ${ev.event_type}">${ev.event_type}</span>
            <span class="feed-zone">${ev.zone_id || '—'}</span>
            <span class="feed-visitor">${shortVisitorId(ev.visitor_id)}</span>
        `;
        feed.appendChild(row);
    });
}

function renderHealth(h) {
    $('footer-uptime').textContent = `Uptime: ${formatUptime(h.uptime_seconds)}`;
    $('footer-db-status').textContent = `DB: ${h.database || '—'}`;
}

// ─── XSS Protection ───
function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

// ═══════════════════════════════════════════════════════════════════════════
// STORE SELECTOR
// ═══════════════════════════════════════════════════════════════════════════

function initStoreSelector() {
    const select = $('store-selector');
    select.addEventListener('change', (e) => {
        CONFIG.STORE_ID = e.target.value;
        // Reset state
        state.lastMetrics = null;
        state.lastFunnel = null;
        state.lastHeatmap = null;
        state.lastAnomalies = null;
        state.feedEvents = [];
        // Re-fetch immediately
        fetchAll();
        fetchEvents();
    });
}

// ═══════════════════════════════════════════════════════════════════════════
// POLLING LOOP
// ═══════════════════════════════════════════════════════════════════════════

function startPolling() {
    // Initial fetch
    fetchAll();
    fetchEvents();

    // Interval
    state.pollTimer = setInterval(() => {
        fetchAll();
        fetchEvents();
    }, CONFIG.POLL_INTERVAL);
}

function stopPolling() {
    if (state.pollTimer) {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
    }
}

// ═══════════════════════════════════════════════════════════════════════════
// INIT
// ═══════════════════════════════════════════════════════════════════════════

document.addEventListener('DOMContentLoaded', () => {
    initStoreSelector();
    setConnectionStatus('connecting', 'Connecting…');
    startPolling();
});

// Cleanup on page hide
document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
        stopPolling();
    } else {
        startPolling();
    }
});
