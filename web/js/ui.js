/* Gemeinsame UI-Bausteine. */
import { escapeHtml, fmtNum, fmtDuration } from './chart.js';

export function h(html) {
    const t = document.createElement('template');
    t.innerHTML = html.trim();
    return t.content.firstElementChild;
}

export function card(title, sub, bodyHtml, headRight) {
    return h(`<section class="card">
        <div class="card-head">
          <div><h3>${escapeHtml(title)}</h3>${
              sub ? `<p class="sub">${escapeHtml(sub)}</p>` : ''}</div>
          <div>${headRight || ''}</div>
        </div>
        <div class="card-body">${bodyHtml || ''}</div>
      </section>`);
}

export function tile(label, valueHtml, note, noteClass) {
    return h(`<section class="card tile">
        <div class="label">${escapeHtml(label)}</div>
        <div class="value">${valueHtml}</div>
        ${note ? `<div class="note ${noteClass || ''}">${escapeHtml(note)}</div>` : ''}
      </section>`);
}

/* Statusanzeige: immer Symbol + Text, nie Farbe allein. */
const ICONS = { good: '●', warning: '▲', serious: '◆', critical: '■', muted: '○' };
export function statusHtml(level, text) {
    const lv = ICONS[level] ? level : 'muted';
    return `<span class="status ${lv}"><span class="ico" aria-hidden="true">${
        ICONS[lv]}</span>${escapeHtml(text)}</span>`;
}

export function nodeStatus(st, ageS) {
    if (st === 'online') return statusHtml('good', 'online');
    if (st === 'stale') return statusHtml('warning',
        `stumm seit ${fmtDuration(ageS)}`);
    return statusHtml('critical', `offline seit ${fmtDuration(ageS)}`);
}

export function sevLevel(sev) {
    return { critical: 'critical', error: 'serious', warning: 'warning',
        info: 'muted', debug: 'muted' }[sev] || 'muted';
}

export function sevText(sev) {
    return { critical: 'kritisch', error: 'Fehler', warning: 'Warnung',
        info: 'Info', debug: 'Debug' }[sev] || sev;
}

export function pct(v, digits = 0) {
    return v === null || v === undefined ? '—' : v.toFixed(digits) + ' %';
}

export function labelStr(labels, skip = []) {
    const e = Object.entries(labels || {}).filter(([k]) => !skip.includes(k));
    if (!e.length) return '';
    return e.map(([k, v]) => `${k}=${v}`).join(' ');
}

let toastTimer = null;
export function toast(msg) {
    let t = document.querySelector('.toast');
    if (!t) {
        t = h('<div class="toast" role="status" aria-live="polite"></div>');
        document.body.appendChild(t);
    }
    t.textContent = msg;
    t.classList.add('on');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove('on'), 2200);
}

export function spinner(text = 'Lade …') {
    return h(`<div class="spinner">${escapeHtml(text)}</div>`);
}

export function errorBanner(err) {
    let msg = String(err && err.message || err);
    if (err && err.status === 401) {
        msg = 'Lese-Token erforderlich. Oben rechts eintragen.';
    } else if (err && err.body) {
        const d = err.body.detail ?? err.body;
        msg = typeof d === 'string' ? d : JSON.stringify(d);
    }
    return h(`<div class="banner critical">${
        statusHtml('critical', 'Fehler')} <span>${escapeHtml(msg)}</span></div>`);
}

/* Chart + umschaltbare Tabellenansicht. Die Tabelle ist nicht optional:
 * sie ist der Zugang für Screenreader und die Entlastung bei knappem
 * Farbkontrast. */
export function chartCard(title, sub, renderChart, renderTable, headRight) {
    const c = card(title, sub, '', `
        <button class="ctl" data-view="chart" aria-pressed="true">Chart</button>
        <button class="ctl" data-view="table" aria-pressed="false">Tabelle</button>
        ${headRight || ''}`);
    const body = c.querySelector('.card-body');
    const chartBox = h('<div></div>');
    const tableBox = h('<div hidden></div>');
    body.appendChild(chartBox);
    body.appendChild(tableBox);

    renderChart(chartBox);
    let tableDone = false;

    c.querySelectorAll('[data-view]').forEach((b) => {
        b.addEventListener('click', () => {
            const want = b.dataset.view;
            c.querySelectorAll('[data-view]').forEach((o) =>
                o.setAttribute('aria-pressed', o.dataset.view === want ? 'true' : 'false'));
            if (want === 'table' && !tableDone) {
                tableBox.innerHTML = renderTable();
                tableDone = true;
            }
            chartBox.hidden = want !== 'chart';
            tableBox.hidden = want !== 'table';
        });
    });
    return c;
}

/* opts.scroll begrenzt die Höhe und scrollt innerhalb der Tabelle.
 * Ohne das wird eine Seite mit 500 Logzeilen zehntausende Pixel lang und
 * alles darunter ist praktisch unerreichbar. */
export function table(cols, rows, opts = {}) {
    const head = cols.map((c) =>
        `<th class="${c.num ? 'num' : ''}">${escapeHtml(c.label)}</th>`).join('');
    const body = rows.map((r) =>
        `<tr>${cols.map((c) => `<td class="${c.num ? 'num' : ''}">${
            c.html ? c.html(r) : escapeHtml(r[c.key] ?? '—')}</td>`).join('')}</tr>`).join('');
    const count = opts.scroll && rows.length
        ? `<div class="source-note">${rows.length} Zeilen – Tabelle scrollt.</div>` : '';
    return `<div class="table-wrap${opts.scroll ? ' scroll-y' : ''}">` +
        `<table class="data"><thead><tr>${head}</tr></thead>` +
        `<tbody>${body || `<tr><td colspan="${cols.length}" class="empty">` +
        `Keine Einträge.</td></tr>`}</tbody></table></div>${count}`;
}

export function minibar(value, max, level) {
    const w = max > 0 ? Math.min(100, (value / max) * 100) : 0;
    return `<span class="minibar"><i class="${level || ''}" style="width:${
        w.toFixed(1)}%"></i></span>`;
}

export { fmtNum, fmtDuration, escapeHtml };
