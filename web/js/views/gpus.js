import { api } from '../api.js';
import { lineChart, seriesTable, fmtBytes, fmtNum } from '../chart.js';
import { chartCard, card, spinner, errorBanner, h, table, escapeHtml,
    statusHtml, pct } from '../ui.js';
import { sourceNote, toSeries } from './overview.js';

export async function render(root, state) {
    root.appendChild(spinner());
    let res, procs;
    try {
        [res, procs] = await Promise.all([
            api.queryMulti({
                start: '-' + state.range, max_points: 400,
                queries: [
                    { metric: 'gpu.temp_c', agg: 'avg' },
                    { metric: 'gpu.util_pct', agg: 'avg' },
                    { metric: 'gpu.mem_used_bytes', agg: 'avg' },
                    { metric: 'gpu.power_w', agg: 'avg' },
                    { metric: 'gpu.fan_pct', agg: 'avg' },
                    { metric: 'gpu.clock_sm_mhz', agg: 'avg' },
                    { metric: 'gpu.throttle_thermal', agg: 'max' },
                    { metric: 'gpu.throttle_power', agg: 'max' },
                    { metric: 'host.cpu_pct', agg: 'avg' },
                    { metric: 'host.mem_used_bytes', agg: 'avg' },
                ],
            }),
            api.latest({ metric: 'gpu.proc_mem_bytes' }).catch(() => []),
        ]);
    } catch (e) {
        root.textContent = '';
        root.appendChild(errorBanner(e));
        return;
    }
    root.textContent = '';

    const label = (s) => `${s.node}/gpu${s.labels.gpu ?? '?'}`;
    const grid = h('<div class="grid cols-2"></div>');

    const panels = [
        [0, 'Temperatur', '°C', (v) => fmtNum(v, 0),
            'Ab ca. 83 °C beginnt bei Consumer-Karten das thermische Throttling.'],
        [1, 'Auslastung', '%', (v) => fmtNum(v, 0),
            'Prozent der Zeit, in der mindestens ein Kernel lief – nicht wie gut die GPU ausgenutzt wird.'],
        [2, 'VRAM belegt', '', fmtBytes,
            'vLLM reserviert VRAM vorab; eine hohe Belegung ist dort normal.'],
        [3, 'Leistungsaufnahme', 'W', (v) => fmtNum(v, 0), ''],
        [4, 'Lüfter', '%', (v) => fmtNum(v, 0), ''],
        [5, 'SM-Takt', 'MHz', (v) => fmtNum(v, 0),
            'Fällt der Takt bei hoher Last, throttelt die Karte.'],
    ];
    for (const [i, title, unit, fmt, sub] of panels) {
        const ser = toSeries(res.results[i], label);
        if (!ser.length) continue;
        grid.appendChild(chartCard(title, sub,
            (box) => lineChart(box, ser, { unit, fmt, height: 180,
                yMin: unit === '%' ? 0 : undefined,
                yMax: unit === '%' ? 100 : undefined,
                note: sourceNote(res.results[i]) }),
            () => seriesTable(ser, fmt)));
    }

    const thermal = toSeries(res.results[6], (s) => `${label(s)} thermisch`);
    const power = toSeries(res.results[7], (s) => `${label(s)} Power-Limit`, 1);
    const thr = [...thermal, ...power].filter((s) =>
        s.points.some(([, v]) => v > 0));
    if (thr.length) {
        grid.appendChild(chartCard('Throttling',
            '1 = die Karte hat im Intervall gebremst. Direkte Ursache für TTFT-Spitzen.',
            (box) => lineChart(box, thr, { height: 150, yMin: 0, yMax: 1.1,
                yTicks: [0, 1], fmt: (v) => (v >= 0.5 ? 'ja' : 'nein'),
                note: sourceNote(res.results[6]) }),
            () => seriesTable(thr, (v) => (v >= 0.5 ? 'ja' : 'nein'))));
    }

    const cpu = toSeries(res.results[8], (s) => s.node);
    if (cpu.length) {
        grid.appendChild(chartCard('CPU der Hosts', '',
            (box) => lineChart(box, cpu, { unit: '%', yMin: 0, yMax: 100,
                height: 180, fmt: (v) => fmtNum(v, 0),
                note: sourceNote(res.results[8]) }),
            () => seriesTable(cpu, (v) => fmtNum(v, 1))));
    }

    root.appendChild(grid);

    if (procs && procs.length) {
        root.appendChild(card('Prozesse auf den GPUs',
            'Teilen sich zwei Dienste eine Karte, kämpfen sie um denselben Speicher.',
            table([
                { label: 'GPU-UUID', html: (r) =>
                    `<code>${escapeHtml((r.labels.uuid || '').slice(0, 24))}</code>` },
                { label: 'PID', html: (r) => escapeHtml(r.labels.pid || '—') },
                { label: 'Prozess', html: (r) => escapeHtml(r.labels.pname || '—') },
                { label: 'Maschine', html: (r) => `<code>${escapeHtml(r.node)}</code>` },
                { label: 'VRAM', num: true, html: (r) => fmtBytes(r.value) },
            ], procs)));
    }
}
