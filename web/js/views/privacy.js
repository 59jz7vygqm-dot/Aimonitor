import { api } from '../api.js';
import { lineChart, seriesTable, barChart, fmtNum, seriesColor } from '../chart.js';
import { card, tile, chartCard, spinner, errorBanner, h, table, statusHtml,
    escapeHtml, pct, toast } from '../ui.js';
import { sourceNote, toSeries, short } from './overview.js';
import { rangeSeconds } from './openterminal.js';

/* SearXNG-Privacy-Proxy: Anfragen, Blockgründe und das Log.
 *
 * Datenschutz-Hinweis im UI ist Absicht: im Log steht die *bereinigte*
 * Anfrage. Rohtext erscheint nur, wenn der Server das ausdrücklich erlaubt -
 * sonst würde das Monitoring genau die Daten sammeln, die der Proxy entfernt.
 */

export async function render(root, state) {
    root.appendChild(spinner());
    let blocked, res, stats;
    try {
        [blocked, res, stats] = await Promise.all([
            api.privacyBlocked({ since: '-' + state.range,
                action: 'blocked,redacted,error', limit: 500 }),
            api.queryMulti({
                start: '-' + state.range, max_points: 400,
                queries: [
                    { metric: 'privacy.requests_total', agg: 'rate' },
                    { metric: 'privacy.blocked_total', agg: 'rate',
                        labels: { category: '_all' } },
                    { metric: 'privacy.block_rate_pct', agg: 'avg' },
                    { metric: 'privacy.upstream_latency_avg_ms', agg: 'avg' },
                    { metric: 'privacy.query_len_avg_chars', agg: 'avg' },
                    { metric: 'privacy.upstream_errors_total', agg: 'rate' },
                ],
            }),
            api.stats().catch(() => null),
        ]);
    } catch (e) {
        root.textContent = '';
        root.appendChild(errorBanner(e));
        return;
    }
    root.textContent = '';

    const counters = {};
    for (const c of blocked.counters || []) {
        if (c.category && c.category !== '_all') continue;
        counters[c.metric] = c.value;
    }

    if (!blocked.counters?.length && !blocked.log.length) {
        root.appendChild(h(`<div class="banner warning">${statusHtml('warning',
            'Kein Privacy-Proxy angebunden')}<span>Der Proxy vor SearXNG meldet nichts.
            Der fertige Integrations-Prompt steht in
            <code>docs/prompts/searxng-privacy-proxy.md</code>.</span></div>`));
        return;
    }

    const rate = counters['privacy.block_rate_pct'];
    if (rate !== undefined && rate >= 25) {
        root.appendChild(h(`<div class="banner warning">${statusHtml('warning',
            `Blockrate ${rate.toFixed(0)} %`)}<span>Entweder ein Filter ist zu scharf
            oder es wird tatsächlich systematisch nach Personen gesucht. Die
            Kategorieverteilung unten unterscheidet das: ein einzelnes Regel-Label
            mit fast allen Treffern spricht für False Positives.</span></div>`));
    }

    const tiles = h('<div class="grid tiles"></div>');
    tiles.appendChild(tile('Anfragen gesamt',
        fmtNum(counters['privacy.requests_total'], 0),
        'seit Start des Proxys'));
    tiles.appendChild(tile('Geblockt gesamt',
        fmtNum(counters['privacy.blocked_total'], 0),
        rate !== undefined ? `Blockrate ${rate.toFixed(1)} %` : ''));
    tiles.appendChild(tile('Bereinigt gesamt',
        fmtNum(counters['privacy.redacted_total'], 0),
        'durchgelassen, aber PII entfernt'));
    tiles.appendChild(tile('Filter-Fehler',
        fmtNum(counters['privacy.errors_total'] ?? 0, 0),
        'Fehler im Proxy selbst'));

    const logBlocked = blocked.log.filter((r) => r.kind === 'privacy.blocked');
    tiles.appendChild(tile(`Im Zeitraum geblockt`, String(logBlocked.length),
        `${blocked.log.length} Einträge im Log`));
    root.appendChild(tiles);

    const grid = h('<div class="grid cols-2"></div>');
    const reqSer = [
        ...toSeries(res.results[0], () => 'Anfragen/s'),
        ...toSeries(res.results[1], () => 'geblockt/s', 1),
        ...toSeries(res.results[5], () => 'Upstream-Fehler/s', 7),
    ];
    if (reqSer.length) {
        grid.appendChild(chartCard('Durchsatz', 'Raten aus den Zählern des Proxys.',
            (box) => lineChart(box, reqSer, { unit: '/s', height: 190, yMin: 0,
                fmt: (v) => fmtNum(v, 2), note: sourceNote(res.results[0]) }),
            () => seriesTable(reqSer, (v) => fmtNum(v, 3))));
    }
    const rateSer = toSeries(res.results[2], (s) => short(s.service));
    if (rateSer.length) {
        grid.appendChild(chartCard('Blockrate',
            'Ein Sprung nach oben ohne Lastanstieg deutet auf eine geänderte Filterregel.',
            (box) => lineChart(box, rateSer, { unit: '%', height: 190, yMin: 0,
                fmt: (v) => fmtNum(v, 1), note: sourceNote(res.results[2]) }),
            () => seriesTable(rateSer, (v) => fmtNum(v, 2) + ' %')));
    }
    const latSer = toSeries(res.results[3], (s) => short(s.service));
    if (latSer.length) {
        grid.appendChild(chartCard('Upstream-Latenz zu SearXNG', '',
            (box) => lineChart(box, latSer, { unit: 'ms', height: 190, yMin: 0,
                fmt: (v) => fmtNum(v, 0), note: sourceNote(res.results[3]) }),
            () => seriesTable(latSer, (v) => fmtNum(v, 1))));
    }
    const lenSer = toSeries(res.results[4], (s) => short(s.service));
    if (lenSer.length) {
        grid.appendChild(chartCard('Mittlere Anfragelänge', 'Zeichen pro Suchanfrage.',
            (box) => lineChart(box, lenSer, { height: 190, yMin: 0,
                fmt: (v) => fmtNum(v, 0), note: sourceNote(res.results[4]) }),
            () => seriesTable(lenSer, (v) => fmtNum(v, 1))));
    }
    root.appendChild(grid);

    // --- Blockgründe. Kategorie und Regel getrennt: die Regel sagt, welcher
    //     Filter feuert, die Kategorie, was er zu finden meint.
    const byCat = (blocked.by_category || [])
        .filter((r) => r.kind === 'privacy.blocked')
        .sort((a, b) => (b.occurrences || 0) - (a.occurrences || 0));
    const catCard = card('Blockgründe im Zeitraum',
        'Nach Kategorie und auslösender Regel.', '<div class="catchart"></div>');
    root.appendChild(catCard);
    barChart(catCard.querySelector('.catchart'), byCat.map((r) => ({
        label: r.category || 'ohne Angabe',
        value: Number(r.occurrences || 0),
        note: r.rule ? `Regel ${r.rule}` : undefined,
    })), { labelHead: 'Kategorie', valueHead: 'Treffer' });

    // --- Das Log
    const rawAllowed = stats?.allow_raw_queries;
    const filterRow = h(`<div class="filterbar">
        <label class="inline">Aktion
          <select data-f="action">
            <option value="blocked,redacted,error">alle Eingriffe</option>
            <option value="blocked">nur geblockt</option>
            <option value="redacted">nur bereinigt</option>
            <option value="error">nur Fehler</option>
          </select></label>
        <label class="inline">Kategorie
          <select data-f="category"><option value="">alle</option>${
            [...new Set((blocked.by_category || []).map((r) => r.category)
                .filter(Boolean))].map((c) =>
                `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join('')
          }</select></label>
        <input type="search" data-f="q" placeholder="im Log suchen" style="min-width:200px">
        <span class="source-note">${rawAllowed
            ? '⚠ Der Server speichert Rohtexte (AIMON_ALLOW_RAW_QUERIES=true).'
            : 'Es wird nur die bereinigte Anfrage gespeichert, kein Rohtext.'}</span>
      </div>`);

    const logCard = card('Log der Eingriffe', blocked.hinweis, '');
    logCard.querySelector('.card-body').appendChild(filterRow);
    const logBox = h('<div></div>');
    logCard.querySelector('.card-body').appendChild(logBox);
    root.appendChild(logCard);

    function drawLog(rows) {
        logBox.innerHTML = table([
            { label: 'Zeit', html: (r) => new Date(r.ts).toLocaleString('de-DE') },
            { label: 'Aktion', html: (r) => {
                const a = r.kind.replace('privacy.', '');
                const lv = a === 'blocked' ? 'critical'
                    : a === 'error' ? 'serious' : 'warning';
                const txt = { blocked: 'geblockt', redacted: 'bereinigt',
                    error: 'Fehler' }[a] || a;
                return statusHtml(lv, txt) + (r.count > 1
                    ? ` <span class="pill">×${r.count}</span>` : '');
            } },
            { label: 'Kategorie', html: (r) => escapeHtml(r.category || '—') },
            { label: 'Regel', html: (r) => `<code>${escapeHtml(r.rule || '—')}</code>` },
            { label: 'Konfidenz', num: true, html: (r) => r.confidence === null
                ? '—' : r.confidence.toFixed(2) },
            { label: 'Anfrage (bereinigt)', html: (r) => r.query_redacted
                ? `<code>${escapeHtml(r.query_redacted)}</code>`
                : (r.query_raw
                    ? `<code>${escapeHtml(r.query_raw)}</code> ` +
                      `<span class="pill critical">Rohtext</span>`
                    : '<span style="color:var(--text-muted)">nicht übertragen</span>') },
            { label: 'Länge', num: true, html: (r) => r.query_len ?? '—' },
            { label: 'Hash', html: (r) => r.query_sha256
                ? `<code title="${escapeHtml(r.query_sha256)}">${
                    escapeHtml(r.query_sha256.slice(0, 10))}…</code>` : '—' },
            { label: 'Client', html: (r) => `<code>${escapeHtml(r.client_id || '—')}</code>` },
        ], rows, { scroll: true });
    }
    drawLog(blocked.log);

    filterRow.querySelectorAll('[data-f]').forEach((ctl) => {
        const ev = ctl.tagName === 'INPUT' ? 'input' : 'change';
        ctl.addEventListener(ev, async () => {
            const action = filterRow.querySelector('[data-f="action"]').value;
            const category = filterRow.querySelector('[data-f="category"]').value;
            const q = filterRow.querySelector('[data-f="q"]').value.toLowerCase();
            try {
                const r = await api.privacyBlocked({ since: '-' + state.range,
                    action, category, limit: 500 });
                const rows = q
                    ? r.log.filter((x) => JSON.stringify(x).toLowerCase().includes(q))
                    : r.log;
                drawLog(rows);
            } catch (e) {
                toast('Filtern fehlgeschlagen: ' + (e.message || e));
            }
        });
    });
}
