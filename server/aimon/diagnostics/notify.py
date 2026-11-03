"""Benachrichtigungen bei kritischen Incidents.

Leitgedanke: eine Benachrichtigung, die zu oft kommt, wird weggefiltert — und
dann fehlt sie genau an dem Tag, an dem sie wichtig gewesen wäre. Deshalb ist
hier alles auf Zurückhaltung ausgelegt:

- Nur ab einer Mindestschwere (Default `critical`).
- Erst, wenn der Incident eine Weile offen ist (Default 5 Minuten). Was von
  selbst wieder verschwindet, meldet sich nie.
- Genau einmal pro Incident, erzwungen über einen eindeutigen Index in der
  Datenbank — nicht über Zustand im Speicher, der einen Neustart nicht übersteht.
- Eine Obergrenze pro Stunde über alle Incidents. Fällt ein Switch aus, sind
  sofort zwanzig Dinge kritisch; zwanzig Mails helfen niemandem.
- Eine Entwarnung, wenn der Vorfall vorbei ist.

Alles ist standardmäßig aus. Ohne konfigurierten Kanal passiert nichts.
"""
from __future__ import annotations

import asyncio
import json
import logging
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage
from typing import Any

from ..config import settings
from ..db import pool

log = logging.getLogger("aimon.notify")

SEV_RANK = {"debug": 0, "info": 1, "warning": 2, "error": 3, "critical": 4}

_stats = {"sent": 0, "failed": 0, "suppressed_rate": 0, "suppressed_young": 0}


def stats() -> dict:
    return dict(_stats)


def channels() -> list[str]:
    out = []
    if settings.alert_webhook_url:
        out.append("webhook")
    if settings.alert_smtp_host and settings.alert_mail_to:
        out.append("email")
    return out


def enabled() -> bool:
    return bool(channels())


def _min_rank() -> int:
    return SEV_RANK.get(settings.alert_min_severity, 4)


async def run_once() -> dict:
    """Fällige Benachrichtigungen verschicken. Wird vom Regeldurchlauf gerufen."""
    chans = channels()
    if not chans:
        return {"sent": 0, "channels": []}

    async with pool().acquire() as conn:
        # Offene Incidents, die lange genug bestehen und schwer genug sind,
        # und für die noch nichts verschickt wurde.
        fired = await conn.fetch(
            """
            SELECT i.*, extract(epoch FROM (now() - i.started_at))::float8 AS age_s
            FROM incidents i
            WHERE i.ended_at IS NULL
              AND i.acknowledged = false
              AND i.started_at < now() - ($1::int * INTERVAL '1 second')
            ORDER BY i.started_at
            LIMIT 50
            """,
            settings.alert_min_duration_s,
        )
        resolved = []
        if settings.alert_send_resolved:
            resolved = await conn.fetch(
                """
                SELECT i.*, extract(epoch FROM (i.ended_at - i.started_at))::float8
                           AS duration_s
                FROM incidents i
                WHERE i.ended_at IS NOT NULL
                  AND i.ended_at > now() - INTERVAL '1 hour'
                  AND EXISTS (SELECT 1 FROM alert_log a
                              WHERE a.incident_id = i.id AND a.kind = 'fired' AND a.ok)
                  AND NOT EXISTS (SELECT 1 FROM alert_log a
                              WHERE a.incident_id = i.id AND a.kind = 'resolved' AND a.ok)
                ORDER BY i.ended_at
                LIMIT 50
                """
            )

    sent = 0
    for row in fired:
        if SEV_RANK.get(row["severity"], 0) < _min_rank():
            continue
        sent += await _notify(row, "fired", chans)
    for row in resolved:
        # Entwarnungen gehen immer raus, wenn der Alarm rausging - auch wenn
        # die Schwere inzwischen herabgestuft wurde.
        sent += await _notify(row, "resolved", chans)

    return {"sent": sent, "channels": chans}


async def _rate_ok(conn, own_id: int) -> bool:
    """Prüft die Stundengrenze.

    `own_id` ist die soeben reservierte Zeile; sie darf nicht gegen ihr
    eigenes Limit zählen, sonst wäre bei max=3 schon die dritte
    Benachrichtigung unterdrückt.
    """
    if settings.alert_max_per_hour <= 0:
        return True
    n = await conn.fetchval(
        "SELECT count(*) FROM alert_log "
        "WHERE ok AND id <> $1 AND sent_at > now() - INTERVAL '1 hour'",
        own_id,
    )
    return (n or 0) < settings.alert_max_per_hour


async def _notify(row, kind: str, chans: list[str]) -> int:
    sent = 0
    for channel in chans:
        async with pool().acquire() as conn:
            # Platzhalter zuerst schreiben: scheitert das wegen des
            # eindeutigen Index, war schon jemand da. So kann auch bei
            # gleichzeitigen Durchläufen nichts doppelt rausgehen.
            claimed = await conn.fetchval(
                """
                INSERT INTO alert_log (incident_id, kind, channel, ok, subject, rule)
                VALUES ($1,$2,$3,true,$4,$5)
                ON CONFLICT DO NOTHING RETURNING id
                """,
                row["id"], kind, channel, row["subject"], row["rule"],
            )
            if claimed is None:
                continue
            if not await _rate_ok(conn, claimed):
                await conn.execute(
                    "UPDATE alert_log SET ok = false, error = $2 WHERE id = $1",
                    claimed,
                    f"unterdrückt: mehr als {settings.alert_max_per_hour} "
                    f"Benachrichtigungen in der letzten Stunde",
                )
                _stats["suppressed_rate"] += 1
                log.warning("Benachrichtigung unterdrückt (Obergrenze erreicht): %s",
                            row["title"])
                continue

        try:
            if channel == "webhook":
                await asyncio.to_thread(_send_webhook, row, kind)
            else:
                await asyncio.to_thread(_send_mail, row, kind)
            _stats["sent"] += 1
            sent += 1
            log.info("Benachrichtigung über %s: [%s] %s", channel, kind, row["title"])
        except Exception as exc:  # noqa: BLE001
            _stats["failed"] += 1
            log.warning("Benachrichtigung über %s fehlgeschlagen: %s", channel, exc)
            async with pool().acquire() as conn:
                # ok=false nimmt die Zeile aus dem eindeutigen Index: beim
                # nächsten Durchlauf wird es erneut versucht.
                await conn.execute(
                    "UPDATE alert_log SET ok = false, error = $2 WHERE id = $1",
                    claimed, str(exc)[:500],
                )
    return sent


def _payload(row, kind: str) -> dict[str, Any]:
    ctx = dict(row["context"] or {})
    return {
        "kind": kind,
        "incident_id": row["id"],
        "rule": row["rule"],
        "severity": row["severity"],
        "subject": row["subject"],
        "subject_kind": row["subject_kind"],
        "title": row["title"],
        "started_at": row["started_at"].isoformat(),
        "ended_at": row["ended_at"].isoformat() if row["ended_at"] else None,
        "context": ctx,
        "url": f"{settings.public_url}/#/diagnose" if settings.public_url else None,
        "source": "aimonitor",
    }


def _subject_line(row, kind: str) -> str:
    if kind == "resolved":
        return f"[aimonitor] behoben: {row['title']}"
    sev = {"critical": "KRITISCH", "error": "FEHLER",
           "warning": "WARNUNG"}.get(row["severity"], row["severity"].upper())
    return f"[aimonitor] {sev}: {row['title']}"


def _body_text(row, kind: str) -> str:
    L = [_subject_line(row, kind), ""]
    if kind == "resolved":
        dur = row.get("duration_s")
        L.append("Der Vorfall ist vorbei"
                 + (f", er bestand {int(dur // 60)} min {int(dur % 60)} s."
                    if dur else "."))
    else:
        age = row.get("age_s")
        L.append(f"Besteht seit {int(age // 60)} min." if age else "")
    L += [
        "",
        f"Regel:      {row['rule']}",
        f"Betroffen:  {row['subject']} ({row['subject_kind']})",
        f"Schwere:    {row['severity']}",
        f"Beginn:     {row['started_at'].isoformat(timespec='seconds')}",
    ]
    ctx = row["context"] or {}
    if ctx:
        L += ["", "Messwerte und Einordnung:"]
        for k, v in ctx.items():
            if isinstance(v, list):
                L.append(f"  {k}:")
                L += [f"    - {item}" for item in v]
            elif isinstance(v, dict):
                L.append(f"  {k}: " + ", ".join(f"{a}={b}" for a, b in v.items()))
            else:
                L.append(f"  {k}: {v}")
    if settings.public_url:
        L += ["", f"Dashboard:  {settings.public_url}/#/diagnose",
              "Dort gibt es einen kopierbaren Fehlerbericht mit dem ganzen Aufbau."]
    L += ["", "--",
          "Diese Nachricht kommt nur bei Incidents ab Schwere "
          f"'{settings.alert_min_severity}', die länger als "
          f"{settings.alert_min_duration_s} s bestehen."]
    return "\n".join(x for x in L if x is not None)


def _send_webhook(row, kind: str) -> None:
    url = settings.alert_webhook_url
    if not url:
        raise RuntimeError("keine Webhook-URL")
    data = json.dumps(_payload(row, kind)).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "aimonitor"}
    if settings.alert_webhook_headers:
        try:
            headers.update(json.loads(settings.alert_webhook_headers))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"AIMON_ALERT_WEBHOOK_HEADERS ist kein JSON-Objekt: {exc}"
            ) from exc
    # Dienste wie ntfy erwarten den Text im Body statt JSON. Dafür reicht es,
    # den Titel zusätzlich als Header mitzuschicken; beides stört sich nicht.
    headers.setdefault("X-Title", _subject_line(row, kind)[:200])

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            if r.status >= 300:
                raise RuntimeError(f"HTTP {r.status}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}: {exc.read()[:200].decode(errors='replace')}") from exc


def _send_mail(row, kind: str) -> None:
    if not settings.alert_smtp_host or not settings.alert_mail_to:
        raise RuntimeError("SMTP nicht vollständig konfiguriert")

    msg = EmailMessage()
    msg["Subject"] = _subject_line(row, kind)
    msg["From"] = settings.alert_mail_from or (
        settings.alert_smtp_user or "aimonitor@localhost")
    msg["To"] = ", ".join(settings.alert_mail_to)
    # Damit Alarm und Entwarnung im Mailprogramm zusammen einsortiert werden.
    msg["X-aimonitor-incident"] = str(row["id"])
    if kind == "resolved":
        msg["In-Reply-To"] = f"<aimon-{row['id']}@aimonitor>"
        msg["References"] = f"<aimon-{row['id']}@aimonitor>"
    else:
        msg["Message-ID"] = f"<aimon-{row['id']}@aimonitor>"
    msg.set_content(_body_text(row, kind))

    mode = settings.alert_smtp_tls
    host, port = settings.alert_smtp_host, settings.alert_smtp_port
    if mode == "ssl":
        srv = smtplib.SMTP_SSL(host, port, timeout=20)
    else:
        srv = smtplib.SMTP(host, port, timeout=20)
    try:
        if mode == "starttls":
            srv.starttls()
        if settings.alert_smtp_user:
            srv.login(settings.alert_smtp_user, settings.alert_smtp_password or "")
        srv.send_message(msg)
    finally:
        try:
            srv.quit()
        except Exception:  # noqa: BLE001
            srv.close()


async def send_test(channel: str | None = None) -> dict:
    """Eine Testnachricht schicken, ohne auf einen echten Vorfall zu warten."""
    import datetime as dt

    chans = channels()
    if channel:
        chans = [c for c in chans if c == channel]
    if not chans:
        return {"ok": False, "error": "kein Kanal konfiguriert",
                "configured": channels()}

    fake = {
        "id": 0, "rule": "aimon.test", "severity": "critical",
        "subject": "test", "subject_kind": "global",
        "title": "Testnachricht von aimonitor",
        "started_at": dt.datetime.now(dt.timezone.utc),
        "ended_at": None,
        "context": {"hinweis": "Wenn du das liest, funktioniert der Kanal.",
                    "min_severity": settings.alert_min_severity,
                    "min_duration_s": settings.alert_min_duration_s},
        "age_s": 0.0,
    }
    results = {}
    for c in chans:
        try:
            if c == "webhook":
                await asyncio.to_thread(_send_webhook, fake, "fired")
            else:
                await asyncio.to_thread(_send_mail, fake, "fired")
            results[c] = "ok"
        except Exception as exc:  # noqa: BLE001
            results[c] = str(exc)
    return {"ok": all(v == "ok" for v in results.values()), "results": results}
