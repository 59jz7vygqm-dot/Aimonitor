import { api } from '../api.js';
import { lineChart, seriesTable, fmtNum, fmtBytes, barChart } from '../chart.js';
import { card, tile, chartCard, spinner, errorBanner, h, table, statusHtml,
    escapeHtml, pct, fmtDuration, minibar } from '../ui.js';
import { sourceNote, toSeries, short } from './overview.js';

export async function render(root, state) {
    root.appendChild(spinner());
    let sum, res;
    try {
        [sum, res] = await Promise.all([
            api.openterminal({ window_s: rangeSeconds(state.range) }),
            api.queryMulti({
                start: '-' + state.range, max_points: 400,
                queries: [
                    { metric: 'openterminal.instances_running', agg: 'avg' },
                    { metric: 'openterminal.instances_desired', agg: 'avg' },
                    { metric: 'openterminal.sessions_active', agg: 'avg' },
                    { metric: 'openterminal.sessions_queued', agg: 'max' },
                    { metric: 'openterminal.headroom_pct', agg: 'min' },
                    { metric: 'openterminal.saturation_pct', agg: 'avg' },
                    { metric: 'openterminal.capacity_sessions', agg: 'last' },
                    { metric: 'openterminal.instance_sessions', agg: 'avg' },
                    { metric: 'openterminal.cpu_pct', agg: 'avg' },
                ],
            }),
        ]);
    } catch (e) {
        root.textContent = '';
        root.appendChild(errorBanner(e));
        return;
    }
    root.textContent = '';

    const proxies = Object.entries(sum.proxies || {});
    if (!proxies.length) {
        root.appendChild(h(`<div class="banner warning">${statusHtml('warning',
            'Kein Autoscaler angebunden')}<span>Der OpenTerminal-Proxy meldet nichts.
            Der fertige Integrations-Prompt steht in
            <code>docs/prompts/openterminal-autoscaler.md</code>.</span></div>`));
        return;
    }

    for (const [key, p] of proxies) {
        const c = p.current || {};
        const w = p.window || {};
        const run = c['openterminal.instances_running'];
        const max = c['openterminal.instances_max'];
        const act = c['openterminal.sessions_active'];
        const cap = c['openterminal.capacity_sessions'];
        const hr = c['openterminal.headroom_pct'];
        const sat = c['openterminal.saturation_pct'];
        const queued = c['openterminal.sessions_queued'];

        root.appendChild(h(`<h2 style="font-size:15px;margin:4px 0 10px">
            <code>${escapeHtml(key)}</code></h2>`));

        if (queued > 0 && run >= max) {
            root.appendChild(h(`<div class="banner critical">${statusHtml('critical',
                'Obergrenze erreicht')}<span>${queued} Sessions warten, aber
                <code>instances_max=${max}</code> ist ausgeschöpft. Der Autoscaler
                kann nicht mehr hochskalieren.</span></div>`));
        } else if (hr !== undefined && hr <= 20) {
            root.appendChild(h(`<div class="banner warning">${statusHtml('warning',
                `Headroom ${hr.toFixed(0)} %`)}<span>Noch ${
                    fmtNum(c['openterminal.headroom_sessions'], 0)} Sessions bis zur
                Obergrenze.</span></div>`));
        }

        const tiles = h('<div class="grid tiles"></div>');
        tiles.appendChild(tile('Instanzen',
            `${fmtNum(run, 0)}<span class="unit"> / ${fmtNum(max, 0)}</span>`,
            c['openterminal.instances_desired'] !== undefined &&
            c['openterminal.instances_desired'] !== run
                ? `Soll ${fmtNum(c['openterminal.instances_desired'], 0)} – ` +
                  `Scale-Vorgang läuft oder hängt`
                : `min ${fmtNum(c['openterminal.instances_min'], 0)}`));
        tiles.appendChild(tile('Aktive Sessions', `${fmtNum(act, 0)}`,
            cap ? `von ${fmtNum(cap, 0)} maximal möglichen` : ''));
        tiles.appendChild(tile('Headroom',
            hr !== undefined ? `${hr.toFixed(0)}<span class="unit"> %</span>` : '—',
            c['openterminal.headroom_sessions'] !== undefined
                ? `${fmtNum(c['openterminal.headroom_sessions'], 0)} Sessions frei ` +
                  `(gegen instances_max)` : '',
            hr !== undefined && hr > 20 ? 'good' : ''));
        tiles.appendChild(tile('Sättigung jetzt',
            sat !== undefined ? `${sat.toFixed(0)}<span class="unit"> %</span>` : '—',
            'gegen die laufenden Instanzen – das ist die Zahl, die ein Scale-up auslöst'));
        tiles.appendChild(tile('Wartende Sessions', `${fmtNum(queued ?? 0, 0)}`,
            c['openterminal.scale_cooldown_s']
                ? `Cooldown ${fmtNum(c['openterminal.scale_cooldown_s'], 0)} s`
                : 'kein Cooldown'));
        const peak = w['openterminal.sessions_active']?.peak;
        tiles.appendChild(tile(`Spitze im Zeitraum`, `${fmtNum(peak, 0)}`,
            w['openterminal.headroom_pct']?.low !== undefined
                ? `niedrigster Headroom ${w['openterminal.headroom_pct'].low.toFixed(0)} %`
                : ''));
        root.appendChild(tiles);
    }

    const grid = h('<div class="grid cols-2"></div>');

    const instSeries = [
        ...toSeries(res.results[0], () => 'laufend'),
        ...toSeries(res.results[1], () => 'Soll', 1),
    ];
    if (instSeries.length) {
        grid.appendChild(chartCard('Instanzen',
            'Weicht „laufend" dauerhaft von „Soll" ab, starten Instanzen nicht.',
            (box) => lineChart(box, instSeries, { height: 190, yMin: 0,
                fmt: (v) => fmtNum(v, 0), note: sourceNote(res.results[0]) }),
            () => seriesTable(instSeries, (v) => fmtNum(v, 1))));
    }

    const sessSeries = [
        ...toSeries(res.results[2], () => 'aktiv'),
        ...toSeries(res.results[3], () => 'wartend', 1),
        ...toSeries(res.results[6], () => 'Kapazität (max)', 2),
    ];
    if (sessSeries.length) {
        grid.appendChild(chartCard('Sessions und Kapazität',
            'Der Abstand zwischen „aktiv" und „Kapazität" ist der Headroom.',
            (box) => lineChart(box, sessSeries, { height: 190, yMin: 0,
                fmt: (v) => fmtNum(v, 0), note: sourceNote(res.results[2]) }),
            () => seriesTable(sessSeries, (v) => fmtNum(v, 1))));
    }

    const hrSeries = [
        ...toSeries(res.results[4], () => 'Headroom (gegen max)'),
        ...toSeries(res.results[5], () => 'Sättigung (laufende Instanzen)', 1),
    ];
    if (hrSeries.length) {
        grid.appendChild(chartCard('Headroom und Sättigung',
            'Zwei verschiedene Fragen: „kann ich noch wachsen" und „muss ich jetzt wachsen".',
            (box) => lineChart(box, hrSeries, { unit: '%', height: 190, yMin: 0,
                yMax: 100, fmt: (v) => fmtNum(v, 0),
                note: sourceNote(res.results[4]) }),
            () => seriesTable(hrSeries, (v) => fmtNum(v, 1) + ' %')));
    }

    const perInst = toSeries(res.results[7], (s) => s.labels.instance || '?');
    if (perInst.length) {
        grid.appendChild(chartCard('Sessions je Instanz',
            'Starke Schieflage heißt: das Load-Balancing verteilt nicht gleichmäßig.',
            (box) => lineChart(box, perInst, { height: 190, yMin: 0,
                fmt: (v) => fmtNum(v, 0), directLabels: perInst.length <= 4,
                note: sourceNote(res.results[7]) }),
            () => seriesTable(perInst, (v) => fmtNum(v, 1))));
    }
    root.appendChild(grid);

    // --- Instanztabelle
    const instances = proxies.flatMap(([key, p]) =>
        (p.instances || []).map((i) => ({ ...i, proxy: key })));
    if (instances.length) {
        const maxSess = Math.max(...instances.map((i) => i.sessions || 0), 1);
        root.appendChild(card('Laufende Instanzen', '', table([
            { label: 'Instanz', html: (r) => `<code>${escapeHtml(r.instance)}</code>` },
            { label: 'Zustand', html: (r) => r.state === 'running'
                ? statusHtml('good', 'läuft')
                : statusHtml('warning', r.state || 'unbekannt') },
            { label: 'Sessions', num: true, html: (r) => fmtNum(r.sessions, 0) },
            { label: '', html: (r) => minibar(r.sessions || 0, maxSess) },
            { label: 'CPU', num: true, html: (r) => pct(r.cpu_pct) },
            { label: 'RAM', num: true, html: (r) => r.mem_used_bytes
                ? fmtBytes(r.mem_used_bytes) : '—' },
            { label: 'Laufzeit', num: true, html: (r) => r.uptime_s
                ? fmtDuration(r.uptime_s) : '—' },
        ], instances)));
    }

    // --- Scale-Historie
    const ev = sum.scale_events || [];
    root.appendChild(card('Scale-Historie', sum.hinweis, table([
        { label: 'Zeit', html: (e) => new Date(e.ts).toLocaleString('de-DE') },
        { label: 'Art', html: (e) => {
            const lv = e.kind === 'scale.blocked' ? 'critical'
                : e.kind === 'scale.up' ? 'warning' : 'muted';
            return statusHtml(lv, e.kind.replace('scale.', ''));
        } },
        { label: 'Beschreibung', html: (e) => escapeHtml(e.title) },
        { label: 'Von→Nach', html: (e) => e.detail?.from !== undefined
            ? `${e.detail.from} → ${e.detail.to}` : '—' },
        { label: 'Grund', html: (e) => `<code>${escapeHtml(
            e.detail?.reason || e.detail?.trigger_metric || '—')}</code>` },
    ], ev, { scroll: true })));
}

export function rangeSeconds(r) {
    const m = /^(\d+)([smhdw])$/.exec(r);
    if (!m) return 3600;
    const u = { s: 1, m: 60, h: 3600, d: 86400, w: 604800 }[m[2]];
    return parseInt(m[1], 10) * u;
}
