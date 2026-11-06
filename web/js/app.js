/* Shell: Tabs, Zeitraum, Auto-Refresh, Theme, Token. */
import { api, getToken, setToken } from './api.js';
import { h, toast, errorBanner, spinner } from './ui.js';
import * as overview from './views/overview.js';
import * as gpus from './views/gpus.js';
import * as inference from './views/inference.js';
import * as topology from './views/topology.js';
import * as openterminal from './views/openterminal.js';
import * as privacy from './views/privacy.js';
import * as diagnose from './views/diagnose.js';
import * as explorer from './views/explorer.js';

const VIEWS = [
    ['overview', 'Übersicht', overview],
    ['gpus', 'GPUs & Hosts', gpus],
    ['inference', 'Inference', inference],
    ['topology', 'Topologie', topology],
    ['openterminal', 'OpenTerminal', openterminal],
    ['privacy', 'SearXNG-Proxy', privacy],
    ['diagnose', 'Diagnose', diagnose],
    ['explorer', 'Explorer', explorer],
];

const RANGES = ['15m', '1h', '6h', '24h', '7d', '30d'];
const REFRESH = [['0', 'aus'], ['10', '10 s'], ['30', '30 s'], ['60', '1 min'],
    ['300', '5 min']];

const state = {
    view: 'overview',
    range: localStorage.getItem('aimon.range') || '1h',
    refresh: parseInt(localStorage.getItem('aimon.refresh') || '30', 10),
    reload: () => {},
};

let timer = null;
// Jeder Render bekommt eine Nummer. Trifft ein älteres Ergebnis nach einem
// neueren ein, wird es verworfen - sonst zeigt die Seite den Inhalt des alten
// Tabs unter der Beschriftung des neuen.
let renderSeq = 0;

function parseHash() {
    const m = /^#\/([a-z]+)/.exec(location.hash);
    const v = m && VIEWS.find(([id]) => id === m[1]);
    return v ? v[0] : 'overview';
}

async function renderView() {
    const seq = ++renderSeq;
    const view = state.view;
    const main = document.querySelector('main');
    const entry = VIEWS.find(([id]) => id === view) || VIEWS[0];

    document.querySelectorAll('nav.tabs button').forEach((b) => {
        if (b.dataset.view === view) b.setAttribute('aria-current', 'page');
        else b.removeAttribute('aria-current');
    });
    document.title = `${entry[1]} – aimonitor`;

    // Scrollposition halten, sonst springt die Seite bei jedem Auto-Refresh
    // nach oben. Beim Tabwechsel wird dagegen nach oben gescrollt.
    const sameView = main.dataset.view === view;
    const scroll = sameView ? window.scrollY : 0;

    const box = document.createElement('div');
    try {
        await entry[2].render(box, state);
    } catch (e) {
        box.textContent = '';
        box.appendChild(errorBanner(e));
        console.error(e);
    }
    if (seq !== renderSeq) return;   // überholt: Ergebnis verwerfen
    main.textContent = '';
    main.dataset.view = view;
    main.appendChild(box);
    window.scrollTo(0, scroll);
}

function scheduleRefresh() {
    clearInterval(timer);
    if (state.refresh > 0) {
        timer = setInterval(() => {
            if (document.hidden) return;   // im Hintergrund nicht pollen
            renderView();
        }, state.refresh * 1000);
    }
}

function applyTheme(mode) {
    if (mode === 'auto') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', mode);
    localStorage.setItem('aimon.theme', mode);
}

async function boot() {
    const root = document.getElementById('app');

    let health = null;
    try { health = await api.health(); } catch { /* unten gemeldet */ }

    const bar = h(`<header class="topbar">
        <div class="brand">aimonitor<span class="ver"
            title="${health ? `Build ${health.build || 'unbekannt'}` : ''}">${
    health ? health.version : '?'}${health?.build && health.build !== 'unbekannt'
    ? ` · ${health.build}` : ''}</span></div>
        <nav class="tabs" aria-label="Bereiche">
          ${VIEWS.map(([id, label]) =>
              `<button type="button" data-view="${id}">${label}</button>`).join('')}
        </nav>
        <div class="topbar-right">
          <label class="inline">Zeitraum
            <select id="range">${RANGES.map((r) =>
                `<option value="${r}"${r === state.range ? ' selected' : ''}>${
                    r}</option>`).join('')}</select></label>
          <label class="inline">Aktualisieren
            <select id="refresh">${REFRESH.map(([v, l]) =>
                `<option value="${v}"${parseInt(v, 10) === state.refresh
                    ? ' selected' : ''}>${l}</option>`).join('')}</select></label>
          <button class="ctl" id="reload" title="Jetzt neu laden">↻</button>
          <select id="theme" title="Darstellung">
            <option value="auto">Auto</option>
            <option value="light">Hell</option>
            <option value="dark">Dunkel</option>
          </select>
          <button class="ctl" id="tokenbtn" title="Lese-Token setzen">Token</button>
        </div>
      </header>`);
    root.appendChild(bar);
    root.appendChild(document.createElement('main'));

    if (!health) {
        document.querySelector('main').appendChild(h(`<div class="banner critical">
          <span class="status critical"><span class="ico">■</span>Server nicht
          erreichbar</span><span>Läuft aimon-server? Antwortet
          <code>/api/v1/health</code>?</span></div>`));
    } else if (health.status !== 'ok') {
        document.querySelector('main').appendChild(h(`<div class="banner warning">
          <span class="status warning"><span class="ico">▲</span>Server
          eingeschränkt</span><span>${health.db}</span></div>`));
    }

    bar.querySelectorAll('nav.tabs button').forEach((b) => {
        b.addEventListener('click', () => { location.hash = '#/' + b.dataset.view; });
    });
    bar.querySelector('#range').addEventListener('change', (e) => {
        state.range = e.target.value;
        localStorage.setItem('aimon.range', state.range);
        renderView();
    });
    bar.querySelector('#refresh').addEventListener('change', (e) => {
        state.refresh = parseInt(e.target.value, 10);
        localStorage.setItem('aimon.refresh', String(state.refresh));
        scheduleRefresh();
    });
    bar.querySelector('#reload').addEventListener('click', renderView);
    const themeSel = bar.querySelector('#theme');
    themeSel.value = localStorage.getItem('aimon.theme') || 'auto';
    themeSel.addEventListener('change', (e) => {
        applyTheme(e.target.value);
        renderView();   // Chartfarben kommen aus CSS-Variablen, Neuzeichnen genügt
    });
    bar.querySelector('#tokenbtn').addEventListener('click', () => {
        const cur = getToken();
        const v = prompt(
            'Lese-Token (nur nötig, wenn AIMON_READ_TOKEN am Server gesetzt ist).\n' +
            'Leer lassen zum Entfernen.', cur);
        if (v === null) return;
        setToken(v.trim());
        toast(v.trim() ? 'Token gespeichert.' : 'Token entfernt.');
        renderView();
    });

    state.reload = renderView;
    window.addEventListener('hashchange', () => {
        state.view = parseHash();
        renderView();
    });
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) renderView();
    });
    window.addEventListener('resize', debounce(renderView, 400));

    state.view = parseHash();
    await renderView();
    scheduleRefresh();
}

function debounce(fn, ms) {
    let t;
    return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

applyTheme(localStorage.getItem('aimon.theme') || 'auto');
boot();
