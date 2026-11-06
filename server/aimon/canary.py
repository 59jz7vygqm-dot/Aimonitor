"""Kanarienvogel: der einzige Check, der „es funktioniert" beantwortet.

Alles andere hier misst Eigenschaften — Temperatur, belegter Speicher, offener
Port. Keine davon beantwortet die Frage, auf die es ankommt: bekommt ein
Benutzer gerade eine Antwort? Ein Port ist offen, während kein Modell geladen
ist. Ein vLLM meldet saubere Metriken, während der Proxy davor auf einen
falschen Upstream zeigt. Beides sieht im Dashboard gut aus.

Deshalb schickt der Server in festem Takt einen echten, winzigen Request durch
denselben Weg, den ein Benutzer nimmt, und misst, was zurückkommt.

Zwei Dinge sind dabei Absicht:

- **Der Request ist echt.** Keine Health-Route, keine Sonderbehandlung. Was
  hier gemessen wird, ist das, was ein Benutzer erlebt.
- **Die Ergebnisse laufen durch denselben Ingest-Weg wie Agent-Daten.** Der
  Kanarienvogel ist ein Dienst wie jeder andere, mit Knoten, Metriken und
  Verlauf. Kein Sonderpfad, der beim nächsten Umbau vergessen wird.

Ohne `AIMON_CANARY_TARGETS` passiert nichts.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import settings
from .db import pool
from .models import Batch
from .tsdb import writer

log = logging.getLogger("aimon.canary")

# Mehr als das liest der Kanarienvogel nicht - die Antwort soll kurz sein, und
# ein Backend, das pathologisch viel schickt, darf hier keinen Speicher fressen.
MAX_BODY_BYTES = 256 * 1024


@dataclass
class Target:
    name: str
    url: str
    model: str
    api_key: str | None = None
    prompt: str = "Antworte nur mit dem Wort: ok"
    max_tokens: int = 16
    timeout_s: float = 30.0

    @property
    def service_key(self) -> str:
        return f"canary@{self.name}"


@dataclass
class Result:
    ok: bool
    ttft_ms: float | None = None
    total_ms: float | None = None
    completion_tokens: float | None = None
    http_status: int | None = None
    error: str | None = None


def parse_targets(raw: str | None) -> list[Target]:
    """Ziele aus der Umgebungsvariable lesen.

    Eine kaputte Konfiguration wird gemeldet und dann ignoriert. Der Server
    darf daran nicht scheitern: ein Tippfehler im Kanarienvogel soll nicht das
    ganze Monitoring abschalten.
    """
    if not raw or not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except ValueError as exc:
        log.error("AIMON_CANARY_TARGETS ist kein gültiges JSON: %s", exc)
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        log.error("AIMON_CANARY_TARGETS muss eine Liste von Objekten sein")
        return []

    out: list[Target] = []
    seen: set[str] = set()
    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            log.error("Kanarienvogel-Ziel %d ist kein Objekt - übersprungen", i)
            continue
        url = str(entry.get("url") or "").strip()
        model = str(entry.get("model") or "").strip()
        name = str(entry.get("name") or "").strip()
        if not url or not model:
            log.error("Kanarienvogel-Ziel %r braucht url und model - übersprungen",
                      name or i)
            continue
        if not name:
            name = f"ziel{i + 1}"
        if name in seen:
            log.error("Kanarienvogel-Ziel %r doppelt vergeben - übersprungen", name)
            continue
        seen.add(name)
        key = entry.get("api_key")
        try:
            out.append(Target(
                name=name,
                url=url,
                model=model,
                api_key=str(key) if key else None,
                prompt=str(entry.get("prompt") or Target.prompt),
                max_tokens=int(entry.get("max_tokens") or Target.max_tokens),
                timeout_s=float(entry.get("timeout_s") or settings.canary_timeout_s),
            ))
        except (TypeError, ValueError) as exc:
            log.error("Kanarienvogel-Ziel %r hat unbrauchbare Werte: %s", name, exc)
    return out


def _probe_blocking(t: Target) -> Result:
    """Einen echten Chat-Request absetzen und mitschreiben, wann was ankommt.

    Bewusst mit urllib und ohne zusätzliche Abhängigkeit: der Server soll für
    seinen eigenen Selbsttest kein Paket brauchen, das ausfallen kann.
    """
    body = json.dumps({
        "model": t.model,
        "messages": [{"role": "user", "content": t.prompt}],
        "max_tokens": t.max_tokens,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }).encode()

    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if t.api_key:
        headers["Authorization"] = f"Bearer {t.api_key}"

    req = urllib.request.Request(t.url, data=body, headers=headers, method="POST")
    started = time.monotonic()
    ttft: float | None = None
    tokens: float | None = None
    saw_content = False
    read = 0

    try:
        with urllib.request.urlopen(req, timeout=t.timeout_s) as resp:
            status = resp.status
            buf = b""
            while read < MAX_BODY_BYTES:
                chunk = resp.read(4096)
                if not chunk:
                    break
                read += len(chunk)
                buf += chunk
                lines = buf.split(b"\n")
                buf = lines.pop()
                for line in lines:
                    line = line.strip()
                    if not line.startswith(b"data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == b"[DONE]":
                        continue
                    try:
                        obj = json.loads(payload)
                    except ValueError:
                        continue
                    if not saw_content and _has_content(obj):
                        saw_content = True
                        ttft = (time.monotonic() - started) * 1000.0
                    usage = obj.get("usage")
                    if isinstance(usage, dict):
                        ct = usage.get("completion_tokens")
                        if isinstance(ct, (int, float)):
                            tokens = float(ct)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(400).decode("utf-8", "replace").strip()
        except Exception:  # pragma: no cover - Fehler beim Fehlerlesen
            pass
        return Result(ok=False, http_status=exc.code,
                      total_ms=(time.monotonic() - started) * 1000.0,
                      error=f"HTTP {exc.code}" + (f": {detail[:300]}" if detail else ""))
    except urllib.error.URLError as exc:
        return Result(ok=False,
                      total_ms=(time.monotonic() - started) * 1000.0,
                      error=f"nicht erreichbar: {exc.reason}")
    except Exception as exc:  # Timeout und alles andere
        return Result(ok=False,
                      total_ms=(time.monotonic() - started) * 1000.0,
                      error=f"{type(exc).__name__}: {exc}")

    total = (time.monotonic() - started) * 1000.0
    if not saw_content:
        # HTTP 200 und trotzdem nichts Brauchbares. Genau der Fall, den ein
        # Port-Check nicht sieht.
        return Result(ok=False, http_status=status, total_ms=total,
                      error="Antwort enthielt keinen Text")
    return Result(ok=True, ttft_ms=ttft, total_ms=total,
                  completion_tokens=tokens, http_status=status)


def _has_content(obj: Any) -> bool:
    try:
        for ch in obj.get("choices") or []:
            delta = ch.get("delta") or {}
            if delta.get("content"):
                return True
            # Nicht-streamende Backends antworten trotz stream=true am Stück.
            msg = ch.get("message") or {}
            if msg.get("content"):
                return True
    except AttributeError:
        return False
    return False


def to_batch(t: Target, r: Result) -> Batch:
    """Ergebnis in genau die Form bringen, die auch ein Agent schickt."""
    samples: list[dict] = [
        {"metric": "canary.ok", "value": 1.0 if r.ok else 0.0,
         "service": t.service_key},
    ]
    if r.total_ms is not None:
        samples.append({"metric": "canary.total_ms", "value": r.total_ms,
                        "service": t.service_key})
    if r.ttft_ms is not None:
        samples.append({"metric": "canary.ttft_ms", "value": r.ttft_ms,
                        "service": t.service_key})
    if r.completion_tokens is not None:
        samples.append({"metric": "canary.completion_tokens",
                        "value": r.completion_tokens, "service": t.service_key})

    events: list[dict] = []
    if not r.ok:
        events.append({
            "kind": "canary.failed",
            "severity": "error",
            "service": t.service_key,
            "title": f"Chat-Request an {t.name} fehlgeschlagen",
            "detail": {
                "ziel": t.name, "url": t.url, "modell": t.model,
                "fehler": r.error, "http_status": r.http_status,
                "timeout_s": t.timeout_s,
            },
        })

    return Batch.model_validate({
        "node": {"name": settings.canary_node},
        "services": [{
            "key": t.service_key, "kind": "canary",
            "name": f"Kanarienvogel {t.name}",
            "labels": {"modell": t.model, "ziel": t.url},
        }],
        "samples": samples,
        "events": events,
    })


async def check_once(t: Target) -> Result:
    r = await asyncio.to_thread(_probe_blocking, t)
    batch = to_batch(t, r)
    async with pool().acquire() as conn:
        async with conn.transaction():
            await writer.write_batch(conn, batch, token_name="canary")
    if r.ok:
        log.debug("Kanarienvogel %s ok (%.0f ms bis zum ersten Token)",
                  t.name, r.ttft_ms or -1)
    else:
        log.warning("Kanarienvogel %s fehlgeschlagen: %s", t.name, r.error)
    return r


async def run_forever() -> None:
    targets = parse_targets(settings.canary_targets)
    if not targets:
        log.info("Kein Kanarienvogel konfiguriert (AIMON_CANARY_TARGETS)")
        return
    log.info("Kanarienvogel aktiv für %s, alle %ds",
             ", ".join(t.name for t in targets), settings.canary_interval_s)
    while True:
        for t in targets:
            try:
                await check_once(t)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Ein Fehler im Selbsttest darf den Selbsttest nicht beenden.
                log.exception("Kanarienvogel %s: unerwarteter Fehler", t.name)
        await asyncio.sleep(settings.canary_interval_s)
