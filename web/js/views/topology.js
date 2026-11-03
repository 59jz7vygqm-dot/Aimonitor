import { api } from '../api.js';
import { escapeHtml, fmtNum } from '../chart.js';
import { card, spinner, errorBanner, h, table, statusHtml, fmtDuration } from '../ui.js';

/* Dienstgraph.
 *
 * Layout ist geschichtet (Eingang links, Backends rechts), nicht
 * kräftebasiert: bei einem Dutzend Diensten ist ein deterministisches
 * Layout lesbarer und springt nicht bei jedem Neuladen herum.
 */

const KIND_LABEL = {
    vllm: 'vLLM', ollama: 'Ollama', comfyui: 'ComfyUI', openwebui: 'OpenWebUI',
    searxng: 'SearXNG', privacy_proxy: 'Privacy-Proxy',
    openterminal_proxy: 'OT-Autoscaler', openterminal_instance: 'OT-Instanz',
    tap: 'aimon-tap', other: 'Dienst',
};

export async function render(root, state) {
    root.appendChild(spinner());
    let topo, matrix;
    try {
        [topo, matrix] = await Promise.all([
            api.topology({ window_s: 600 }),
            api.matrix({ window_s: 900 }),
        ]);
    } catch (e) {
        root.textContent = '';
        root.appendChild(errorBanner(e));
        return;
    }
    root.textContent = '';

    // Maschinen gehören in den Graphen: die meisten Probe-Kanten gehen von
    // einem Node aus, nicht von einem Dienst. Ohne sie bliebe genau der Teil
    // unsichtbar, um den es geht - wer erreicht wen.
    const graphNodes = topo.nodes.filter((n) =>
        n.type === 'service' || n.type === 'client' || n.type === 'external' ||
        n.type === 'node');
    // Eine Probe-Kante ist gestört, wenn der Check fehlschlägt. Eine
    // Verkehrskante mit ein paar Fehlern unter vielen Requests ist das nicht -
    // sie würde die Störungsanzeige sonst dauerhaft rot färben.
    const failing = topo.edges.filter((e) => e.kind !== 'traffic' && e.ok === false);
    const degraded = topo.edges.filter((e) =>
        e.kind === 'traffic' && e.errors > 0);

    if (failing.length) {
        root.appendChild(h(`<div class="banner critical">${statusHtml('critical',
            `${failing.length} gestörte Verbindungen`)}<span>${escapeHtml(
                failing.slice(0, 3).map((e) =>
                    `${e.source.replace(/^\w+:/, '')} → ${
                        e.target.replace(/^\w+:/, '')}`).join(' · '))}</span></div>`));
    } else if (degraded.length) {
        const tot = degraded.reduce((a, e) => a + (e.requests || 0), 0);
        const err = degraded.reduce((a, e) => a + (e.errors || 0), 0);
        root.appendChild(h(`<div class="banner warning">${statusHtml('warning',
            'Alle Dienste erreichbar')}<span>Aber ${err} von ${tot}
            Inference-Requests sind fehlgeschlagen.</span></div>`));
    } else {
        root.appendChild(h(`<div class="banner good">${statusHtml('good',
            'Alle gemessenen Verbindungen in Ordnung')}<span>${
                topo.edges.length} Kanten im Fenster.</span></div>`));
    }

    const graphCard = card('Dienstgraph', topo.note, '<div class="topo"></div>');
    root.appendChild(graphCard);
    drawGraph(graphCard.querySelector('.topo'), topo, graphNodes);

    // --- Kantenliste: die Zahlen hinter den Pfeilen
    root.appendChild(card('Verbindungen',
        'kind=probe sind aktive Checks der Agents, kind=traffic ist beobachteter ' +
        'Inference-Verkehr.',
        table([
            { label: 'Von', html: (e) => `<code>${escapeHtml(
                e.source.replace(/^(node|svc|client|ext):/, ''))}</code>` },
            { label: 'Nach', html: (e) => `<code>${escapeHtml(
                e.target.replace(/^(node|svc|client|ext):/, ''))}</code>` },
            { label: 'Art', key: 'kind' },
            { label: 'Zustand', html: (e) => edgeState(e) },
            { label: 'Latenz', num: true, html: (e) => e.latency_ms === null
                ? '—' : `${fmtNum(e.latency_ms, 1)} ms` },
            { label: 'p95', num: true, html: (e) =>
                e.window_p95_latency_ms ? `${fmtNum(e.window_p95_latency_ms, 1)} ms` : '—' },
            { label: 'Checks (Fenster)', num: true, html: (e) =>
                e.window_probes ? `${e.window_probes - (e.window_fails || 0)}/${
                    e.window_probes}` : (e.requests ? `${e.requests} Req` : '—') },
            { label: 'Fehler gesamt', num: true, html: (e) =>
                e.total_fails !== undefined ? String(e.total_fails)
                    : (e.errors !== undefined ? String(e.errors) : '—') },
        ], [...failing, ...topo.edges.filter((e) => e.ok !== false)],
           { scroll: true })));

    // --- Erreichbarkeitsmatrix
    if (matrix.cells.length) {
        const byKey = new Map(matrix.cells.map((c) => [c.src + '\u0000' + c.dst, c]));
        const head = `<thead><tr><th>von \\ nach</th>${matrix.targets
            .map((t) => `<th class="num">${escapeHtml(t.replace(/^https?:\/\//, ''))}` +
                `</th>`).join('')}</tr></thead>`;
        const body = matrix.sources.map((s) =>
            `<tr><td><code>${escapeHtml(s)}</code></td>${matrix.targets.map((t) => {
                const c = byKey.get(s + '\u0000' + t);
                if (!c) return '<td class="num" style="color:var(--text-muted)">·</td>';
                const ok = c.success_pct >= 99.9;
                const part = c.success_pct > 0;
                const bg = ok ? 'transparent'
                    : part ? 'color-mix(in srgb, var(--st-warning) 22%, transparent)'
                        : 'color-mix(in srgb, var(--st-critical) 22%, transparent)';
                const txt = ok ? `${fmtNum(c.avg_latency_ms, 1)} ms`
                    : `${c.success_pct.toFixed(0)} %`;
                const title = `${c.ok}/${c.probes} Checks erfolgreich` +
                    (c.last_error ? `, letzter Fehler: ${c.last_error}` : '');
                return `<td class="num cell" style="background:${bg}" title="${
                    escapeHtml(title)}">${ok ? '' : '▲ '}${escapeHtml(txt)}</td>`;
            }).join('')}</tr>`).join('');
        root.appendChild(card('Erreichbarkeitsmatrix',
            'Zelle zeigt die mittlere Latenz, wenn alle Checks durchkamen – sonst die ' +
            'Erfolgsquote mit Warnsymbol. Leer heißt: diese Richtung wird nicht geprüft.',
            `<div class="table-wrap"><table class="data matrix">${head}<tbody>${
                body}</tbody></table></div>`));
    }
}

function drawGraph(container, topo, nodes) {
    container.textContent = '';

    const byLayer = new Map();
    for (const n of nodes) {
        // Maschinen ganz links (sie proben), dann Clients, dann die Dienst-Layer.
        const l = n.type === 'node' ? -2 : n.type === 'client' ? -1 : (n.layer ?? 3);
        if (!byLayer.has(l)) byLayer.set(l, []);
        byLayer.get(l).push(n);
    }
    const layers = [...byLayer.keys()].sort((a, b) => a - b);
    if (!layers.length) {
        container.innerHTML = '<div class="empty">Noch keine Dienste bekannt.</div>';
        return;
    }

    const BW = 186, BH = 48, GX = 86, GY = 20;
    const maxRows = Math.max(...layers.map((l) => byLayer.get(l).length));
    const width = layers.length * BW + (layers.length - 1) * GX + 24;
    const height = Math.max(160, maxRows * (BH + GY) + 24);

    const pos = new Map();
    layers.forEach((l, li) => {
        const col = byLayer.get(l);
        const colH = col.length * (BH + GY) - GY;
        col.forEach((n, i) => {
            pos.set(n.id, {
                x: 12 + li * (BW + GX),
                y: (height - colH) / 2 + i * (BH + GY),
            });
        });
    });

    const NS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', 'Dienstgraph');
    svg.style.maxWidth = width + 'px';

    // Kanten zuerst, damit Knoten darüber liegen
    const seen = new Set();
    for (const e of topo.edges) {
        const a = pos.get(e.source), b = pos.get(e.target);
        if (!a || !b) continue;
        const k = e.source + '>' + e.target + e.kind;
        if (seen.has(k)) continue;
        seen.add(k);
        const x1 = a.x + BW, y1 = a.y + BH / 2;
        const x2 = b.x, y2 = b.y + BH / 2;
        const mx = (x1 + x2) / 2;
        const p = document.createElementNS(NS, 'path');
        p.setAttribute('class', 'edge ' +
            (e.kind === 'traffic' ? 'traffic' : e.ok === false ? 'fail' : 'ok'));
        p.setAttribute('d', `M${x1},${y1}C${mx},${y1} ${mx},${y2} ${x2},${y2}`);
        const lat = e.latency_ms !== null && e.latency_ms !== undefined
            ? `${e.latency_ms.toFixed(1)} ms` : 'keine Latenz';
        const desc = e.kind === 'traffic'
            ? `${e.requests ?? 0} Requests, ${e.errors ?? 0} Fehler, ⌀ TTFT ${lat}`
            : (e.ok === false ? 'FEHLER' : lat);
        p.appendChild(mkTitle(NS, `${e.source} → ${e.target} (${e.kind}): ${desc}`));
        svg.appendChild(p);
    }

    for (const n of nodes) {
        const p = pos.get(n.id);
        if (!p) continue;
        const g = document.createElementNS(NS, 'g');
        g.setAttribute('class', 'node');
        g.setAttribute('transform', `translate(${p.x},${p.y})`);

        const r = document.createElementNS(NS, 'rect');
        r.setAttribute('class', 'nodebox ' +
            (n.status === 'online' ? '' : n.status === 'stale' ? 'stale' : 'offline'));
        r.setAttribute('width', BW);
        r.setAttribute('height', BH);
        r.setAttribute('rx', 8);
        g.appendChild(r);

        // Farbstreifen = Art des Dienstes; Text daneben nennt sie ebenfalls,
        // damit die Information nicht an der Farbe hängt.
        const bar = document.createElementNS(NS, 'rect');
        bar.setAttribute('x', 0);
        bar.setAttribute('y', 0);
        bar.setAttribute('width', 4);
        bar.setAttribute('height', BH);
        bar.setAttribute('rx', 2);
        bar.setAttribute('fill', kindColor(n.kind, n.type));
        g.appendChild(bar);

        const t1 = document.createElementNS(NS, 'text');
        t1.setAttribute('class', 'nlabel');
        t1.setAttribute('x', 12);
        t1.setAttribute('y', 20);
        t1.textContent = clip(n.label, 26);
        g.appendChild(t1);

        const t2 = document.createElementNS(NS, 'text');
        t2.setAttribute('class', 'nkind');
        t2.setAttribute('x', 12);
        t2.setAttribute('y', 35);
        const stTxt = n.status === 'online' ? '' :
            n.status === 'stale' ? ' · ▲ stumm' : ' · ■ offline';
        const typeLabel = n.type === 'node' ? 'Maschine'
            : n.type === 'client' ? 'Client'
            : n.type === 'external' ? 'externer Endpunkt'
            : (KIND_LABEL[n.kind] || n.kind || n.type);
        t2.textContent = clip(typeLabel + (n.node ? ' auf ' + n.node : '') + stTxt, 34);
        g.appendChild(t2);

        g.appendChild(mkTitle(NS, [
            n.key || n.label,
            n.kind ? 'Art: ' + (KIND_LABEL[n.kind] || n.kind) : '',
            n.node ? 'Maschine: ' + n.node : '',
            n.version ? 'Version: ' + n.version : '',
            n.status ? 'Zustand: ' + n.status : '',
            n.last_seen_age_s !== null && n.last_seen_age_s !== undefined
                ? 'zuletzt gesehen vor ' + fmtDuration(n.last_seen_age_s) : '',
        ].filter(Boolean).join('\n')));
        svg.appendChild(g);
    }

    container.appendChild(svg);

    const lg = h(`<div class="legend">
        <span class="item"><span class="swatch" style="background:${
            kindColor(null, 'node')}"></span>Maschine</span>
        ${Object.entries(KIND_LABEL).slice(0, 9).map(([k, v]) =>
            `<span class="item"><span class="swatch" style="background:${
                kindColor(k)}"></span>${escapeHtml(v)}</span>`).join('')}
        <span class="item"><span class="swatch" style="background:var(--st-critical)">
        </span>gestörte Kante</span>
        <span class="item"><span class="swatch" style="background:var(--series-1)">
        </span>echter Inference-Verkehr</span>
      </div>`);
    container.appendChild(lg);
}

function mkTitle(NS, text) {
    const t = document.createElementNS(NS, 'title');
    t.textContent = text;
    return t;
}

function kindColor(kind, type) {
    if (type === 'node') return 'var(--text-secondary)';
    if (type === 'client') return 'var(--series-1)';
    const map = {
        openwebui: 'var(--series-1)',
        privacy_proxy: 'var(--series-2)',
        vllm: 'var(--series-3)',
        ollama: 'var(--series-4)',
        comfyui: 'var(--series-5)',
        searxng: 'var(--series-6)',
        openterminal_proxy: 'var(--series-7)',
        tap: 'var(--series-8)',
    };
    return map[kind] || 'var(--axis)';
}

function clip(s, n) {
    s = String(s || '');
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
}

/* Zustand einer Kante. Probe und Verkehr messen Verschiedenes und dürfen
 * deshalb nicht denselben Text bekommen. */
function edgeState(e) {
    if (e.kind === 'traffic') {
        const n = e.requests ?? 0;
        const err = e.errors ?? 0;
        if (!err) return statusHtml('good', `${n} Requests, keine Fehler`);
        const rate = n ? (err / n) * 100 : 0;
        return statusHtml(rate >= 10 ? 'critical' : 'warning',
            `${err} von ${n} Requests fehlgeschlagen (${rate.toFixed(1)} %)`);
    }
    if (e.ok) return statusHtml('good', 'ok');
    return statusHtml('critical', e.last_ok_age_s !== null &&
        e.last_ok_age_s !== undefined
        ? `Fehler, letzter Erfolg vor ${fmtDuration(e.last_ok_age_s)}`
        : 'Fehler, nie erfolgreich');
}
