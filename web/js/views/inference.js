import { api } from '../api.js';
import { lineChart, seriesTable, fmtNum, barChart } from '../chart.js';
import { chartCard, card, spinner, errorBanner, h, table, escapeHtml,
    statusHtml, pct } from '../ui.js';
import { sourceNote, toSeries, short } from './overview.js';

export async function render(root, state) {
    root.appendChild(spinner());
    let res, lat, ps, services;
    try {
        [res, lat, ps, services] = await Promise.all([
            api.queryMulti({
                start: '-' + state.range, max_points: 400,
                queries: [
                    { metric: 'llm.requests_running', agg: 'avg' },
                    { metric: 'llm.requests_waiting', agg: 'avg' },
                    { metric: 'llm.kv_cache_usage_pct', agg: 'avg' },
                    { metric: 'llm.prefix_cache_hit_pct', agg: 'avg' },
                    { metric: 'llm.generation_tokens_total', agg: 'rate' },
                    { metric: 'llm.prompt_tokens_total', agg: 'rate' },
                    { metric: 'llm.preemptions_total', agg: 'rate' },
                    { metric: 'ollama.model_vram_bytes', agg: 'last' },
                    { metric: 'ollama.probe_ttft_ms', agg: 'avg' },
                    { metric: 'comfy.queue_pending', agg: 'avg' },
                ],
            }),
            api.latency({ what: 'ttft', start: '-' + state.range }),
            api.promptStats({ start: '-' + state.range }),
            api.services(),
        ]);
    } catch (e) {
        root.textContent = '';
        root.appendChild(errorBanner(e));
        return;
    }
    root.textContent = '';

    // --- TTFT: Quelle offenlegen, nie verschweigen
    const srcText = {
        requests: 'Pro Request gemessen (aimon-tap im Datenpfad) – exakt.',
        histogram: 'Aus vLLM-Histogrammen interpoliert – Genauigkeit durch ' +
            'Bucket-Grenzen begrenzt, nicht nach Modell aufgelöst.',
    };
    const qrows = lat.quantiles
        ? Object.entries(lat.quantiles).map(([q, v]) => ({
            label: `p${(parseFloat(q) * 100).toFixed(0)}`,
            value: v === null ? 0 : v,
        })) : [];
    const ttftCard = card('Time to First Token',
        lat.source ? srcText[lat.source] || lat.source
            : (lat.note || 'Keine Daten.'),
        qrows.length
            ? `<div class="grid tiles" style="margin-bottom:10px">${
                qrows.map((r) => `<div><div class="label" style="font-size:11.5px;` +
                    `color:var(--text-secondary)">${r.label}</div>` +
                    `<div style="font-size:24px;font-weight:600">${
                        fmtNum(r.value, 0)}<span style="font-size:14px;` +
                    `color:var(--text-secondary)"> ms</span></div></div>`).join('')}
               ${lat.avg !== undefined && lat.avg !== null
                ? `<div><div class="label" style="font-size:11.5px;` +
                  `color:var(--text-secondary)">⌀</div><div style="font-size:24px;` +
                  `font-weight:600">${fmtNum(lat.avg, 0)}<span style="font-size:14px;` +
                  `color:var(--text-secondary)"> ms</span></div></div>` : ''}
               <div><div class="label" style="font-size:11.5px;` +
                `color:var(--text-secondary)">Requests</div>` +
                `<div style="font-size:24px;font-weight:600">${lat.n ?? 0}</div></div>
              </div>`
            : `<div class="empty">${escapeHtml(lat.note || 'Keine Daten.')}</div>`);
    root.appendChild(ttftCard);

    // --- Prompt-Längen: beide Quellen getrennt ausweisen
    const fr = ps.from_requests || [];
    const vc = ps.from_vllm_counters;
    root.appendChild(card('Prompt-Längen', ps.note,
        table([
            { label: 'Dienst', html: (r) => `<code>${escapeHtml(r.service)}</code>` },
            { label: 'Modell', html: (r) => escapeHtml(r.model || '—') },
            { label: 'Requests', num: true, key: 'n' },
            { label: 'Prompt ⌀ (Tokens)', num: true, html: (r) =>
                fmtNum(r.avg_prompt_tokens, 0) },
            { label: 'Prompt p50', num: true, html: (r) =>
                fmtNum(r.p50_prompt_tokens, 0) },
            { label: 'Prompt max', num: true, html: (r) =>
                fmtNum(r.max_prompt_tokens, 0) },
            { label: 'Prompt ⌀ (Zeichen)', num: true, html: (r) =>
                fmtNum(r.avg_prompt_chars, 0) },
            { label: 'Output ⌀', num: true, html: (r) =>
                fmtNum(r.avg_completion_tokens, 0) },
        ], fr) +
        (vc ? `<div class="source-note">Aus den vLLM-Zählern über alle Requests: ` +
            `<strong>${fmtNum(vc.value, 0)} Tokens</strong> im Schnitt ` +
            `(${vc.n} Requests). Das deckt auch Verkehr ab, der nicht durch ` +
            `aimon-tap läuft.</div>` : '')));

    const grid = h('<div class="grid cols-2"></div>');
    const panels = [
        [0, 'Laufende Requests', '', (v) => fmtNum(v, 0), '', 0],
        [1, 'Wartende Requests', '',  (v) => fmtNum(v, 0),
            'Alles > 0 landet direkt in der TTFT der Benutzer.', 0],
        [2, 'KV-Cache-Belegung', '%', (v) => fmtNum(v, 0),
            'Ab ca. 90 % fängt vLLM an zu preempten – TTFT springt dann sprunghaft.', 0],
        [3, 'Prefix-Cache-Treffer', '%', (v) => fmtNum(v, 0),
            'Hohe Trefferquote heißt: gleiche Systemprompts werden wiederverwendet.', 0],
        [4, 'Ausgabe-Tokens pro Sekunde', '/s', (v) => fmtNum(v, 1), '', 0],
        [5, 'Prompt-Tokens pro Sekunde', '/s', (v) => fmtNum(v, 1), '', 0],
        [6, 'Preemptions pro Sekunde', '/s', (v) => fmtNum(v, 2),
            'Verdrängte Sequenzen. Jede davon kostet die betroffenen Requests Zeit.', 0],
    ];
    for (const [i, title, unit, fmt, sub] of panels) {
        const ser = toSeries(res.results[i], (s) => short(s.service));
        if (!ser.length) continue;
        grid.appendChild(chartCard(title, sub,
            (box) => lineChart(box, ser, { unit, fmt, height: 180, yMin: 0,
                yMax: unit === '%' ? 100 : undefined,
                note: sourceNote(res.results[i]) }),
            () => seriesTable(ser, fmt)));
    }

    const ollamaVram = toSeries(res.results[7], (s) => s.labels.model || '?');
    if (ollamaVram.length) {
        grid.appendChild(chartCard('Ollama: VRAM je geladenem Modell',
            'Modelle werden nach Inaktivität entladen; Lücken sind entladene Phasen.',
            (box) => lineChart(box, ollamaVram, { height: 180, yMin: 0,
                fmt: (v) => fmtNum(v / 1e9, 1),
                unit: 'GB', note: sourceNote(res.results[7]) }),
            () => seriesTable(ollamaVram, (v) => fmtNum(v / 1e9, 2) + ' GB')));
    }
    const probe = toSeries(res.results[8], (s) => s.labels.model || '?');
    if (probe.length) {
        grid.appendChild(chartCard('Ollama: synthetische TTFT-Probe',
            'Eigener 1-Token-Request des Agents. Messung der Modell-Bereitschaft, ' +
            'NICHT die Latenz echter Benutzer – dafür braucht es aimon-tap.',
            (box) => lineChart(box, probe, { unit: 'ms', height: 180, yMin: 0,
                fmt: (v) => fmtNum(v, 0), note: sourceNote(res.results[8]) }),
            () => seriesTable(probe, (v) => fmtNum(v, 1))));
    }
    const comfy = toSeries(res.results[9], (s) => short(s.service));
    if (comfy.length) {
        grid.appendChild(chartCard('ComfyUI-Warteschlange', '',
            (box) => lineChart(box, comfy, { height: 180, yMin: 0,
                fmt: (v) => fmtNum(v, 0), note: sourceNote(res.results[9]) }),
            () => seriesTable(comfy, (v) => fmtNum(v, 0))));
    }
    root.appendChild(grid);

    // --- Dienste-Inventar
    root.appendChild(card('Dienste', 'Was der Server kennt und wann er es zuletzt sah.',
        table([
            { label: 'Schlüssel', html: (r) => `<code>${escapeHtml(r.key)}</code>` },
            { label: 'Art', key: 'kind' },
            { label: 'Name', html: (r) => escapeHtml(r.name || '—') },
            { label: 'Maschine', html: (r) => `<code>${escapeHtml(r.node_name || '—')}</code>` },
            { label: 'Version', html: (r) => escapeHtml(r.version || '—') },
            { label: 'Labels', html: (r) => `<code>${escapeHtml(
                Object.entries(r.labels || {}).map(([k, v]) => `${k}=${v}`)
                    .join(' ') || '—')}</code>` },
            { label: 'Zustand', html: (r) => r.status === 'online'
                ? statusHtml('good', 'läuft')
                : statusHtml(r.status === 'stale' ? 'warning' : 'critical', r.status) },
            { label: 'URL', html: (r) => r.base_url
                ? `<a href="${escapeHtml(r.base_url)}" target="_blank" rel="noopener">` +
                  `${escapeHtml(r.base_url)}</a>` : '—' },
        ], services)));
}
