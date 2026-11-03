import { api } from '../api.js';
import { lineChart, seriesTable, fmtBytes, fmtNum, seriesColor } from '../chart.js';
import {
    card, tile, chartCard, table, statusHtml, nodeStatus, sevLevel, sevText,
    pct, h, spinner, errorBanner, minibar, fmtDuration, escapeHtml,
} from '../ui.js';

export async function render(root, state) {
    root.appendChild(spinner());
    let ov, inc;
    try {
        [ov, inc] = await Promise.all([api.overview(), api.incidents({ limit: 50 })]);
    } catch (e) {
        root.textContent = '';
        root.appendChild(errorBanner(e));
        return;
    }
    root.textContent = '';

    // --- Kopfzeile: nur melden, was wirklich offen ist
    const open = inc.filter((i) => !i.ended_at);
    const crit = open.filter((i) => i.severity === 'critical');
    const errs = open.filter((i) => i.severity === 'error');
    if (crit.length || errs.length) {
        root.appendChild(h(`<div class="banner critical">${statusHtml('critical',
            `${crit.length} kritisch, ${errs.length} Fehler`)}
            <span>${escapeHtml(open.slice(0, 2).map((i) => i.title).join(' · '))}${
                open.length > 2 ? ` · und ${open.length - 2} weitere` : ''}</span></div>`));
    } else if (open.length) {
        root.appendChild(h(`<div class="banner warning">${statusHtml('warning',
            `${open.length} Warnungen`)} <span>${escapeHtml(
                open.slice(0, 3).map((i) => i.title).join(' · '))}</span></div>`));
    } else {
        root.appendChild(h(`<div class="banner good">${statusHtml('good',
            'Keine offenen Incidents')} <span>Alle Regeln ruhig.</span></div>`));
    }

    // --- Stat-Tiles
    const tiles = h('<div class="grid tiles"></div>');
    const g = ov.gpu_summary || {};
    const nodes = ov.nodes || {};

    tiles.appendChild(tile('Maschinen online',
        `${nodes.online ?? 0}<span class="unit"> / ${nodes.total ?? 0}</span>`,
        (nodes.stale || []).length
            ? `stumm: ${nodes.stale.join(', ')}`
            : 'alle melden',
        (nodes.stale || []).length ? '' : 'good'));

    const vramPct = g.total_vram_bytes
        ? (g.used_vram_bytes / g.total_vram_bytes) * 100 : null;
    tiles.appendChild(tile('GPUs',
        `${g.count ?? 0}`,
        g.total_vram_bytes
            ? `${fmtBytes(g.used_vram_bytes)} / ${fmtBytes(g.total_vram_bytes)} VRAM` +
              (vramPct !== null ? ` (${vramPct.toFixed(0)} %)` : '')
            : 'keine GPU-Daten'));

    tiles.appendChild(tile('Höchste GPU-Temperatur',
        g.max_temp_c !== null && g.max_temp_c !== undefined
            ? `${g.max_temp_c.toFixed(0)}<span class="unit"> °C</span>` : '—',
        (g.throttling || []).length
            ? `throttelt: ${g.throttling.join(', ')}`
            : 'kein Throttling'));

    const llm = ov.llm || {};
    const perSvc = llm.per_service || [];
    const totalReq = perSvc.reduce((a, s) => a + (s.requests || 0), 0);
    const worstP95 = perSvc.reduce((a, s) =>
        s.p95_ttft_ms !== null && s.p95_ttft_ms > a ? s.p95_ttft_ms : a, 0);
    tiles.appendChild(tile('TTFT p95 (schlechtester Dienst, 1 h)',
        worstP95 ? `${fmtNum(worstP95, 0)}<span class="unit"> ms</span>` : '—',
        totalReq ? `aus ${totalReq} gemessenen Requests`
            : 'keine Request-Daten – aimon-tap nicht im Pfad?'));

    const ot = Object.values(ov.openterminal || {})[0] || {};
    const hr = ot['openterminal.headroom_pct'];
    tiles.appendChild(tile('OpenTerminal-Headroom',
        hr !== undefined ? `${hr.toFixed(0)}<span class="unit"> %</span>` : '—',
        ot['openterminal.instances_running'] !== undefined
            ? `${ot['openterminal.instances_running']} / ${
                ot['openterminal.instances_max'] ?? '?'} Instanzen, ${
                ot['openterminal.sessions_active'] ?? '?'} Sessions`
            : 'Autoscaler meldet nichts',
        hr !== undefined && hr > 20 ? 'good' : ''));

    const pvEv = ov.privacy?.events_1h || {};
    const blocked = pvEv['privacy.blocked']?.occurrences ?? 0;
    const pvCounters = {};
    for (const r of ov.privacy?.latest || []) {
        if (r.labels?.category && r.labels.category !== '_all') continue;
        pvCounters[r.metric] = r.value;
    }
    tiles.appendChild(tile('Geblockte Suchanfragen (1 h)',
        `${blocked}`,
        pvCounters['privacy.block_rate_pct'] !== undefined
            ? `Blockrate insgesamt ${pct(pvCounters['privacy.block_rate_pct'], 1)}`
            : 'Privacy-Proxy meldet nichts'));

    root.appendChild(tiles);

    // --- GPU-Tabelle. Zahlen plus Balken: Magnitude sichtbar, Wert exakt.
    const gpus = ov.gpus || [];
    const maxTemp = 100;
    root.appendChild(card('GPUs', 'Letzter Messwert je Karte.', table([
        { label: 'Maschine', html: (r) => `<code>${escapeHtml(r.node)}</code>` },
        { label: 'GPU', html: (r) => escapeHtml(String(r.gpu)) },
        { label: 'Modell', html: (r) => escapeHtml(r.name || '—') },
        { label: 'Temp', num: true, html: (r) =>
            r.temp_c === null ? '—' : `${r.temp_c.toFixed(0)} °C` },
        { label: '', html: (r) => minibar(r.temp_c || 0, maxTemp,
            r.temp_c >= 88 ? 'critical' : r.temp_c >= 83 ? 'warning' : '') },
        { label: 'Last', num: true, html: (r) => pct(r.util_pct) },
        { label: 'VRAM', num: true, html: (r) => r.mem_pct === null ? '—'
            : `${r.mem_pct.toFixed(0)} %` },
        { label: '', html: (r) => minibar(r.mem_pct || 0, 100,
            r.mem_pct >= 98 ? 'critical' : r.mem_pct >= 92 ? 'warning' : '') },
        { label: 'VRAM absolut', num: true, html: (r) => r.mem_total
            ? `${fmtBytes(r.mem_used)} / ${fmtBytes(r.mem_total)}` : '—' },
        { label: 'Leistung', num: true, html: (r) => r.power_w === null ? '—'
            : `${r.power_w.toFixed(0)}${r.power_limit_w
                ? ` / ${r.power_limit_w.toFixed(0)}` : ''} W` },
        { label: 'Zustand', html: (r) => {
            if (!r.fresh) return statusHtml('warning',
                `alt (${fmtDuration(r.age_s)})`);
            const t = [];
            if (r.throttle_thermal > 0) t.push('thermisch');
            if (r.throttle_power > 0) t.push('Power-Limit');
            if (r.throttle_hw > 0) t.push('Hardware');
            return t.length ? statusHtml('warning', 'throttelt: ' + t.join(', '))
                : statusHtml('good', 'ok');
        } },
    ], gpus)));

    // --- Inference
    root.appendChild(card('Inference (letzte Stunde)',
        llm.note || '', table([
            { label: 'Dienst', html: (r) => `<code>${escapeHtml(r.service)}</code>` },
            { label: 'Requests', num: true, key: 'requests' },
            { label: 'TTFT ⌀', num: true, html: (r) =>
                r.avg_ttft_ms === null ? '—' : `${fmtNum(r.avg_ttft_ms, 0)} ms` },
            { label: 'TTFT p95', num: true, html: (r) =>
                r.p95_ttft_ms === null ? '—' : `${fmtNum(r.p95_ttft_ms, 0)} ms` },
            { label: 'Prompt ⌀', num: true, html: (r) =>
                r.avg_prompt_tokens === null ? '—'
                    : `${fmtNum(r.avg_prompt_tokens, 0)} Tok` },
            { label: 'Output ⌀', num: true, html: (r) =>
                r.avg_completion_tokens === null ? '—'
                    : `${fmtNum(r.avg_completion_tokens, 0)} Tok` },
            { label: 'Fehler', num: true, html: (r) => r.errors
                ? statusHtml('serious', String(r.errors)) : '0' },
        ], perSvc)));

    // --- Live-Verlauf: GPU-Temperatur und TTFT
    const qs = await api.queryMulti({
        start: '-' + state.range, max_points: 300,
        queries: [
            { metric: 'gpu.temp_c', agg: 'avg' },
            { metric: 'llm.requests_running', agg: 'avg' },
            { metric: 'llm.requests_waiting', agg: 'avg' },
            { metric: 'llm.kv_cache_usage_pct', agg: 'avg' },
        ],
    }).catch(() => null);

    if (qs) {
        const grid = h('<div class="grid cols-2"></div>');
        const temps = toSeries(qs.results[0], (s) =>
            `${s.node}/gpu${s.labels.gpu ?? '?'}`);
        grid.appendChild(chartCard('GPU-Temperatur', `Zeitraum ${state.range}.`,
            (box) => lineChart(box, temps, { unit: '°C', height: 190,
                fmt: (v) => fmtNum(v, 0),
                note: sourceNote(qs.results[0]) }),
            () => seriesTable(temps, (v) => fmtNum(v, 1))));

        const running = toSeries(qs.results[1], (s) => `laufend ${short(s.service)}`);
        const waiting = toSeries(qs.results[2], (s) => `wartend ${short(s.service)}`,
            running.length);
        const load = [...running, ...waiting];
        grid.appendChild(chartCard('vLLM-Auslastung',
            'Laufende und wartende Requests. Wartende gehen voll in die TTFT ein.',
            (box) => lineChart(box, load, { height: 190, fmt: (v) => fmtNum(v, 0),
                yMin: 0, note: sourceNote(qs.results[1]) }),
            () => seriesTable(load, (v) => fmtNum(v, 1))));
        root.appendChild(grid);
    }

    // --- Offene Incidents
    if (open.length) {
        const list = open.slice(0, 12).map((i) => `
            <div class="incident">
              <div>${statusHtml(sevLevel(i.severity), sevText(i.severity))}</div>
              <div class="body">
                <div class="title">${escapeHtml(i.title)}</div>
                <div class="meta"><code>${escapeHtml(i.rule)}</code> ·
                  ${escapeHtml(i.subject)} · seit ${fmtDuration(i.age_s)}</div>
              </div>
            </div>`).join('');
        root.appendChild(card('Offene Incidents',
            `${open.length} offen. Vollständig mit Kontext unter „Diagnose“.`, list));
    }
}

export function short(key) {
    if (!key) return '?';
    return key.split('@')[0] + '@' + (key.split('@')[1] || '').split(':')[0];
}

export function sourceNote(res) {
    if (!res) return '';
    const map = { raw: 'Rohdaten', rollup_60: '1-Minuten-Mittel',
        rollup_3600: 'Stunden-Mittel' };
    if (!res.source) return 'Keine Serie gefunden.';
    return `Quelle: ${map[res.source] || res.source}` +
        (res.bucket_s ? `, Bucket ${res.bucket_s} s` : '');
}

export function toSeries(res, nameFn, colorOffset = 0) {
    if (!res || !res.series) return [];
    return res.series.map((s, i) => ({
        name: nameFn(s),
        points: s.points,
        color: seriesColor(i + colorOffset),
    }));
}
