/**
 * SniperX — TradingView Lightweight Charts Integration
 */

let chart = null;
let candleSeries = null;
let zoneOverlays = [];
let priceLines = [];

const COLORS = {
    bg: '#0a0a0f',
    grid: '#1a1a2e',
    text: '#8888aa',
    green: '#22c55e',
    greenDim: 'rgba(34,197,94,0.12)',
    red: '#ef4444',
    redDim: 'rgba(239,68,68,0.12)',
    amber: '#f59e0b',
    amberDim: 'rgba(245,158,11,0.10)',
    purple: '#a855f7',
    gray: '#555570',
    grayDim: 'rgba(85,85,112,0.10)',
};

function initChart(containerId) {
    const container = document.getElementById(containerId);
    if (!container) {
        console.error('Chart container not found:', containerId);
        return;
    }

    // Wait for container to have dimensions
    const rect = container.getBoundingClientRect();
    const w = rect.width || 800;
    const h = rect.height || 500;

    chart = LightweightCharts.createChart(container, {
        width: w,
        height: h,
        layout: {
            background: { type: 'solid', color: COLORS.bg },
            textColor: COLORS.text,
            fontFamily: "'JetBrains Mono', monospace",
            fontSize: 11,
        },
        grid: {
            vertLines: { color: COLORS.grid },
            horzLines: { color: COLORS.grid },
        },
        crosshair: {
            mode: LightweightCharts.CrosshairMode.Normal,
            vertLine: { color: 'rgba(136,136,170,0.3)', labelBackgroundColor: '#12121a' },
            horzLine: { color: 'rgba(136,136,170,0.3)', labelBackgroundColor: '#12121a' },
        },
        rightPriceScale: {
            borderColor: '#1e1e3a',
            scaleMargins: { top: 0.05, bottom: 0.05 },
        },
        timeScale: {
            borderColor: '#1e1e3a',
            timeVisible: true,
            secondsVisible: false,
        },
    });

    candleSeries = chart.addCandlestickSeries({
        upColor: COLORS.green,
        downColor: COLORS.red,
        borderUpColor: COLORS.green,
        borderDownColor: COLORS.red,
        wickUpColor: COLORS.green,
        wickDownColor: COLORS.red,
    });

    // Resize observer
    const resizeObserver = new ResizeObserver(entries => {
        for (const entry of entries) {
            const { width, height } = entry.contentRect;
            if (width > 0 && height > 0) {
                chart.applyOptions({ width, height });
            }
        }
    });
    resizeObserver.observe(container);

    console.log('Chart initialized:', w, 'x', h);
    return chart;
}

function updateCandles(candleData) {
    if (!candleSeries || !candleData || candleData.length === 0) {
        console.warn('No candle data to display');
        return;
    }

    // Sort by time and ensure unique timestamps
    const sorted = candleData
        .filter(c => c.time && c.open && c.high && c.low && c.close)
        .sort((a, b) => a.time - b.time);

    // Remove duplicates
    const unique = [];
    let lastTime = 0;
    for (const c of sorted) {
        if (c.time > lastTime) {
            unique.push(c);
            lastTime = c.time;
        }
    }

    if (unique.length === 0) return;

    console.log('Setting', unique.length, 'candles. Last:', unique[unique.length - 1]);
    candleSeries.setData(unique);
    chart.timeScale().fitContent();
}

function addCandle(candle) {
    if (!candleSeries || !candle) return;
    candleSeries.update(candle);
}

function drawZones(zones) {
    clearZones();
    if (!chart || !candleSeries || !zones || zones.length === 0) return;

    zones.forEach(zone => {
        const isActive = zone.status === 'active' || zone.status === 'triggered';
        let color;

        if (zone.status === 'stale' || zone.status === 'broken') {
            color = COLORS.gray;
        } else if (zone.type === 'buy') {
            color = COLORS.green;
        } else {
            color = COLORS.red;
        }

        const topLine = candleSeries.createPriceLine({
            price: zone.high,
            color: color,
            lineWidth: 2,
            lineStyle: LightweightCharts.LineStyle.Solid,
            axisLabelVisible: isActive,
            title: isActive ? `${zone.type.toUpperCase()} ${zone.confidence}%` : '',
        });

        const bottomLine = candleSeries.createPriceLine({
            price: zone.low,
            color: color,
            lineWidth: 2,
            lineStyle: LightweightCharts.LineStyle.Solid,
            axisLabelVisible: false,
            title: '',
        });

        zoneOverlays.push(topLine, bottomLine);
    });
}

function clearZones() {
    if (!candleSeries) return;
    zoneOverlays.forEach(line => {
        try { candleSeries.removePriceLine(line); } catch(e) {}
    });
    zoneOverlays = [];
}

function drawSignals(signals) {
    if (!candleSeries || !signals || signals.length === 0) return;

    const markers = signals
        .filter(sig => sig.time && sig.direction)
        .map(sig => ({
            time: sig.time,
            position: sig.direction === 'buy' ? 'belowBar' : 'aboveBar',
            color: sig.direction === 'buy' ? COLORS.green : COLORS.red,
            shape: sig.direction === 'buy' ? 'arrowUp' : 'arrowDown',
            text: `${sig.direction.toUpperCase()}`,
        }));

    markers.sort((a, b) => a.time - b.time);
    if (markers.length > 0) {
        candleSeries.setMarkers(markers);
    }
}

function drawLevels(position) {
    clearLevels();
    if (!candleSeries || !position) return;

    const slLine = candleSeries.createPriceLine({
        price: position.stop_loss,
        color: COLORS.red,
        lineWidth: 1,
        lineStyle: LightweightCharts.LineStyle.Dashed,
        axisLabelVisible: true,
        title: 'SL',
    });
    priceLines.push(slLine);

    const tp1Line = candleSeries.createPriceLine({
        price: position.take_profit_1,
        color: COLORS.green,
        lineWidth: 1,
        lineStyle: LightweightCharts.LineStyle.Dashed,
        axisLabelVisible: true,
        title: position.tp1_hit ? 'TP1 HIT' : 'TP1',
    });
    priceLines.push(tp1Line);

    const tp2Line = candleSeries.createPriceLine({
        price: position.take_profit_2,
        color: COLORS.green,
        lineWidth: 1,
        lineStyle: LightweightCharts.LineStyle.Dotted,
        axisLabelVisible: true,
        title: 'TP2',
    });
    priceLines.push(tp2Line);

    const entryLine = candleSeries.createPriceLine({
        price: position.entry_price,
        color: COLORS.amber,
        lineWidth: 1,
        lineStyle: LightweightCharts.LineStyle.Dotted,
        axisLabelVisible: true,
        title: 'ENTRY',
    });
    priceLines.push(entryLine);
}

function clearLevels() {
    if (!candleSeries) return;
    priceLines.forEach(line => {
        try { candleSeries.removePriceLine(line); } catch(e) {}
    });
    priceLines = [];
}

function fitContent() {
    if (chart) chart.timeScale().fitContent();
}
