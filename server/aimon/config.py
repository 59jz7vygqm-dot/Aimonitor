"""Konfiguration aus Umgebungsvariablen. Keine Datei, keine Magie."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import quote


def _b(key: str, default: bool) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _i(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, "") or default)
    except ValueError:
        return default


def _f(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, "") or default)
    except ValueError:
        return default



def _database_url() -> str:
    """DSN ermitteln.

    Entweder komplett aus AIMON_DATABASE_URL, oder aus Einzelteilen
    zusammengesetzt. Letzteres ist der Normalfall, weil ein Passwort aus
    `openssl rand -base64 32` mit hoher Wahrscheinlichkeit ein `/` enthält -
    und das zerlegt eine von Hand zusammengeklebte URL, weil der Parser dort
    den Pfad beginnen sieht. Deshalb wird das Passwort hier prozent-kodiert.
    """
    explicit = os.getenv("AIMON_DATABASE_URL")
    if explicit:
        return explicit

    host = os.getenv("AIMON_DB_HOST", "localhost")
    port = os.getenv("AIMON_DB_PORT", "5432")
    user = os.getenv("AIMON_DB_USER", "aimon")
    password = os.getenv("AIMON_DB_PASSWORD", "aimon")
    name = os.getenv("AIMON_DB_NAME", "aimon")

    return (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{quote(name, safe='')}"
    )


@dataclass
class Settings:
    version: str = "0.1.0"
    # Commit, aus dem dieses Image gebaut wurde. Wird beim Build gesetzt.
    build: str = field(
        default_factory=lambda: os.getenv("AIMON_BUILD") or "unbekannt"
    )

    database_url: str = field(default_factory=lambda: _database_url())
    db_pool_min: int = field(default_factory=lambda: _i("AIMON_DB_POOL_MIN", 2))
    db_pool_max: int = field(default_factory=lambda: _i("AIMON_DB_POOL_MAX", 12))

    bootstrap_token: str | None = field(
        default_factory=lambda: os.getenv("AIMON_BOOTSTRAP_TOKEN") or None
    )
    # Wenn gesetzt, verlangen alle Lese-Endpunkte dieses Token (oder ein Token
    # mit Scope `read`). Wenn leer, ist die Lese-API offen -> hinter Reverse-Proxy stellen.
    read_token: str | None = field(
        default_factory=lambda: os.getenv("AIMON_READ_TOKEN") or None
    )

    # Datenschutz
    allow_raw_queries: bool = field(
        default_factory=lambda: _b("AIMON_ALLOW_RAW_QUERIES", False)
    )
    raw_query_retention_days: int = field(
        default_factory=lambda: _i("AIMON_RAW_QUERY_RETENTION_DAYS", 2)
    )

    # Retention
    raw_retention_days: int = field(default_factory=lambda: _i("AIMON_RAW_RETENTION_DAYS", 14))
    rollup_1m_days: int = field(default_factory=lambda: _i("AIMON_ROLLUP_1M_DAYS", 90))
    rollup_1h_days: int = field(default_factory=lambda: _i("AIMON_ROLLUP_1H_DAYS", 730))
    requests_retention_days: int = field(
        default_factory=lambda: _i("AIMON_REQUESTS_RETENTION_DAYS", 30)
    )
    events_retention_days: int = field(
        default_factory=lambda: _i("AIMON_EVENTS_RETENTION_DAYS", 180)
    )
    probes_retention_days: int = field(
        default_factory=lambda: _i("AIMON_PROBES_RETENTION_DAYS", 14)
    )

    # Worker
    rollup_interval_s: int = field(default_factory=lambda: _i("AIMON_ROLLUP_INTERVAL_S", 60))
    # Wie weit hinter den Wasserstand der Rollup bei jedem Lauf zurückgeht, um
    # nachträglich eingetroffene Daten (gepufferte Agents) zu erfassen.
    # Sollte mindestens so groß sein wie der Puffer der Agents (Default 20 min).
    rollup_lookback_s: int = field(default_factory=lambda: _i("AIMON_ROLLUP_LOOKBACK_S", 1800))
    retention_interval_s: int = field(
        default_factory=lambda: _i("AIMON_RETENTION_INTERVAL_S", 3600)
    )
    rules_interval_s: int = field(default_factory=lambda: _i("AIMON_RULES_INTERVAL_S", 30))

    # Ein Node gilt als `stale`, wenn er so lange nichts geschickt hat.
    stale_after_s: int = field(default_factory=lambda: _i("AIMON_STALE_AFTER_S", 90))

    # Limits
    max_body_bytes: int = field(default_factory=lambda: _i("AIMON_MAX_BODY_BYTES", 5_242_880))
    max_items_per_batch: int = field(default_factory=lambda: _i("AIMON_MAX_ITEMS", 10_000))
    rate_limit_per_min: int = field(default_factory=lambda: _i("AIMON_RATE_LIMIT_PER_MIN", 600))

    # Schwellen für das Regelwerk
    gpu_temp_warn_c: float = field(default_factory=lambda: _f("AIMON_GPU_TEMP_WARN_C", 83.0))
    gpu_temp_crit_c: float = field(default_factory=lambda: _f("AIMON_GPU_TEMP_CRIT_C", 88.0))
    vram_warn_pct: float = field(default_factory=lambda: _f("AIMON_VRAM_WARN_PCT", 92.0))
    vram_crit_pct: float = field(default_factory=lambda: _f("AIMON_VRAM_CRIT_PCT", 98.0))
    ttft_warn_ms: float = field(default_factory=lambda: _f("AIMON_TTFT_WARN_MS", 2000.0))
    ttft_crit_ms: float = field(default_factory=lambda: _f("AIMON_TTFT_CRIT_MS", 8000.0))
    kv_cache_warn_pct: float = field(default_factory=lambda: _f("AIMON_KV_CACHE_WARN_PCT", 90.0))
    # Unterhalb dieser Auslastung ist ein gesetztes Power-Cap-Flag auffällig;
    # darüber ist es der Normalbetrieb einer ausgelasteten Karte.
    power_cap_idle_util_pct: float = field(
        default_factory=lambda: _f("AIMON_POWER_CAP_IDLE_UTIL_PCT", 30.0)
    )
    headroom_warn_pct: float = field(default_factory=lambda: _f("AIMON_HEADROOM_WARN_PCT", 20.0))
    headroom_crit_pct: float = field(default_factory=lambda: _f("AIMON_HEADROOM_CRIT_PCT", 5.0))
    disk_warn_pct: float = field(default_factory=lambda: _f("AIMON_DISK_WARN_PCT", 88.0))
    inodes_warn_pct: float = field(
        default_factory=lambda: _f("AIMON_INODES_WARN_PCT", 85.0)
    )
    mem_warn_pct: float = field(default_factory=lambda: _f("AIMON_MEM_WARN_PCT", 90.0))
    mem_crit_pct: float = field(default_factory=lambda: _f("AIMON_MEM_CRIT_PCT", 96.0))
    # Auf Maschinen mit gemeinsamem Speicher (GB10, Grace, Jetson) ist der
    # Arbeitsspeicher zugleich der VRAM. Dort wird es nicht langsam, wenn er
    # knapp wird - dort beendet der OOM-Killer das Modell. Deshalb frueher.
    mem_warn_pct_shared: float = field(
        default_factory=lambda: _f("AIMON_MEM_WARN_PCT_SHARED", 82.0)
    )
    # Swap auf einem Inference-Host heisst: der Arbeitsspeicher war knapp.
    # Auch wenn er wieder frei ist, bleibt Ausgelagertes liegen - der Wert
    # ist deshalb ein Hinweis, keine akute Meldung.
    swap_warn_pct: float = field(
        default_factory=lambda: _f("AIMON_SWAP_WARN_PCT", 25.0)
    )
    # CPU-Temperatur: die Grenzen haengen am Modell. Konservativ gesetzt,
    # damit die Regel nicht staendig anschlaegt.
    cpu_temp_warn_c: float = field(
        default_factory=lambda: _f("AIMON_CPU_TEMP_WARN_C", 90.0)
    )
    cpu_temp_crit_c: float = field(
        default_factory=lambda: _f("AIMON_CPU_TEMP_CRIT_C", 97.0)
    )
    block_rate_warn_pct: float = field(default_factory=lambda: _f("AIMON_BLOCK_RATE_WARN_PCT", 25.0))
    # Unterhalb dieser Zahl von Anfragen ist eine Blockquote keine Aussage.
    block_rate_min_requests: int = field(
        default_factory=lambda: _i("AIMON_BLOCK_RATE_MIN_REQUESTS", 50)
    )

    # ---------------------------------------------------------- Kanarienvogel
    # Echte Mini-Requests durch den Weg, den ein Benutzer nimmt. Ohne Ziele
    # passiert nichts. Format: JSON-Liste aus
    # {"name":..., "url":..., "model":..., "api_key":..., "prompt":...}
    canary_targets: str | None = field(
        default_factory=lambda: os.getenv("AIMON_CANARY_TARGETS")
    )
    canary_interval_s: int = field(
        default_factory=lambda: _i("AIMON_CANARY_INTERVAL_S", 300)
    )
    canary_timeout_s: float = field(
        default_factory=lambda: _f("AIMON_CANARY_TIMEOUT_S", 30.0)
    )
    # Unter welchem Knoten die Ergebnisse auftauchen. Der Kanarienvogel laeuft
    # im Server, nicht auf einer der ueberwachten Maschinen.
    canary_node: str = field(
        default_factory=lambda: os.getenv("AIMON_CANARY_NODE", "aimon-server")
    )
    canary_ttft_warn_ms: float = field(
        default_factory=lambda: _f("AIMON_CANARY_TTFT_WARN_MS", 5000.0)
    )

    # ------------------------------------------------------------ Alerting
    #
    # Default aus. Eingeschaltet wird nur bei kritischen Incidents benachrichtigt,
    # und erst, wenn sie eine Weile offen sind - sonst meldet sich das System
    # bei jeder kurzen Lastspitze und wird nach einer Woche weggefiltert.
    alert_min_severity: str = field(
        default_factory=lambda: (os.getenv("AIMON_ALERT_MIN_SEVERITY") or "critical")
        .strip().lower()
    )
    alert_min_duration_s: int = field(
        default_factory=lambda: _i("AIMON_ALERT_MIN_DURATION_S", 300)
    )
    alert_send_resolved: bool = field(
        default_factory=lambda: _b("AIMON_ALERT_SEND_RESOLVED", True)
    )
    # Höchstens so viele Benachrichtigungen pro Stunde, über alle Incidents.
    # Bremse gegen Ketteneffekte: fällt ein Switch aus, sind sofort zwanzig
    # Dinge kritisch, und zwanzig Mails helfen niemandem.
    alert_max_per_hour: int = field(
        default_factory=lambda: _i("AIMON_ALERT_MAX_PER_HOUR", 10)
    )

    alert_webhook_url: str | None = field(
        default_factory=lambda: os.getenv("AIMON_ALERT_WEBHOOK_URL") or None
    )
    alert_webhook_headers: str | None = field(
        default_factory=lambda: os.getenv("AIMON_ALERT_WEBHOOK_HEADERS") or None
    )

    alert_smtp_host: str | None = field(
        default_factory=lambda: os.getenv("AIMON_ALERT_SMTP_HOST") or None
    )
    alert_smtp_port: int = field(default_factory=lambda: _i("AIMON_ALERT_SMTP_PORT", 587))
    alert_smtp_user: str | None = field(
        default_factory=lambda: os.getenv("AIMON_ALERT_SMTP_USER") or None
    )
    alert_smtp_password: str | None = field(
        default_factory=lambda: os.getenv("AIMON_ALERT_SMTP_PASSWORD") or None
    )
    alert_smtp_tls: str = field(
        default_factory=lambda: (os.getenv("AIMON_ALERT_SMTP_TLS") or "starttls")
        .strip().lower()
    )
    alert_mail_from: str | None = field(
        default_factory=lambda: os.getenv("AIMON_ALERT_MAIL_FROM") or None
    )
    alert_mail_to: list[str] = field(
        default_factory=lambda: [
            a.strip() for a in (os.getenv("AIMON_ALERT_MAIL_TO") or "").replace(";", ",").split(",")
            if a.strip()
        ]
    )
    # Für die Links in der Benachrichtigung.
    public_url: str = field(
        default_factory=lambda: (os.getenv("AIMON_PUBLIC_URL") or "").rstrip("/")
    )

    cors_origins: list[str] = field(
        default_factory=lambda: [
            o.strip() for o in (os.getenv("AIMON_CORS_ORIGINS", "") or "").split(",") if o.strip()
        ]
    )


settings = Settings()
