"""Konfiguration aus Umgebungsvariablen. Keine Datei, keine Magie."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


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


@dataclass
class Settings:
    version: str = "0.1.0"

    database_url: str = field(
        default_factory=lambda: os.getenv(
            "AIMON_DATABASE_URL",
            "postgresql://aimon:aimon@localhost:5432/aimon",
        )
    )
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
    headroom_warn_pct: float = field(default_factory=lambda: _f("AIMON_HEADROOM_WARN_PCT", 20.0))
    headroom_crit_pct: float = field(default_factory=lambda: _f("AIMON_HEADROOM_CRIT_PCT", 5.0))
    disk_warn_pct: float = field(default_factory=lambda: _f("AIMON_DISK_WARN_PCT", 88.0))
    block_rate_warn_pct: float = field(default_factory=lambda: _f("AIMON_BLOCK_RATE_WARN_PCT", 25.0))

    cors_origins: list[str] = field(
        default_factory=lambda: [
            o.strip() for o in (os.getenv("AIMON_CORS_ORIGINS", "") or "").split(",") if o.strip()
        ]
    )


settings = Settings()
