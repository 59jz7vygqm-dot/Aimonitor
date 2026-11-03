import { api, fetchPrompt } from '../api.js';
import { escapeHtml, fmtNum } from '../chart.js';
import { card, spinner, errorBanner, h, table, statusHtml, sevLevel, sevText,
    fmtDuration, toast } from '../ui.js';

/* Incidents und der kopierbare Fehlerbericht.
 *
 * Der Bericht ist der eigentliche Zweck dieser Seite: er beschreibt den
 * ganzen Aufbau plus das aktuelle Fehlerbild in einem Textblock, den man
 * einem anderen Modell oder einem anderen Menschen vorlegen kann.
 */

export async function render(root, state) {
    root.appendChild(spinner());
    let inc, ev;
    try {
        [inc, ev] = await Promise.all([
            api.incidents({ open_only: false, limit: 300 }),
            api.events({ since: '-' + state.range, limit: 200 }),
        ]);
    } catch (e) {
        root.textContent = '';
        root.appendChild(errorBanner(e));
        return;
    }
    root.textContent = '';

    const open = inc.filter((i) => !i.ended_at);
    const closed = inc.filter((i) => i.ended_at);

    // ---------------------------------------------------- Prompt-Generator
    const gen = h(`<section class="card">
      <div class="card-head">
        <div>
          <h3>Fehlerbericht erzeugen</h3>
          <p class="sub">Beschreibt Aufbau, Messwerte und Fehlerbild in einem
          Textblock. Zum Kopieren und einem anderen Modell oder Menschen
          vorlegen.</p>
        </div>
      </div>
      <div class="card-body">
        <div class="filterbar">
          <label class="inline">Zeitfenster
            <select data-g="window">
              <option value="900">15 Minuten</option>
              <option value="3600" selected>1 Stunde</option>
              <option value="21600">6 Stunden</option>
              <option value="86400">24 Stunden</option>
            </select></label>
          <label class="inline">Umfang
            <select data-g="scope">
              <option value="">alles, was offen ist</option>
              ${open.map((i) => `<option value="incident:${i.id}">nur: ${
                  escapeHtml(i.title.slice(0, 70))}</option>`).join('')}
            </select></label>
          <label class="inline">
            <input type="checkbox" data-g="redact">
            Hostnamen und IPs ersetzen</label>
          <label class="inline">
            <input type="checkbox" data-g="evidence" checked>
            Zeitreihen als Beleg anhängen</label>
        </div>
        <label class="inline" style="display:block;margin-bottom:6px">
          Was beobachtest du? (kommt wörtlich in den Bericht)</label>
        <textarea data-g="note" rows="3" placeholder="z. B. OpenWebUI antwortet seit
einer Stunde sehr langsam, besonders beim großen Modell. Bilder generieren geht
gar nicht mehr."></textarea>
        <div class="filterbar" style="margin:10px 0 0">
          <button class="ctl primary" data-g="build">Bericht erzeugen</button>
          <button class="ctl" data-g="copy" disabled>In die Zwischenablage</button>
          <button class="ctl" data-g="download" disabled>Als .md speichern</button>
          <span class="source-note" data-g="info"></span>
        </div>
        <div data-g="out" style="margin-top:12px"></div>
      </div>
    </section>`);
    root.appendChild(gen);

    const q = (s) => gen.querySelector(`[data-g="${s}"]`);
    let current = '';

    async function build() {
        const btn = q('build');
        btn.disabled = true;
        btn.textContent = 'Erzeuge …';
        q('out').textContent = '';
        q('out').appendChild(spinner('Sammle Messwerte …'));
        const scopeRaw = q('scope').value;
        const body = {
            window_s: parseInt(q('window').value, 10),
            redact: q('redact').checked,
            include_evidence: q('evidence').checked,
            note: q('note').value.trim() || null,
        };
        if (scopeRaw.startsWith('incident:')) {
            body.scope = 'incident';
            body.subject = scopeRaw.slice('incident:'.length);
        }
        try {
            const res = await fetchPrompt(body);
            current = res.prompt;
            const legend = res.legend || [];
            q('out').innerHTML =
                `<pre class="prompt">${escapeHtml(current)}</pre>` +
                (legend.length
                    ? `<details style="margin-top:10px">
                         <summary style="cursor:pointer;font-size:12.5px">
                           Platzhalter-Legende (${legend.length}) – nur für dich,
                           nicht Teil des kopierten Berichts</summary>
                         <div class="table-wrap scroll-y" style="margin-top:8px">
                           <table class="data"><thead><tr><th>Platzhalter</th>
                           <th>steht für</th></tr></thead><tbody>${legend.map((l) =>
                             `<tr><td><code>${escapeHtml(l.alias)}</code></td>
                              <td><code>${escapeHtml(l.original)}</code></td></tr>`
                           ).join('')}</tbody></table></div></details>`
                    : '');
            q('copy').disabled = false;
            q('download').disabled = false;
            q('info').textContent =
                `${current.length.toLocaleString('de-DE')} Zeichen` +
                (res.truncated ? ' · gekürzt' : '') +
                (legend.length ? ` · ${legend.length} Platzhalter, Zuordnung bleibt hier`
                    : '');
        } catch (e) {
            q('out').textContent = '';
            q('out').appendChild(errorBanner(e));
        } finally {
            btn.disabled = false;
            btn.textContent = 'Bericht erzeugen';
        }
    }

    q('build').addEventListener('click', build);
    q('copy').addEventListener('click', async () => {
        try {
            await navigator.clipboard.writeText(current);
            toast('Bericht kopiert.');
        } catch {
            // Clipboard-API braucht HTTPS oder localhost; Fallback über Auswahl.
            const pre = q('out').querySelector('pre');
            if (pre) {
                const r = document.createRange();
                r.selectNodeContents(pre);
                const sel = window.getSelection();
                sel.removeAllRanges();
                sel.addRange(r);
                toast('Zwischenablage gesperrt – Text ist markiert, Strg+C drücken.');
            }
        }
    });
    q('download').addEventListener('click', () => {
        const blob = new Blob([current], { type: 'text/markdown;charset=utf-8' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = `aimonitor-bericht-${new Date().toISOString()
            .slice(0, 19).replace(/[:T]/g, '-')}.md`;
        a.click();
        URL.revokeObjectURL(a.href);
    });

    // ------------------------------------------------------------ Incidents
    const incCard = card('Offene Incidents',
        `${open.length} offen. Jeder Eintrag zeigt den Kontext, mit dem die Regel ` +
        `ausgelöst hat.`, '', `<button class="ctl" data-act="eval">Jetzt neu prüfen</button>`);
    const body = incCard.querySelector('.card-body');
    if (!open.length) {
        body.innerHTML = `<div class="banner good">${statusHtml('good',
            'Keine offenen Incidents')}<span>Alle Regeln ruhig. Wenn du trotzdem ein
            Problem siehst, liegt es außerhalb dessen, was gemessen wird – genau
            dafür ist der Bericht oben da.</span></div>`;
    } else {
        body.innerHTML = open.map((i) => `
          <div class="incident">
            <div>${statusHtml(sevLevel(i.severity), sevText(i.severity))}</div>
            <div class="body">
              <div class="title">${escapeHtml(i.title)}</div>
              <div class="meta"><code>${escapeHtml(i.rule)}</code> ·
                ${escapeHtml(i.subject)} (${escapeHtml(i.subject_kind)}) ·
                seit ${fmtDuration(i.age_s)}</div>
              <div class="ctx">${escapeHtml(fmtContext(i.context))}</div>
            </div>
          </div>`).join('');
    }
    incCard.querySelector('[data-act="eval"]').addEventListener('click', async (e) => {
        e.target.disabled = true;
        e.target.textContent = 'Prüfe …';
        try {
            const r = await api.evaluate();
            toast(`${r.findings} Findings: ${r.opened} neu, ${r.closed} geschlossen.`);
            state.reload();
        } catch (err) {
            toast('Fehlgeschlagen: ' + (err.message || err));
            e.target.disabled = false;
            e.target.textContent = 'Jetzt neu prüfen';
        }
    });
    root.appendChild(incCard);

    if (closed.length) {
        root.appendChild(card('Zuletzt von selbst verschwunden',
            'Was kommt und geht, deutet auf Last- oder Scheduling-Effekte, nicht auf Defekte.',
            table([
                { label: 'Schwere', html: (i) =>
                    statusHtml(sevLevel(i.severity), sevText(i.severity)) },
                { label: 'Regel', html: (i) => `<code>${escapeHtml(i.rule)}</code>` },
                { label: 'Betroffen', html: (i) => escapeHtml(i.subject) },
                { label: 'Beschreibung', html: (i) => escapeHtml(i.title) },
                { label: 'Dauer', num: true, html: (i) => fmtDuration(
                    (new Date(i.ended_at) - new Date(i.started_at)) / 1000) },
                { label: 'Ende', html: (i) => new Date(i.ended_at)
                    .toLocaleString('de-DE') },
            ], closed.slice(0, 40), { scroll: true })));
    }

    // ------------------------------------------------------------- Ereignisse
    const evFilter = h(`<div class="filterbar">
        <label class="inline">Schwere
          <select data-e="severity">
            <option value="">alle</option>
            <option value="critical,error">nur Fehler und kritisch</option>
            <option value="warning">nur Warnungen</option>
            <option value="info,debug">nur Info</option>
          </select></label>
        <label class="inline">Art
          <input type="text" data-e="kind" placeholder="z. B. privacy. oder scale."
            style="min-width:180px"></label>
        <input type="search" data-e="q" placeholder="in Titel und Details suchen"
          style="min-width:220px">
      </div>`);
    const evCard = card('Ereignisse',
        `${ev.total} im Zeitraum. Diskrete Vorfälle, keine Logzeilen.`, '');
    evCard.querySelector('.card-body').appendChild(evFilter);
    const evBox = h('<div></div>');
    evCard.querySelector('.card-body').appendChild(evBox);
    root.appendChild(evCard);

    function drawEvents(rows) {
        evBox.innerHTML = table([
            { label: 'Zeit', html: (e) => new Date(e.ts).toLocaleString('de-DE') },
            { label: 'Schwere', html: (e) =>
                statusHtml(sevLevel(e.severity), sevText(e.severity)) },
            { label: 'Art', html: (e) => `<code>${escapeHtml(e.kind)}</code>` },
            { label: 'Dienst', html: (e) => `<code>${escapeHtml(e.service || '—')}</code>` },
            { label: 'Beschreibung', html: (e) => escapeHtml(e.title) +
                (e.count > 1 ? ` <span class="pill">×${e.count}</span>` : '') },
            { label: 'Details', html: (e) => `<code>${escapeHtml(
                fmtContext(e.detail).slice(0, 200))}</code>` },
        ], rows, { scroll: true });
    }
    drawEvents(ev.events);

    async function reloadEvents() {
        const severity = evFilter.querySelector('[data-e="severity"]').value;
        const kind = evFilter.querySelector('[data-e="kind"]').value.trim();
        const search = evFilter.querySelector('[data-e="q"]').value.trim();
        try {
            const r = await api.events({ since: '-' + state.range, limit: 300,
                severity, kind, search });
            drawEvents(r.events);
        } catch (e) {
            toast('Filtern fehlgeschlagen: ' + (e.message || e));
        }
    }
    evFilter.querySelectorAll('[data-e]').forEach((c) => {
        c.addEventListener(c.tagName === 'INPUT' ? 'change' : 'change', reloadEvents);
    });
}

function fmtContext(ctx) {
    if (!ctx || typeof ctx !== 'object') return '';
    const parts = [];
    for (const [k, v] of Object.entries(ctx)) {
        if (v === null || v === undefined) continue;
        if (Array.isArray(v)) parts.push(`${k}: ${v.join(' | ')}`);
        else if (typeof v === 'object') parts.push(`${k}: ${JSON.stringify(v)}`);
        else if (typeof v === 'number') parts.push(`${k}: ${fmtNum(v)}`);
        else parts.push(`${k}: ${v}`);
    }
    return parts.join('\n');
}
