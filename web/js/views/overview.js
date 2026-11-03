import { api } from '../api.js';
import {
    lineChart, seriesTable, sparkline, meter, fmtBytes, fmtNum, seriesColor,
    escapeHtml,
} from '../chart.js';
import {
    card, tile, chartCard, table, statusHtml, sevLevel, sevText,
    pct, h, spinner, errorBanner, fmtDuration, toast,
} from '../ui.js';

/* Übersicht.
 *
 * Gruppiert nach Maschine, nicht nach Metrik: wer ein Problem sucht, denkt in
 * "welcher Rechner", nicht in "welche Spalte". Jede Maschine ist eine Karte
 * mit ihren GPUs, ihren Diensten und dem Befehl, um sich aufzuschalten.
 */

export async function render(root, state) {
    root.appendChild(spinner());
    let ov, inc, trends;
    try {
        [ov, inc] = await Promise.all([api.overview(), api.incidents({ limit: 100 })]);
        // Verläufe für die Sparklines. Bewusst grob: sie sollen die Richtung
        // zeigen, nicht Details - dafür gibt es die eigenen Seiten.
        trends = await api.queryMulti({
            start: '-' + state.range, max_points: 60,
            queries: [
                { metric: 'gpu.temp_c', agg: 'avg' },
                { metric: 'gpu.util_pct', agg: 'avg' },
                { metric: 'host.cpu_pct', agg: 'avg' },
                { metric: 'llm.requests_running', agg: 'avg' },
                { metric: 'llm.requests_waiting', agg: 'avg' },
                { metric: 'llm.kv_cache_usage_pct', agg: 'avg' },
            ],
        }).catch(() => null);
    } catch (e) {
        root.textContent = '';
        root.appendChild(errorBanner(e));
        return;
    }
    root.textContent = '';

    const open = inc.filter((i) => !i.ended_at);
    root.appendChild(statusBanner(open, ov));
    root.appendChild(summaryTiles(ov, open));

    // --- Maschinen
    const nodes = ov.nodes?.detail || [];
    const gpusByNode = groupBy(ov.gpus || [], (g) => g.node);
    const incByNode = groupIncidents(open);
    const trendIdx = indexTrends(trends);

    root.appendChild(h(`<div class="section-head">
        <h2>Maschinen</h2>
        <span class="hint">${nodes.length} bekannt · Zeitraum ${state.range}</span>
      </div>`));

    const grid = h('<div class="grid cols-2"></div>');
    for (const n of nodes) {
        grid.appendChild(machineCard(n, gpusByNode.get(n.name) || [],
            incByNode, trendIdx, ov));
    }
    root.appendChild(grid);

    // --- Inference
    root.appendChild(h(`<div class="section-head">
        <h2>Inference</h2>
        <span class="hint">letzte Stunde</span>
      </div>`));
    root.appendChild(inferencePanel(ov, trends, trendIdx));

    // --- Offene Incidents
    if (open.length) {
        root.appendChild(h(`<div class="section-head">
            <h2>Offene Incidents</h2>
            <span class="hint">${open.length} · vollständig mit Kontext
              unter „Diagnose“</span>
          </div>`));
        root.appendChild(incidentList(open));
    }
}

/* ------------------------------------------------------------------ Banner */

function statusBanner(open, ov) {
    const by = (sev) => open.filter((i) => i.severity === sev);
    const crit = by('critical');
    const errs = by('error');
    const warns = by('warning');
    // Info-Incidents sind Hinweise. Sie als Warnung zu zählen entwertet das
    // Banner: nach drei Tagen sieht niemand mehr hin.
    const infos = open.filter((i) => ['info', 'debug'].includes(i.severity));
    const infoTxt = infos.length
        ? ` · ${infos.length} Hinweis${infos.length === 1 ? '' : 'e'}` : '';

    const alerts = ov.alerts || {};
    const alertNote = alerts.enabled
        ? `<span class="pill good" title="Benachrichtigt wird ab Schwere
           ${escapeHtml(alerts.min_severity)}">Alarm an: ${
            escapeHtml((alerts.channels || []).join(', '))}</span>`
        : `<span class="pill" title="Einrichten in deploy/.env">Alarm aus</span>`;

    if (crit.length || errs.length) {
        const lead = [crit.length && `${crit.length} kritisch`,
            errs.length && `${errs.length} Fehler`,
            warns.length && `${warns.length} Warnungen`].filter(Boolean).join(', ');
        const top = [...crit, ...errs];
        return h(`<div class="banner critical">${statusHtml('critical', lead)}
            <span style="flex:1">${escapeHtml(
                top.slice(0, 2).map((i) => i.title).join(' · '))}${
            top.length > 2 ? ` · und ${top.length - 2} weitere` : ''}</span>
            ${alertNote}</div>`);
    }
    if (warns.length) {
        return h(`<div class="banner warning">${statusHtml('warning',
            `${warns.length} Warnung${warns.length === 1 ? '' : 'en'}${infoTxt}`)}
            <span style="flex:1">${escapeHtml(
                warns.slice(0, 3).map((i) => i.title).join(' · '))}</span>
            ${alertNote}</div>`);
    }
    return h(`<div class="banner good">${statusHtml('good',
        infos.length ? `Nichts Kritisches${infoTxt}` : 'Alles ruhig')}
        <span style="flex:1">${escapeHtml(infos.length
            ? infos.slice(0, 2).map((i) => i.title).join(' · ')
            : 'Keine offenen Incidents.')}</span>
        ${alertNote}</div>`);
}

/* ------------------------------------------------------------------- Tiles */

function summaryTiles(ov, open) {
    const t = h('<div class="grid tiles"></div>');
    const g = ov.gpu_summary || {};
    const nodes = ov.nodes || {};
    const gpus = ov.gpus || [];

    t.appendChild(tile('Maschinen online',
        `${nodes.online ?? 0}<span class="unit"> / ${nodes.total ?? 0}</span>`,
        (nodes.stale || []).length ? `stumm: ${nodes.stale.join(', ')}` : 'alle melden',
        (nodes.stale || []).length ? '' : 'good'));

    // VRAM nur über die Karten summieren, die überhaupt welchen melden -
    // sonst wäre der Prozentsatz still falsch.
    const withMem = gpus.filter((x) => x.mem_total);
    const used = withMem.reduce((a, x) => a + (x.mem_used || 0), 0);
    const total = withMem.reduce((a, x) => a + (x.mem_total || 0), 0);
    const missing = gpus.length - withMem.length;
    t.appendChild(tile('GPUs', `${gpus.length}`,
        total
            ? `${fmtBytes(used)} / ${fmtBytes(total)}` +
              (missing ? ` · ${missing} ohne Speicherangabe` : '')
            : 'keine Speicherangaben'));

    t.appendChild(tile('Höchste GPU-Temperatur',
        g.max_temp_c != null ? `${g.max_temp_c.toFixed(0)}<span class="unit"> °C</span>`
            : '—',
        thermalNote(gpus)));

    const per = ov.llm?.per_service || [];
    const reqs = per.reduce((a, s) => a + (s.requests || 0), 0);
    const worst = per.reduce((a, s) =>
        s.p95_ttft_ms != null && s.p95_ttft_ms > a ? s.p95_ttft_ms : a, 0);
    t.appendChild(tile('TTFT p95',
        worst ? `${fmtNum(worst, 0)}<span class="unit"> ms</span>` : '—',
        reqs ? `aus ${reqs} gemessenen Requests (1 h)`
            : 'kein aimon-tap im Pfad – nur Histogramme'));

    const ot = Object.values(ov.openterminal || {})[0] || {};
    const hr = ot['openterminal.headroom_pct'];
    t.appendChild(tile('OpenTerminal-Headroom',
        hr != null ? `${hr.toFixed(0)}<span class="unit"> %</span>` : '—',
        ot['openterminal.instances_running'] != null
            ? `${ot['openterminal.instances_running']} / ${
                ot['openterminal.instances_max'] ?? '?'} Instanzen`
            : 'Autoscaler meldet nichts',
        hr != null && hr > 20 ? 'good' : ''));

    const blocked = ov.privacy?.events_1h?.['privacy.blocked']?.occurrences ?? 0;
    const rate = (ov.privacy?.latest || []).find(
        (r) => r.metric === 'privacy.block_rate_pct');
    t.appendChild(tile('Geblockte Suchen (1 h)', `${blocked}`,
        rate ? `Blockrate ${pct(rate.value, 1)}` : 'Privacy-Proxy meldet nichts'));

    return t;
}

function thermalNote(gpus) {
    const hot = gpus.filter((g) => g.throttle_thermal > 0 || g.throttle_hw > 0);
    if (hot.length) {
        return `throttelt: ${hot.map((g) => `${g.node}/gpu${g.gpu}`).join(', ')}`;
    }
    // Power-Cap unter Last ist Normalbetrieb und gehört nicht in eine
    // Warnmeldung - sonst steht hier dauerhaft etwas Beunruhigendes.
    const capped = gpus.filter((g) => g.throttle_power > 0 && (g.util_pct ?? 0) >= 30);
    if (capped.length) {
        return `${capped.length} Karte${capped.length === 1 ? '' : 'n'} am ` +
            `Leistungslimit (normal unter Last)`;
    }
    return 'kein Throttling';
}

/* ---------------------------------------------------------- Maschinenkarte */

function machineCard(n, gpus, incByNode, trends, ov) {
    const incidents = incByNode.get(n.name) || [];
    const worst = worstSeverity(incidents);
    const offline = n.age_s > 600;
    const stale = n.age_s > 90;

    const cls = worst === 'critical' || worst === 'error' ? 'alert'
        : worst === 'warning' ? 'warn' : '';
    const el = h(`<section class="machine ${cls} ${offline ? 'offline' : ''}">
        <div class="machine-head"></div>
        <div class="machine-body"></div>
      </section>`);

    const head = el.querySelector('.machine-head');
    head.appendChild(h(`<div>
        <div class="name">${escapeHtml(n.name)}</div>
        ${n.hostname && n.hostname !== n.name
            ? `<div class="host">${escapeHtml(n.hostname)}</div>` : ''}
      </div>`));
    head.appendChild(h('<div class="spacer"></div>'));
    head.appendChild(h(`<div>${stale
        ? statusHtml(offline ? 'critical' : 'warning',
            `stumm seit ${fmtDuration(n.age_s)}`)
        : statusHtml('good', 'online')}</div>`));

    const body = el.querySelector('.machine-body');

    // Zugang: ein Befehl zum Kopieren, nicht eine IP zum Abtippen.
    const cmd = sshCommand(n);
    const labels = Object.entries(n.labels || {})
        .filter(([k]) => !['ip', 'ips', 'ssh_user', 'ssh_port'].includes(k))
        .map(([k, v]) => `${k}=${v}`);
    const meta = h(`<div class="facts" style="padding:8px 0 4px">
        ${cmd ? `<span class="sshline"><span>${escapeHtml(cmd)}</span>
            <button type="button" title="Befehl kopieren">kopieren</button></span>` : ''}
        ${n.agent_version ? `<span class="fact">Agent <b>${
            escapeHtml(n.agent_version)}</b></span>` : ''}
        ${n.service_count ? `<span class="fact">Dienste <b>${
            n.service_count}</b></span>` : ''}
        ${labels.length ? `<span class="fact" style="color:var(--text-muted)">${
            escapeHtml(labels.join(' · '))}</span>` : ''}
      </div>`);
    const copyBtn = meta.querySelector('button');
    if (copyBtn) copyBtn.addEventListener('click', () => copyText(cmd));
    body.appendChild(meta);

    // Welche Dienste laufen hier - das ist die zweite Frage nach "lebt die
    // Maschine noch", und sie stand bisher nur als nackte Zahl da.
    const svcs = (ov.services_by_node || {})[n.name] || [];
    if (svcs.length) {
        body.appendChild(h(`<div class="svc-chips">${svcs.map((s) => {
            const down = s.status !== 'online';
            return `<span class="svc-chip ${down ? 'down' : ''}"
                title="${escapeHtml(s.key)}${s.version
                ? ' · Version ' + escapeHtml(s.version) : ''}">
              <span class="dot" style="background:${kindColor(s.kind)}"></span>
              ${escapeHtml(kindLabel(s.kind))}${s.model
                ? ` <span style="color:var(--text-muted)">${
                    escapeHtml(s.model)}</span>` : ''}
            </span>`;
        }).join('')}</div>`));
    }

    // Host-Kennzahlen mit Verlauf
    const cpu = trends.host.get(n.name);
    if (cpu && cpu.length) {
        const last = lastValue(cpu);
        body.appendChild(h(`<div class="gpu-row">
            <div class="gname"><b>Host</b><span>CPU</span></div>
            <div class="cell"><span class="k">Auslastung</span>
              <span class="v">${fmtNum(last, 0)} <small>%</small></span>
              ${meter(last, { warn: 85, crit: 95 })}</div>
            <div class="cell"></div>
            <div class="trail">${sparkline(cpu, { width: 92, height: 24,
                label: 'CPU-Verlauf', yMin: 0, yMax: 100 })}</div>
          </div>`));
    }

    if (!gpus.length && !cpu) {
        body.appendChild(h(`<div class="empty" style="padding:10px 0">
            Keine GPU- oder Host-Werte.</div>`));
    }

    for (const g of gpus) {
        body.appendChild(gpuRow(g, trends));
    }

    if (incidents.length) {
        body.appendChild(h(`<div style="padding-top:9px">${incidents.slice(0, 3)
            .map((i) => `<div style="font-size:11.5px;margin-top:3px">${
                statusHtml(sevLevel(i.severity), sevText(i.severity))}
              <span style="color:var(--text-secondary)">${escapeHtml(i.title)}</span>
            </div>`).join('')}${incidents.length > 3
            ? `<div class="source-note">und ${incidents.length - 3} weitere</div>` : ''}
          </div>`));
    }
    return el;
}

function gpuRow(g, trends) {
    const key = `${g.node}\u0000${g.gpu}`;
    const temp = trends.temp.get(key) || [];
    const stale = !g.fresh;

    // Speicher: fehlt er, sagen wir warum. Ein nackter Strich sieht aus wie
    // ein Ausfall des Agents, ist aber bei gemeinsamem Speicher der Normalfall.
    let memHtml;
    if (g.mem_total) {
        const unified = g.memory_kind === 'unified';
        memHtml = `<span class="k">${unified ? 'Speicher (gemeinsam)' : 'VRAM'}</span>
            <span class="v">${fmtNum(g.mem_pct, 0)} <small>%</small>
              <small>${fmtBytes(g.mem_used)} / ${fmtBytes(g.mem_total)}</small></span>
            ${meter(g.mem_pct, unified ? { warn: 95, crit: 99 } : {})}`;
    } else if (g.memory_kind === 'unavailable') {
        memHtml = `<span class="k">Speicher</span>
            <span class="v" style="font-weight:400;color:var(--text-muted)"
              title="Weder --query-gpu noch nvidia-smi -q liefern Werte. Bei
gemeinsamem Speicher ist der Arbeitsspeicher des Hosts die Grenze.">
              vom Treiber nicht gemeldet</span>`;
    } else {
        memHtml = `<span class="k">Speicher</span>
            <span class="v" style="font-weight:400;color:var(--text-muted)">—</span>`;
    }

    return h(`<div class="gpu-row">
        <div class="gname">
          <b>GPU ${escapeHtml(String(g.gpu))}</b>
          <span title="${escapeHtml(g.name || '')}">${escapeHtml(g.name || '—')}</span>
        </div>
        <div class="cell">
          <span class="k">Temperatur${stale ? ' (alt)' : ''}</span>
          <span class="v">${g.temp_c != null ? fmtNum(g.temp_c, 0) : '—'}
            <small>°C</small>
            ${g.util_pct != null
            ? `<small>· ${fmtNum(g.util_pct, 0)} % Last</small>` : ''}</span>
          ${meter(g.temp_c, { warn: 83, crit: 88 })}
        </div>
        <div class="cell">${memHtml}</div>
        <div class="trail">
          ${sparkline(temp, { width: 92, height: 24,
        label: `Temperaturverlauf GPU ${g.gpu}`,
        color: g.temp_c >= 83 ? 'var(--st-warning)' : 'var(--series-1)' })}
          ${gpuStateChip(g)}
        </div>
      </div>`);
}

/* Ein Wort zum Zustand. Power-Cap unter Last ist neutral, nicht Warnung. */
function gpuStateChip(g) {
    if (!g.fresh) {
        return statusHtml('warning', fmtDuration(g.age_s) + ' alt');
    }
    const bad = [];
    if (g.throttle_thermal > 0) bad.push('thermisch');
    if (g.throttle_hw > 0) bad.push('Hardware');
    if (bad.length) return statusHtml('warning', bad.join('/'));
    if (g.throttle_power > 0) {
        return (g.util_pct ?? 0) >= 30
            ? `<span class="status muted" title="Die Karte arbeitet an ihrem
Leistungslimit. Unter Last ist das der Normalbetrieb."><span class="ico"
>○</span>am Limit</span>`
            : statusHtml('warning', 'Limit im Leerlauf');
    }
    return statusHtml('good', 'ok');
}

/* -------------------------------------------------------------- Inference */

function inferencePanel(ov, trends, idx) {
    const per = ov.llm?.per_service || [];
    const wrap = h('<div class="grid cols-2"></div>');

    if (per.length) {
        wrap.appendChild(card('Pro Dienst', ov.llm?.note || '', table([
            { label: 'Dienst', html: (r) => `<code>${escapeHtml(r.service)}</code>` },
            { label: 'Requests', num: true, key: 'requests' },
            { label: 'TTFT ⌀', num: true, html: (r) =>
                r.avg_ttft_ms == null ? '—' : `${fmtNum(r.avg_ttft_ms, 0)} ms` },
            { label: 'p95', num: true, html: (r) =>
                r.p95_ttft_ms == null ? '—' : `${fmtNum(r.p95_ttft_ms, 0)} ms` },
            { label: 'Prompt ⌀', num: true, html: (r) =>
                r.avg_prompt_tokens == null ? '—'
                    : `${fmtNum(r.avg_prompt_tokens, 0)} Tok` },
            { label: 'Fehler', num: true, html: (r) => r.errors
                ? statusHtml('serious', String(r.errors)) : '0' },
        ], per)));
    } else {
        wrap.appendChild(card('Pro Dienst',
            'Ohne aimon-tap gibt es keine Messung pro Request.',
            `<div class="empty">Keine Request-Daten.<br>
             <span style="font-size:11.5px">Der Tap zwischen OpenWebUI und dem
             Backend liefert TTFT und Prompt-Längen pro Modell. Bis dahin
             stehen die vLLM-Histogramme unter „Inference“.</span></div>`));
    }

    if (trends) {
        const load = [
            ...toSeries(trends.results[3], (s) => `laufend ${short(s.service)}`),
        ];
        const waiting = toSeries(trends.results[4],
            (s) => `wartend ${short(s.service)}`, 1);
        const kv = toSeries(trends.results[5], (s) => `KV-Cache ${short(s.service)}`, 2);
        const all = [...load, ...waiting];
        if (all.length) {
            wrap.appendChild(chartCard('Auslastung der Inference-Server',
                'Wartende Requests gehen voll in die TTFT der Benutzer ein.',
                (box) => lineChart(box, all, { height: 180, yMin: 0,
                    fmt: (v) => fmtNum(v, 0), note: sourceNote(trends.results[3]) }),
                () => seriesTable(all, (v) => fmtNum(v, 1))));
        } else if (kv.length) {
            wrap.appendChild(chartCard('KV-Cache', '',
                (box) => lineChart(box, kv, { unit: '%', height: 180, yMin: 0,
                    yMax: 100, fmt: (v) => fmtNum(v, 0) }),
                () => seriesTable(kv, (v) => fmtNum(v, 1))));
        }
    }
    return wrap;
}

function incidentList(open) {
    return card('', '', open.slice(0, 15).map((i) => `
        <div class="incident">
          <div>${statusHtml(sevLevel(i.severity), sevText(i.severity))}</div>
          <div class="body">
            <div class="title">${escapeHtml(i.title)}</div>
            <div class="meta"><code>${escapeHtml(i.rule)}</code> ·
              ${escapeHtml(i.subject)} · seit ${fmtDuration(i.age_s)}</div>
          </div>
        </div>`).join(''));
}

/* -------------------------------------------------------------- Hilfsmittel */

export function sshCommand(node) {
    const l = node.labels || {};
    let host = l.ip || node.hostname || node.name;
    if (!host) return '';
    if (host.includes(':') && !host.startsWith('[')) host = `[${host}]`;
    const parts = ['ssh'];
    if (l.ssh_port && l.ssh_port !== '22') parts.push('-p', l.ssh_port);
    parts.push(l.ssh_user ? `${l.ssh_user}@${host}` : host);
    return parts.join(' ');
}

export async function copyText(text) {
    try {
        await navigator.clipboard.writeText(text);
        toast('Kopiert: ' + text);
    } catch {
        // Die Clipboard-API gibt es nur über HTTPS oder auf localhost.
        toast('Zwischenablage gesperrt – ' + text);
    }
}

function groupBy(arr, keyFn) {
    const m = new Map();
    for (const x of arr) {
        const k = keyFn(x);
        if (!m.has(k)) m.set(k, []);
        m.get(k).push(x);
    }
    return m;
}

function groupIncidents(open) {
    const m = new Map();
    for (const i of open) {
        // "ki/gpu0", "ki:/mount" und "ki -> dienst" gehören alle zu "ki".
        const node = String(i.subject || '').split(/[/:]| -> /)[0];
        if (!m.has(node)) m.set(node, []);
        m.get(node).push(i);
    }
    return m;
}

function worstSeverity(incidents) {
    const order = ['critical', 'error', 'warning', 'info', 'debug'];
    for (const s of order) {
        if (incidents.some((i) => i.severity === s)) return s;
    }
    return null;
}

function indexTrends(trends) {
    const temp = new Map();
    const util = new Map();
    const host = new Map();
    if (trends) {
        for (const s of trends.results[0]?.series || []) {
            temp.set(`${s.node}\u0000${s.labels.gpu}`, s.points);
        }
        for (const s of trends.results[1]?.series || []) {
            util.set(`${s.node}\u0000${s.labels.gpu}`, s.points);
        }
        for (const s of trends.results[2]?.series || []) {
            host.set(s.node, s.points);
        }
    }
    return { temp, util, host };
}

function lastValue(points) {
    for (let i = points.length - 1; i >= 0; i--) {
        if (points[i][1] != null) return points[i][1];
    }
    return null;
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

/* Zustand einer GPU in einem Satz - auch von anderen Ansichten benutzt. */
export function gpuState(r) {
    if (!r.fresh) {
        return statusHtml('warning', `Daten veraltet (${fmtDuration(r.age_s)})`);
    }
    const bad = [];
    if (r.throttle_thermal > 0) bad.push('thermisch');
    if (r.throttle_hw > 0) bad.push('Hardware');
    if (bad.length) return statusHtml('warning', 'throttelt: ' + bad.join(', '));
    if (r.throttle_power > 0) {
        return (r.util_pct ?? 0) >= 30
            ? statusHtml('muted', 'am Leistungslimit')
            : statusHtml('warning',
                `Leistungslimit bei ${fmtNum(r.util_pct, 0)} % Last`);
    }
    return statusHtml('good', 'ok');
}


const KIND_LABEL = {
    vllm: 'vLLM', ollama: 'Ollama', comfyui: 'ComfyUI', openwebui: 'OpenWebUI',
    searxng: 'SearXNG', privacy_proxy: 'Privacy-Proxy',
    openterminal_proxy: 'OT-Autoscaler', openterminal_instance: 'OT-Instanz',
    tap: 'aimon-tap', other: 'Dienst',
};

function kindLabel(kind) { return KIND_LABEL[kind] || kind || 'Dienst'; }

function kindColor(kind) {
    return {
        openwebui: 'var(--series-1)', privacy_proxy: 'var(--series-2)',
        vllm: 'var(--series-3)', ollama: 'var(--series-4)',
        comfyui: 'var(--series-5)', searxng: 'var(--series-6)',
        openterminal_proxy: 'var(--series-7)', tap: 'var(--series-8)',
    }[kind] || 'var(--axis)';
}
