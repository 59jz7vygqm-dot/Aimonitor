import { api } from '../api.js';
import { lineChart, seriesTable, fmtNum, fmtBytes } from '../chart.js';
import { card, spinner, errorBanner, h, table, escapeHtml, chartCard,
    statusHtml, fmtDuration, toast } from '../ui.js';
import { sourceNote } from './overview.js';

/* Metrik-Explorer.
 *
 * Wichtig für die Erweiterbarkeit: wer einen neuen Dienst anbindet, sieht hier
 * sofort, ob dessen Metriken ankommen - ohne dass ein Panel dafür gebaut werden
 * muss.
 */

export async function render(root, state) {
    root.appendChild(spinner());
    let cat, srv, nodes;
    try {
        [cat, srv, nodes] = await Promise.all([
            api.catalog(), api.stats().catch(() => null), api.nodes(),
        ]);
    } catch (e) {
        root.textContent = '';
        root.appendChild(errorBanner(e));
        return;
    }
    root.textContent = '';

    // --- Selbstüberwachung
    if (srv) {
        const c = srv.counts || {};
        root.appendChild(card('aimonitor selbst',
            `Version ${srv.version}, ${srv.timescale
                ? 'TimescaleDB aktiv' : 'normales PostgreSQL (kein Timescale)'}.`,
            table([
                { label: 'Kennzahl', key: 'k' },
                { label: 'Wert', num: true, key: 'v' },
            ], [
                { k: 'Maschinen', v: c.nodes ?? '—' },
                { k: 'Dienste', v: c.services ?? '—' },
                { k: 'Zeitreihen (Serien)', v: c.series ?? '—' },
                { k: 'Samples (geschätzt)', v: (srv.samples_estimate ?? 0)
                    .toLocaleString('de-DE') },
                { k: 'Rollup-Zeilen', v: (c.rollup_rows ?? 0).toLocaleString('de-DE') },
                { k: 'Ereignisse', v: (c.events ?? 0).toLocaleString('de-DE') },
                { k: 'Offene Incidents', v: c.incidents_open ?? '—' },
                { k: 'Datenbankgröße', v: fmtBytes(srv.db_size_bytes || 0) },
                { k: 'Rollup-Verzug', v: fmtDuration(srv.rollup?.lag_s ?? 0) },
                { k: 'Ingest-Items', v: (srv.ingest?.items ?? 0)
                    .toLocaleString('de-DE') },
                { k: 'Ingest abgelehnt', v: srv.ingest?.rejected ?? 0 },
                { k: 'Rohtexte erlaubt', v: srv.allow_raw_queries ? 'ja' : 'nein' },
            ]) +
            `<div class="source-note">Aufbewahrung: Samples ${
                srv.retention_days?.samples} Tage, 1-Minuten-Rollup ${
                srv.retention_days?.rollup_1m} Tage, Stunden-Rollup ${
                srv.retention_days?.rollup_1h} Tage, Requests ${
                srv.retention_days?.requests} Tage, Ereignisse ${
                srv.retention_days?.events} Tage.</div>`));
    }

    root.appendChild(card('Maschinen', 'Was Daten schickt und wann zuletzt.', table([
        { label: 'Name', html: (n) => `<code>${escapeHtml(n.name)}</code>` },
        { label: 'Hostname', html: (n) => escapeHtml(n.hostname || '—') },
        { label: 'Agent', html: (n) => escapeHtml(n.agent_version || '—') },
        { label: 'Dienste', num: true, key: 'services' },
        { label: 'Serien', num: true, key: 'series' },
        { label: 'Zustand', html: (n) => n.status === 'online'
            ? statusHtml('good', 'online')
            : statusHtml(n.status === 'stale' ? 'warning' : 'critical',
                `${n.status}, stumm seit ${fmtDuration(n.last_seen_age_s)}`) },
        { label: 'Labels', html: (n) => `<code>${escapeHtml(
            Object.entries(n.labels || {}).map(([k, v]) => `${k}=${v}`).join(' ')
            || '—')}</code>` },
    ], nodes)));

    // --- Ad-hoc-Abfrage
    const ex = h(`<section class="card">
      <div class="card-head"><div>
        <h3>Metrik ansehen</h3>
        <p class="sub">Jede Metrik, die ankommt – auch von Diensten, für die es noch
        kein eigenes Panel gibt.</p>
      </div></div>
      <div class="card-body">
        <div class="filterbar">
          <label class="inline">Metrik
            <select data-x="metric" style="min-width:260px">
              ${cat.map((m) => `<option value="${escapeHtml(m.metric)}">${
                  escapeHtml(m.metric)} (${m.series})</option>`).join('')}
            </select></label>
          <label class="inline">Aggregation
            <select data-x="agg">
              <option value="avg">Mittel</option>
              <option value="max">Maximum</option>
              <option value="min">Minimum</option>
              <option value="last">letzter Wert</option>
              <option value="sum">Summe</option>
              <option value="rate">Rate (für Zähler)</option>
            </select></label>
          <label class="inline">Maschine
            <select data-x="node"><option value="">alle</option>${
              nodes.map((n) => `<option value="${escapeHtml(n.name)}">${
                  escapeHtml(n.name)}</option>`).join('')}</select></label>
          <button class="ctl primary" data-x="go">Anzeigen</button>
        </div>
        <div data-x="out"></div>
      </div>
    </section>`);
    root.appendChild(ex);

    const sel = ex.querySelector('[data-x="metric"]');
    const aggSel = ex.querySelector('[data-x="agg"]');
    // Zähler sinnvoll vorbelegen: der Absolutwert eines Counters sagt nichts.
    sel.addEventListener('change', () => {
        const m = cat.find((c) => c.metric === sel.value);
        aggSel.value = m && m.is_counter ? 'rate' : 'avg';
    });

    async function show() {
        const out = ex.querySelector('[data-x="out"]');
        out.textContent = '';
        out.appendChild(spinner());
        const metric = sel.value;
        const agg = aggSel.value;
        const node = ex.querySelector('[data-x="node"]').value;
        try {
            const r = await api.query({ metric, agg, node,
                start: '-' + state.range, max_points: 400 });
            out.textContent = '';
            if (!r.series.length) {
                out.appendChild(h(`<div class="empty">${escapeHtml(
                    r.note || 'Keine Serie gefunden.')}</div>`));
                return;
            }
            const info = cat.find((c) => c.metric === metric);
            const isBytes = metric.endsWith('_bytes');
            const fmt = isBytes ? fmtBytes : (v) => fmtNum(v);
            const ser = r.series.map((s) => ({
                name: [s.node, s.service, Object.entries(s.labels || {})
                    .map(([k, v]) => `${k}=${v}`).join(' ')]
                    .filter(Boolean).join(' ') || metric,
                points: s.points,
            }));
            out.appendChild(chartCard(metric,
                `${info?.is_counter ? 'Zähler' : 'Gauge'} · ${ser.length} Serien` +
                (info?.label_keys?.length
                    ? ` · Labels: ${info.label_keys.join(', ')}` : ''),
                (box) => lineChart(box, ser, { height: 230, fmt,
                    directLabels: ser.length <= 4, note: sourceNote(r) }),
                () => seriesTable(ser, fmt)));
        } catch (e) {
            out.textContent = '';
            out.appendChild(errorBanner(e));
        }
    }
    ex.querySelector('[data-x="go"]').addEventListener('click', show);
    if (cat.length) show();

    // --- Katalog
    root.appendChild(card('Metrik-Katalog',
        `${cat.length} Metriken bekannt. Neue erscheinen hier automatisch, sobald ` +
        `sie zum ersten Mal ankommen – es braucht keine Migration.`,
        table([
            { label: 'Metrik', html: (m) => `<code>${escapeHtml(m.metric)}</code>` },
            { label: 'Typ', html: (m) => m.is_counter ? 'Zähler' : 'Gauge' },
            { label: 'Serien', num: true, key: 'series' },
            { label: 'Label-Keys', html: (m) => `<code>${escapeHtml(
                (m.label_keys || []).join(', ') || '—')}</code>` },
            { label: 'Zuletzt gesehen', html: (m) =>
                new Date(m.last_seen).toLocaleString('de-DE') },
        ], cat, { scroll: true })));
}
