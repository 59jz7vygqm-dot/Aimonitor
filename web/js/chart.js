/* Minimaler SVG-Chart-Renderer.
 *
 * Eigenbau statt Bibliothek, damit das UI ohne Build-Schritt und ohne
 * CDN-Abhängigkeit läuft - ein Homelab-Dashboard soll auch ohne Internet
 * funktionieren.
 *
 * Regeln, die hier fest verdrahtet sind:
 *  - 2px Linien, Marker >= 8px, Datenenden abgerundet
 *  - Crosshair + Tooltip auf jedem Linien-/Flächenchart
 *  - Legende ab zwei Serien, bis vier Serien zusätzlich Direktlabels
 *  - Text trägt Text-Farben, nie die Serienfarbe
 *  - eine Achse, niemals zwei y-Skalen
 */

const SERIES_VARS = [
    '--series-1', '--series-2', '--series-3', '--series-4',
    '--series-5', '--series-6', '--series-7', '--series-8',
];

export function seriesColor(i) {
    return `var(${SERIES_VARS[i % SERIES_VARS.length]})`;
}

const SVG_NS = 'http://www.w3.org/2000/svg';

function el(name, attrs = {}, parent = null) {
    const n = document.createElementNS(SVG_NS, name);
    for (const [k, v] of Object.entries(attrs)) {
        if (v !== null && v !== undefined) n.setAttribute(k, String(v));
    }
    if (parent) parent.appendChild(n);
    return n;
}

export function fmtNum(v, digits) {
    if (v === null || v === undefined || Number.isNaN(v)) return '—';
    const a = Math.abs(v);
    if (digits !== undefined) return v.toFixed(digits);
    if (a >= 1e9) return (v / 1e9).toFixed(1) + 'G';
    if (a >= 1e6) return (v / 1e6).toFixed(1) + 'M';
    if (a >= 1e4) return (v / 1e3).toFixed(0) + 'k';
    if (a >= 100) return v.toFixed(0);
    if (a >= 10) return v.toFixed(1);
    if (a >= 1) return v.toFixed(2);
    if (a === 0) return '0';
    return v.toPrecision(2);
}

export function fmtBytes(v) {
    if (v === null || v === undefined || Number.isNaN(v)) return '—';
    const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    let n = v, i = 0;
    while (Math.abs(n) >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + ' ' + u[i];
}

export function fmtDuration(s) {
    if (s === null || s === undefined) return '—';
    s = Math.round(s);
    if (s < 60) return s + ' s';
    if (s < 3600) return Math.floor(s / 60) + ' min';
    if (s < 86400) return Math.floor(s / 3600) + ' h ' + Math.floor((s % 3600) / 60) + ' min';
    return Math.floor(s / 86400) + ' d ' + Math.floor((s % 86400) / 3600) + ' h';
}

function timeTicks(t0, t1, count) {
    const span = t1 - t0;
    const steps = [
        1e3, 5e3, 15e3, 30e3, 60e3, 300e3, 900e3, 1800e3, 3600e3,
        2 * 3600e3, 6 * 3600e3, 12 * 3600e3, 86400e3, 7 * 86400e3, 30 * 86400e3,
    ];
    const ideal = span / count;
    const step = steps.find((s) => s >= ideal) || steps[steps.length - 1];
    const out = [];
    for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) out.push(t);
    return { ticks: out, step };
}

function fmtTime(ms, step) {
    const d = new Date(ms);
    const p = (n) => String(n).padStart(2, '0');
    if (step >= 86400e3) return `${p(d.getDate())}.${p(d.getMonth() + 1)}.`;
    if (step >= 3600e3) return `${p(d.getHours())}:${p(d.getMinutes())}`;
    return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function niceStep(raw) {
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    return (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
}

/* Achsenwerte.
 *
 * `fmt` wird mitgegeben, weil die Beschriftung über die Schrittweite
 * mitentscheidet: bei einer Serie, die nur zwischen 0 und 1 läuft, ergäbe
 * der rechnerisch schöne Schritt 0,5 die Ticks 0, 0,5 und 1 - und mit einer
 * Formatierung ohne Nachkommastellen stünde an der Achse "0, 1, 1". Deshalb
 * wird die Schrittweite so lange vergrößert, bis alle Beschriftungen
 * verschieden sind.
 */
function valueTicks(min, max, count, fmt) {
    if (min === max) { min -= 1; max += 1; }
    let step = niceStep((max - min) / count);

    const build = (st) => {
        const out = [];
        for (let v = Math.ceil(min / st) * st; v <= max + st * 1e-9; v += st) {
            out.push(Math.abs(v) < st * 1e-9 ? 0 : v);
        }
        return out;
    };

    let ticks = build(step);
    if (typeof fmt === 'function') {
        // Höchstens ein paar Versuche – danach ist die Achse ohnehin leer.
        for (let i = 0; i < 6; i++) {
            const labels = ticks.map((v) => fmt(v));
            if (new Set(labels).size === labels.length && ticks.length >= 2) break;
            step = niceStep(step * 1.5);
            ticks = build(step);
        }
    }
    return ticks;
}

/* Liniendiagramm.
 *
 * series: [{ name, points: [[tsMs, value|null], ...], color?, dashed?, unit? }]
 * opts:   { height, unit, fmt, yMin, yMax, area, note, directLabels }
 */
export function lineChart(container, series, opts = {}) {
    container.textContent = '';
    container.classList.add('chart');

    const visible = series.filter((s) => s.points && s.points.length);
    if (!visible.length) {
        const d = document.createElement('div');
        d.className = 'empty';
        d.textContent = opts.emptyText || 'Keine Daten im gewählten Zeitraum.';
        container.appendChild(d);
        return;
    }

    const height = opts.height || 190;
    const fmt = opts.fmt || fmtNum;
    const padL = 44, padR = opts.directLabels === false ? 10 : 56, padT = 10, padB = 22;
    const width = Math.max(container.clientWidth || 560, 260);
    const iw = width - padL - padR;
    const ih = height - padT - padB;

    let t0 = Infinity, t1 = -Infinity, vMin = Infinity, vMax = -Infinity;
    for (const s of visible) {
        for (const [t, v] of s.points) {
            if (t < t0) t0 = t;
            if (t > t1) t1 = t;
            if (v === null || v === undefined) continue;
            if (v < vMin) vMin = v;
            if (v > vMax) vMax = v;
        }
    }
    if (!Number.isFinite(vMin)) { vMin = 0; vMax = 1; }
    if (opts.yMin !== undefined) vMin = opts.yMin;
    if (opts.yMax !== undefined) vMax = opts.yMax;
    if (vMin === vMax) { vMax = vMin + 1; }
    else {
        const pad = (vMax - vMin) * 0.08;
        if (opts.yMin === undefined) vMin = Math.min(vMin, vMin - pad);
        if (opts.yMax === undefined) vMax = vMax + pad;
        if (opts.yMin === undefined && vMin > 0 && vMin < (vMax - vMin) * 1.5) vMin = 0;
    }
    if (t0 === t1) t1 = t0 + 1000;

    const x = (t) => padL + ((t - t0) / (t1 - t0)) * iw;
    const y = (v) => padT + ih - ((v - vMin) / (vMax - vMin)) * ih;

    const svg = el('svg', {
        viewBox: `0 0 ${width} ${height}`,
        role: 'img',
        'aria-label': opts.ariaLabel || 'Zeitreihe',
    }, container);

    // Gitter (zurückhaltend) und Achsenbeschriftung.
    // opts.yTicks erzwingt feste Werte - nötig bei 0/1-Serien, wo ein
    // automatischer Tick bei 0,5 eine Beschriftung erfände, die es nicht gibt.
    for (const v of (opts.yTicks || valueTicks(vMin, vMax, 4, fmt))) {
        const yy = y(v);
        if (yy < padT - 1 || yy > padT + ih + 1) continue;
        el('line', { class: 'gridline', x1: padL, x2: padL + iw, y1: yy, y2: yy }, svg);
        const tx = el('text', { class: 'tick', x: padL - 6, y: yy + 3.5,
            'text-anchor': 'end' }, svg);
        tx.textContent = fmt(v);
    }
    const { ticks: tt, step } = timeTicks(t0, t1, Math.max(2, Math.floor(iw / 80)));
    for (const t of tt) {
        const xx = x(t);
        const tx = el('text', { class: 'tick', x: xx, y: height - 6,
            'text-anchor': 'middle' }, svg);
        tx.textContent = fmtTime(t, step);
    }
    el('line', { class: 'axisline', x1: padL, x2: padL + iw,
        y1: padT + ih, y2: padT + ih }, svg);

    const hidden = new Set();
    const paths = [];

    function draw() {
        for (const p of paths) p.remove();
        paths.length = 0;
        visible.forEach((s, i) => {
            if (hidden.has(s.name)) return;
            const color = s.color || seriesColor(i);
            // Lücken bewusst als Lücke zeichnen: eine durchgezogene Linie über
            // fehlende Daten würde Messwerte behaupten, die es nicht gibt.
            const segs = [];
            let cur = [];
            for (const [t, v] of s.points) {
                if (v === null || v === undefined || Number.isNaN(v)) {
                    if (cur.length) { segs.push(cur); cur = []; }
                } else cur.push([x(t), y(v)]);
            }
            if (cur.length) segs.push(cur);

            if (opts.area && segs.length) {
                for (const seg of segs) {
                    if (seg.length < 2) continue;
                    const d = `M${seg[0][0]},${padT + ih}` +
                        seg.map(([px, py]) => `L${px},${py}`).join('') +
                        `L${seg[seg.length - 1][0]},${padT + ih}Z`;
                    paths.push(el('path', { class: 'area', d, fill: color }, svg));
                }
            }
            for (const seg of segs) {
                if (seg.length === 1) {
                    paths.push(el('circle', { class: 'marker', cx: seg[0][0],
                        cy: seg[0][1], r: 4, fill: color }, svg));
                    continue;
                }
                const d = `M${seg.map(([px, py]) => `${px},${py}`).join('L')}`;
                paths.push(el('path', {
                    class: 'line', d, stroke: color,
                    'stroke-dasharray': s.dashed ? '5 4' : null,
                }, svg));
            }
            // Direktlabel am letzten Punkt: Identität nicht nur über Farbe.
            if (opts.directLabels !== false && visible.length <= 4 && segs.length) {
                const last = segs[segs.length - 1][segs[segs.length - 1].length - 1];
                const lv = [...s.points].reverse().find(
                    ([, v]) => v !== null && v !== undefined)?.[1];
                const t = el('text', {
                    class: 'dlabel', x: Math.min(last[0] + 6, padL + iw + 4),
                    y: Math.max(padT + 8, Math.min(last[1] + 3.5, padT + ih)),
                    fill: color,
                }, svg);
                t.textContent = fmt(lv);
            }
        });
    }
    draw();

    // ---- Crosshair + Tooltip
    const cross = el('line', { class: 'crosshair', y1: padT, y2: padT + ih,
        x1: -10, x2: -10, visibility: 'hidden' }, svg);
    const dots = el('g', { visibility: 'hidden' }, svg);
    const tip = document.createElement('div');
    tip.className = 'tooltip';
    container.appendChild(tip);

    const hit = el('rect', { x: padL, y: padT, width: iw, height: ih,
        fill: 'transparent', style: 'cursor:crosshair' }, svg);

    function nearest(s, t) {
        let best = null, bd = Infinity;
        for (const p of s.points) {
            if (p[1] === null || p[1] === undefined) continue;
            const d = Math.abs(p[0] - t);
            if (d < bd) { bd = d; best = p; }
        }
        return bd <= (t1 - t0) / 10 ? best : null;
    }

    function move(ev) {
        const r = svg.getBoundingClientRect();
        const px = ((ev.clientX - r.left) / r.width) * width;
        if (px < padL || px > padL + iw) { leave(); return; }
        const t = t0 + ((px - padL) / iw) * (t1 - t0);

        cross.setAttribute('x1', px);
        cross.setAttribute('x2', px);
        cross.setAttribute('visibility', 'visible');
        dots.textContent = '';
        dots.setAttribute('visibility', 'visible');

        const rows = [];
        visible.forEach((s, i) => {
            if (hidden.has(s.name)) return;
            const p = nearest(s, t);
            if (!p) return;
            const color = s.color || seriesColor(i);
            el('circle', { class: 'marker', cx: x(p[0]), cy: y(p[1]), r: 4.5,
                fill: color }, dots);
            rows.push({ name: s.name, v: p[1], color, unit: s.unit || opts.unit || '' });
        });
        if (!rows.length) { leave(); return; }

        rows.sort((a, b) => b.v - a.v);
        const ts = new Date(rows.length ? nearest(visible[0], t)?.[0] || t : t);
        tip.innerHTML =
            `<div class="t-time">${ts.toLocaleTimeString('de-DE')}</div>` +
            rows.map((r) =>
                `<div class="t-row"><span class="k">` +
                `<span class="swatch" style="width:9px;height:9px;border-radius:2px;` +
                `display:inline-block;background:${r.color}"></span>` +
                `${escapeHtml(r.name)}</span>` +
                `<span class="v">${fmt(r.v)}${r.unit ? ' ' + r.unit : ''}</span></div>`
            ).join('');
        tip.classList.add('on');
        const tw = tip.offsetWidth || 150;
        const left = px / width * (container.clientWidth || width);
        tip.style.left = Math.max(2, Math.min(left + 12,
            (container.clientWidth || width) - tw - 2)) + 'px';
        tip.style.top = '6px';
    }

    function leave() {
        cross.setAttribute('visibility', 'hidden');
        dots.setAttribute('visibility', 'hidden');
        tip.classList.remove('on');
    }

    hit.addEventListener('pointermove', move);
    hit.addEventListener('pointerleave', leave);
    hit.addEventListener('pointerdown', move);

    // ---- Legende (ab zwei Serien immer vorhanden)
    if (visible.length >= 2) {
        const lg = document.createElement('div');
        lg.className = 'legend';
        visible.forEach((s, i) => {
            const b = document.createElement('button');
            b.className = 'item';
            b.type = 'button';
            b.setAttribute('aria-pressed', 'true');
            b.style.background = 'transparent';
            b.style.border = 'none';
            b.style.padding = '0';
            b.style.font = 'inherit';
            b.innerHTML = `<span class="swatch" style="background:${
                s.color || seriesColor(i)}"></span><span>${escapeHtml(s.name)}</span>`;
            b.addEventListener('click', () => {
                if (hidden.has(s.name)) hidden.delete(s.name);
                else hidden.add(s.name);
                b.setAttribute('aria-pressed', hidden.has(s.name) ? 'false' : 'true');
                draw();
            });
            lg.appendChild(b);
        });
        container.appendChild(lg);
    }

    if (opts.note) {
        const n = document.createElement('div');
        n.className = 'source-note';
        n.textContent = opts.note;
        container.appendChild(n);
    }
}

/* Horizontales Balkendiagramm für Kategorien (z. B. Blockgründe).
 * rows: [{ label, value, color?, note? }]
 */
export function barChart(container, rows, opts = {}) {
    container.textContent = '';
    container.classList.add('chart');
    if (!rows.length) {
        const d = document.createElement('div');
        d.className = 'empty';
        d.textContent = opts.emptyText || 'Keine Daten.';
        container.appendChild(d);
        return;
    }
    const fmt = opts.fmt || fmtNum;
    const max = Math.max(...rows.map((r) => r.value), 1);
    const tbl = document.createElement('table');
    tbl.className = 'data';
    tbl.innerHTML = `<thead><tr><th>${escapeHtml(opts.labelHead || 'Kategorie')}</th>` +
        `<th style="width:45%"></th><th class="num">${
            escapeHtml(opts.valueHead || 'Anzahl')}</th></tr></thead>`;
    const tb = document.createElement('tbody');
    rows.forEach((r, i) => {
        const tr = document.createElement('tr');
        const color = r.color || seriesColor(i);
        tr.innerHTML =
            `<td>${escapeHtml(r.label)}${r.note
                ? `<div class="source-note">${escapeHtml(r.note)}</div>` : ''}</td>` +
            `<td><span class="minibar" title="${fmt(r.value)}">` +
            `<i style="width:${(r.value / max * 100).toFixed(1)}%;background:${color}"></i>` +
            `</span></td>` +
            `<td class="num">${fmt(r.value)}</td>`;
        tb.appendChild(tr);
    });
    tbl.appendChild(tb);
    const wrap = document.createElement('div');
    wrap.className = 'table-wrap';
    wrap.appendChild(tbl);
    container.appendChild(wrap);
}

export function escapeHtml(s) {
    return String(s === null || s === undefined ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

/* Tabellenansicht zu einem Chart - die Entlastung für Farbkontrast-Warnungen
 * und der Zugang für Screenreader. */
export function seriesTable(series, fmt = fmtNum) {
    const times = new Set();
    for (const s of series) for (const [t] of s.points || []) times.add(t);
    const sorted = [...times].sort((a, b) => a - b).slice(-200);
    const idx = series.map((s) => new Map((s.points || []).map((p) => [p[0], p[1]])));
    const head = `<thead><tr><th>Zeit</th>${series
        .map((s) => `<th class="num">${escapeHtml(s.name)}</th>`).join('')}</tr></thead>`;
    const body = sorted.map((t) =>
        `<tr><td>${new Date(t).toLocaleString('de-DE')}</td>${
            idx.map((m) => `<td class="num">${fmt(m.get(t) ?? null)}</td>`).join('')
        }</tr>`).join('');
    return `<div class="table-wrap scroll-y"><table class="data">${head}<tbody>${
        body}</tbody></table></div>`;
}


/* Sparkline: Verlauf auf kleinstem Raum, ohne Achsen und Beschriftung.
 *
 * Gedacht als Begleiter einer Zahl, nicht als eigenständiges Diagramm - sie
 * beantwortet nur "steigt das oder fällt es", und das soll man im Vorbeigehen
 * sehen. Für alles Genauere gibt es die großen Charts.
 */
export function sparkline(points, opts = {}) {
    const w = opts.width || 120;
    const h = opts.height || 28;
    const color = opts.color || 'var(--series-1)';
    const vals = (points || []).filter(
        (p) => p[1] !== null && p[1] !== undefined && !Number.isNaN(p[1]));
    if (vals.length < 2) {
        return `<svg class="spark" width="${w}" height="${h}" aria-hidden="true"></svg>`;
    }

    let min = opts.yMin, max = opts.yMax;
    if (min === undefined) min = Math.min(...vals.map((p) => p[1]));
    if (max === undefined) max = Math.max(...vals.map((p) => p[1]));
    if (min === max) { min -= 1; max += 1; }

    const t0 = vals[0][0], t1 = vals[vals.length - 1][0];
    const span = t1 - t0 || 1;
    const pad = 2.5;   // Platz für die Strichstärke am Rand
    const x = (t) => ((t - t0) / span) * (w - 2 * pad) + pad;
    const y = (v) => h - pad - ((v - min) / (max - min)) * (h - 2 * pad);

    const d = vals.map((p, i) => `${i ? 'L' : 'M'}${x(p[0]).toFixed(1)},`
        + `${y(p[1]).toFixed(1)}`).join('');
    const area = `M${x(t0).toFixed(1)},${h} ` + d.slice(1)
        + `L${x(t1).toFixed(1)},${h}Z`;
    const last = vals[vals.length - 1];

    return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"
        preserveAspectRatio="none" role="img"
        aria-label="${escapeHtml(opts.label || 'Verlauf')}">
        <path d="${area}" fill="${color}" opacity="0.10"/>
        <path d="${d}" fill="none" stroke="${color}" stroke-width="1.6"
              stroke-linejoin="round" stroke-linecap="round"/>
        <circle cx="${x(last[0]).toFixed(1)}" cy="${y(last[1]).toFixed(1)}" r="2.2"
                fill="${color}"/>
      </svg>`;
}

/* Waagerechter Füllstandsbalken mit Schwellenfärbung. */
export function meter(pct, opts = {}) {
    if (pct === null || pct === undefined || Number.isNaN(pct)) {
        return `<span class="meter"><i style="width:0"></i></span>`;
    }
    const v = Math.max(0, Math.min(100, pct));
    const warn = opts.warn ?? 92;
    const crit = opts.crit ?? 98;
    const level = v >= crit ? 'critical' : v >= warn ? 'warning' : '';
    return `<span class="meter" title="${v.toFixed(1)} %">`
        + `<i class="${level}" style="width:${v.toFixed(1)}%"></i></span>`;
}
