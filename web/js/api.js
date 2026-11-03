/* API-Zugriff. Ein Lese-Token wird, wenn gesetzt, im localStorage gehalten -
 * das Dashboard gehört ohnehin hinter einen Reverse-Proxy. */

const TOKEN_KEY = 'aimon.token';

export function getToken() {
    try { return localStorage.getItem(TOKEN_KEY) || ''; } catch { return ''; }
}

export function setToken(t) {
    try {
        if (t) localStorage.setItem(TOKEN_KEY, t);
        else localStorage.removeItem(TOKEN_KEY);
    } catch { /* Private-Mode: dann eben nur für diese Seite */ }
}

export class ApiError extends Error {
    constructor(status, body) {
        super(`HTTP ${status}`);
        this.status = status;
        this.body = body;
    }
}

async function req(path, init = {}) {
    const headers = { ...(init.headers || {}) };
    const t = getToken();
    if (t) headers.Authorization = 'Bearer ' + t;
    const r = await fetch('/api/v1' + path, { ...init, headers });
    const text = await r.text();
    let body = null;
    try { body = text ? JSON.parse(text) : null; } catch { body = text; }
    if (!r.ok) throw new ApiError(r.status, body);
    return body;
}

function qs(params) {
    const p = new URLSearchParams();
    for (const [k, v] of Object.entries(params || {})) {
        if (v === undefined || v === null || v === '') continue;
        p.set(k, typeof v === 'object' ? JSON.stringify(v) : String(v));
    }
    const s = p.toString();
    return s ? '?' + s : '';
}

export const api = {
    health: () => req('/health'),
    overview: () => req('/overview'),
    stats: () => req('/stats'),
    nodes: () => req('/nodes'),
    services: (p) => req('/services' + qs(p)),
    topology: (p) => req('/topology' + qs(p)),
    matrix: (p) => req('/connectivity-matrix' + qs(p)),
    incidents: (p) => req('/incidents' + qs(p)),
    evaluate: () => req('/incidents/evaluate', { method: 'POST' }),
    events: (p) => req('/events' + qs(p)),
    latency: (p) => req('/latency' + qs(p)),
    promptStats: (p) => req('/prompt-stats' + qs(p)),
    latest: (p) => req('/latest' + qs(p)),
    query: (p) => req('/query' + qs(p)),
    queryMulti: (body) => req('/query/multi', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    }),
    catalog: () => req('/metrics-catalog'),
    series: (p) => req('/series' + qs(p)),
    privacyBlocked: (p) => req('/privacy/blocked' + qs(p)),
    openterminal: (p) => req('/openterminal/summary' + qs(p)),
    diagPrompt: (body) => req('/diagnostics/prompt', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    }).catch(async (e) => { throw e; }),
};

/* Bericht holen. Die Platzhalter-Legende kommt getrennt vom Prompt zurück:
 * stünde sie im Text, würde ein vollständiges Kopieren die Anonymisierung
 * wieder aufheben. */
export async function fetchPrompt(body) {
    const headers = { 'Content-Type': 'application/json' };
    const t = getToken();
    if (t) headers.Authorization = 'Bearer ' + t;
    const p = new URLSearchParams();
    for (const [k, v] of Object.entries(body)) {
        if (v === null || v === undefined) continue;
        p.set(k, String(v));
    }
    const r = await fetch('/api/v1/diagnostics/prompt?' + p.toString(), { headers });
    const text = await r.text();
    let data;
    try { data = JSON.parse(text); } catch { data = null; }
    if (!r.ok) throw new ApiError(r.status, data ?? text);
    return data;
}
