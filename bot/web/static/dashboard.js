/**
 * SniperX — Dashboard Logic
 * Auto-switches to symbol/timeframe when signal detected
 */

const TIMEFRAMES = ['M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1'];

const state = {
    connected: false,
    symbols: [],
    activeSymbol: null,
    activeTimeframe: 'M1',
    zones: [],
    positions: [],
    stats: { total_r: 0, wins: 0, losses: 0, trades: 0, win_rate: 0 },
    signals: [],
    candles: {},
    ws: null,
    chartReady: false,
    lastSignalCount: 0,
};

// ─── Init ───
window.addEventListener('load', () => {
    setTimeout(() => {
        initChart('chart');
        state.chartReady = true;
        initTimeframeTabs();
        fetchInitialData();
        connectWebSocket();
        startClock();
    }, 200);
});

// ─── Timeframe Tabs ───
function initTimeframeTabs() {
    document.querySelectorAll('.tf-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const tf = btn.getAttribute('data-tf');
            switchTimeframe(tf);
        });
    });
    // Set initial active
    updateTimeframeUI();
}

function switchTimeframe(tf) {
    state.activeTimeframe = tf;
    updateTimeframeUI();
    if (state.activeSymbol) {
        fetchCandles(state.activeSymbol, tf);
    }
}

function updateTimeframeUI() {
    document.querySelectorAll('.tf-btn').forEach(btn => {
        btn.classList.toggle('active', btn.getAttribute('data-tf') === state.activeTimeframe);
    });
}

// ─── Data Fetching ───
async function fetchInitialData() {
    try {
        const res = await fetch('/api/data');
        const data = await res.json();
        state.symbols = data.symbols || [];
        state.zones = data.zones || [];
        state.positions = data.positions || [];
        state.stats = data.stats || state.stats;
        state.signals = data.signals || [];
        state.lastSignalCount = state.signals.length;

        renderSymbolTabs();
        if (state.symbols.length > 0) {
            switchSymbol(state.symbols[0]);
        }
        updateUI();
    } catch (e) {
        console.error('Failed to fetch initial data:', e);
    }
}

async function fetchCandles(symbol, timeframe) {
    timeframe = timeframe || state.activeTimeframe;
    try {
        const res = await fetch('/api/candles/' + encodeURIComponent(symbol) + '?timeframe=' + timeframe + '&count=500');
        const data = await res.json();
        const key = symbol + '_' + timeframe;
        state.candles[key] = data.candles || [];

        if (symbol === state.activeSymbol && state.chartReady) {
            updateCandles(state.candles[key]);
        }
    } catch (e) {
        console.error('Failed to fetch candles:', e);
    }
}

// ─── WebSocket ───
function connectWebSocket() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = proto + '://' + location.host + '/ws';
    state.ws = new WebSocket(url);

    state.ws.onopen = () => {
        state.connected = true;
        updateConnectionStatus();
    };

    state.ws.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            if (data.type === 'update') {
                state.zones = data.zones || state.zones;
                state.positions = data.positions || state.positions;
                state.stats = data.stats || state.stats;

                // Check for new signals — auto switch to signal chart
                if (data.signals && data.signals.length > 0) {
                    const newSignals = data.signals.filter(sig =>
                        !state.signals.find(s => s.time === sig.time && s.symbol === sig.symbol)
                    );
                    newSignals.forEach(sig => state.signals.unshift(sig));
                    state.signals = state.signals.slice(0, 50);

                    // AUTO-SWITCH: if new signal detected, jump to that chart
                    if (newSignals.length > 0) {
                        const latest = newSignals[0];
                        if (latest.symbol && latest.symbol !== state.activeSymbol) {
                            showNotification('SIGNAL: ' + (latest.direction || '').toUpperCase() + ' ' + latest.symbol, latest.direction);
                            switchSymbol(latest.symbol);
                        }
                    }
                }

                updateUI();
                document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString();
            }
        } catch (e) {
            console.error('WS parse error:', e);
        }
    };

    state.ws.onclose = () => {
        state.connected = false;
        updateConnectionStatus();
        setTimeout(connectWebSocket, 3000);
    };

    state.ws.onerror = () => {
        state.connected = false;
        updateConnectionStatus();
    };
}

// ─── Notification ───
function showNotification(message, type) {
    // Browser notification
    if (Notification.permission === 'granted') {
        new Notification('SniperX', { body: message, icon: '/static/icon.png' });
    } else if (Notification.permission !== 'denied') {
        Notification.requestPermission();
    }

    // On-screen flash
    const flash = document.createElement('div');
    flash.style.cssText = 'position:fixed;top:60px;left:50%;transform:translateX(-50%);padding:12px 24px;border-radius:8px;font-size:14px;font-weight:700;z-index:9999;animation:fadeout 4s forwards;font-family:monospace;';
    flash.style.background = type === 'buy' ? 'rgba(34,197,94,0.9)' : type === 'sell' ? 'rgba(239,68,68,0.9)' : 'rgba(59,130,246,0.9)';
    flash.style.color = '#fff';
    flash.textContent = message;
    document.body.appendChild(flash);
    setTimeout(() => flash.remove(), 4000);
}

// ─── UI Updates ───
function updateUI() {
    updatePerformance();
    updateZoneList();
    updatePositionList();
    updateSignalLog();
    updateSymbolIndicators();
    updateChartOverlays();
}

function updateConnectionStatus() {
    const dot = document.getElementById('statusDot');
    const text = document.getElementById('statusText');
    if (state.connected) {
        dot.classList.add('connected');
        text.textContent = 'Connected';
    } else {
        dot.classList.remove('connected');
        text.textContent = 'Disconnected';
    }
}

function updatePerformance() {
    const s = state.stats;
    const val = s.total_r || 0;
    const totalR = document.getElementById('totalR');
    totalR.textContent = (val >= 0 ? '+' : '') + val.toFixed(1) + 'R';
    totalR.className = 'perf-value ' + (val >= 0 ? 'positive' : 'negative');
    document.getElementById('winRate').textContent = (s.win_rate || 0).toFixed(0) + '%';
    document.getElementById('tradeCount').textContent = s.trades || 0;
    document.getElementById('openCount').textContent =
        state.positions.filter(p => p.status === 'open' || p.status === 'partial').length;
    document.getElementById('bottomR').textContent =
        'Total: ' + (val >= 0 ? '+' : '') + val.toFixed(1) + 'R';
}

function renderSymbolTabs() {
    const container = document.getElementById('symbolTabs');
    container.innerHTML = '';
    state.symbols.forEach(sym => {
        const tab = document.createElement('div');
        tab.className = 'symbol-tab' + (sym === state.activeSymbol ? ' active' : '');
        tab.textContent = sym;
        tab.onclick = () => switchSymbol(sym);
        container.appendChild(tab);
    });
}

function switchSymbol(symbol) {
    state.activeSymbol = symbol;
    document.querySelectorAll('.symbol-tab').forEach(t => {
        t.classList.toggle('active', t.textContent.trim() === symbol);
    });
    fetchCandles(symbol, state.activeTimeframe);
    updateChartOverlays();
}

function updateSymbolIndicators() {
    document.querySelectorAll('.symbol-tab').forEach(tab => {
        const sym = tab.textContent.trim();
        const existing = tab.querySelector('.signal-dot');
        if (existing) existing.remove();

        const activeZone = state.zones.find(z => z.symbol === sym && z.status === 'active');
        if (activeZone) {
            const dot = document.createElement('span');
            dot.className = 'signal-dot ' + activeZone.type;
            tab.appendChild(dot);
        }
    });
}

function updateZoneList() {
    const container = document.getElementById('zoneList');
    const filtered = state.zones.filter(z => !state.activeSymbol || z.symbol === state.activeSymbol);

    if (filtered.length === 0) {
        container.innerHTML = '<div class="empty-state">No active zones</div>';
        return;
    }

    container.innerHTML = filtered.map(z => `
        <div class="zone-item ${z.type} ${z.status === 'stale' || z.status === 'broken' ? 'stale' : ''}">
            <div class="zone-header">
                <span class="zone-type ${z.type}">${z.type.toUpperCase()} ZONE</span>
                <span style="font-size:10px;color:var(--text-secondary)">${(z.confidence||0).toFixed(0)}%</span>
            </div>
            <div class="zone-detail">
                <span>${z.high.toFixed(5)} — ${z.low.toFixed(5)}</span>
                <span>
                    ${z.gap_count > 0 ? '<span class="gap-badge">GAP ' + z.gap_count + '</span>' : ''}
                    <span class="pin-badge">PIN:${z.pin_count}</span>
                </span>
            </div>
            <div class="zone-scores">
                <span>BRK: ${(z.breakout_magnitude||0)}x</span>
                <span>TRI: ${(z.triangular_score||0).toFixed(0)}</span>
                <span>FRS: ${(z.white_space_score||0).toFixed(0)}</span>
            </div>
        </div>
    `).join('');
}

function updatePositionList() {
    const container = document.getElementById('positionList');
    const filtered = state.positions.filter(p =>
        (p.status === 'open' || p.status === 'partial') &&
        (!state.activeSymbol || p.symbol === state.activeSymbol)
    );

    if (filtered.length === 0) {
        container.innerHTML = '<div class="empty-state">No open positions</div>';
        return;
    }

    container.innerHTML = filtered.map(p => `
        <div class="position-item ${p.direction}">
            <div class="pos-header">
                <span class="pos-dir ${p.direction}">${p.direction.toUpperCase()} ${p.symbol}</span>
                <span class="pos-r" style="color:${p.r_result >= 0 ? 'var(--green)' : 'var(--red)'}">
                    ${p.r_result >= 0 ? '+' : ''}${p.r_result.toFixed(2)}R
                </span>
            </div>
            <div class="pos-detail">
                <span>Entry: ${p.entry_price.toFixed(5)}</span>
                <span>SL: ${p.stop_loss.toFixed(5)}</span>
            </div>
            <div class="pos-detail">
                <span>TP1: ${p.take_profit_1.toFixed(5)} ${p.tp1_hit ? '<span class="tp1-badge">HIT</span>' : ''}</span>
                <span>TP2: ${p.take_profit_2.toFixed(5)}</span>
            </div>
        </div>
    `).join('');
}

function updateSignalLog() {
    const container = document.getElementById('signalLog');
    if (state.signals.length === 0) {
        container.innerHTML = '<div class="empty-state">Waiting for signals...</div>';
        return;
    }
    container.innerHTML = state.signals.slice(0, 20).map(sig => {
        const time = sig.time ? new Date(sig.time * 1000).toLocaleTimeString() : '--:--';
        return '<div class="signal-entry"><span class="time">' + time + '</span>' +
            (sig.message || '') + '</div>';
    }).join('');
}

function updateChartOverlays() {
    if (!state.chartReady) return;
    const symZones = state.zones.filter(z => z.symbol === state.activeSymbol);
    drawZones(symZones);

    const symPos = state.positions.filter(p =>
        p.symbol === state.activeSymbol && (p.status === 'open' || p.status === 'partial')
    );
    if (symPos.length > 0) drawLevels(symPos[0]);
    else clearLevels();

    const symSig = state.signals.filter(s => s.symbol === state.activeSymbol && s.direction);
    drawSignals(symSig);
}

// ─── Clock ───
function startClock() {
    function tick() { document.getElementById('clock').textContent = new Date().toLocaleTimeString(); }
    tick();
    setInterval(tick, 1000);
}

// Request notification permission on load
if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission();
}

// CSS animation for notifications
const style = document.createElement('style');
style.textContent = '@keyframes fadeout { 0%{opacity:1} 70%{opacity:1} 100%{opacity:0} }';
document.head.appendChild(style);
