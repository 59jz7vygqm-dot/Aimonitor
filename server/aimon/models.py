"""Pydantic-Modelle = ausführbare Fassung von docs/integration-contract.md."""
from __future__ import annotations

import datetime as dt
import math
import re
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
)

METRIC_RE = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")

SERVICE_KINDS = {
    "vllm",
    "ollama",
    "comfyui",
    "openwebui",
    "searxng",
    "privacy_proxy",
    "openterminal_proxy",
    "openterminal_instance",
    "tap",
    "other",
}

SEVERITIES = ("debug", "info", "warning", "error", "critical")


def _parse_ts(v: Any) -> Any:
    """RFC3339-String oder Unix-Sekunden -> aware datetime."""
    if v is None or isinstance(v, dt.datetime):
        return v
    if isinstance(v, (int, float)):
        if not math.isfinite(v):
            raise ValueError("ts ist nicht endlich")
        # Millisekunden freundlich behandeln: alles > Jahr 5138 ist sicher ms.
        if v > 1e11:
            v = v / 1000.0
        return dt.datetime.fromtimestamp(v, tz=dt.timezone.utc)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return _parse_ts(float(s))
        except ValueError:
            pass
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            # Ohne Zone: als UTC lesen. Die lokale Zone des Servers anzunehmen
            # wäre je nach Deployment eine andere Zeit.
            return d.replace(tzinfo=dt.timezone.utc)
        # Mit Offset: auf UTC normalisieren, damit alle Zeitstempel im Modell
        # vergleichbar sind und nicht je nach Absender anders formatiert werden.
        return d.astimezone(dt.timezone.utc)
    raise ValueError(f"ts nicht interpretierbar: {v!r}")


Ts = Annotated[dt.datetime | None, BeforeValidator(_parse_ts)]


def _clean_labels(v: Any) -> dict[str, str]:
    if not v:
        return {}
    if not isinstance(v, dict):
        raise ValueError("labels muss ein Objekt sein")
    out: dict[str, str] = {}
    for k, val in list(v.items())[:32]:
        if val is None:
            continue
        ks = str(k)[:64]
        out[ks] = str(val)[:200]
    return out


Labels = Annotated[dict[str, str], BeforeValidator(_clean_labels)]


class Lax(BaseModel):
    """Unbekannte Felder sind erlaubt und werden als Warnung gemeldet, nie als Fehler."""

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    def unknown_fields(self) -> list[str]:
        return sorted((self.model_extra or {}).keys())


class NodeIn(Lax):
    name: str = Field(min_length=1, max_length=128)
    hostname: str | None = Field(default=None, max_length=255)
    agent_version: str | None = Field(default=None, max_length=64)
    labels: Labels = {}


class ServiceIn(Lax):
    key: str = Field(min_length=1, max_length=200)
    kind: str = "other"
    name: str | None = Field(default=None, max_length=200)
    base_url: str | None = Field(default=None, max_length=500)
    version: str | None = Field(default=None, max_length=64)
    labels: Labels = {}

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        v = (v or "other").strip().lower()
        return v if v in SERVICE_KINDS else "other"


class SampleIn(Lax):
    metric: str
    value: float
    ts: Ts = None
    labels: Labels = {}
    service: str | None = None

    @field_validator("metric")
    @classmethod
    def _metric(cls, v: str) -> str:
        # Kleinschreibung ist Absicht: sonst wären `GPU.Temp` und `gpu.temp`
        # zwei getrennte Metriken, je nachdem wer sie schickt.
        v = (v or "").strip().lower()
        if not METRIC_RE.match(v):
            raise ValueError(f"ungültiger Metrikname: {v!r}")
        return v

    @field_validator("value")
    @classmethod
    def _value(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("value muss endlich sein (kein NaN/Inf)")
        return float(v)


class RequestIn(Lax):
    service: str
    ts: Ts = None
    model: str | None = Field(default=None, max_length=200)
    ttft_ms: float | None = None
    total_ms: float | None = None
    queue_ms: float | None = None
    prompt_tokens: int | None = None
    prompt_chars: int | None = None
    completion_tokens: int | None = None
    status: int | None = None
    stream: bool | None = None
    client: str | None = Field(default=None, max_length=64)
    route: str | None = Field(default=None, max_length=200)
    error: str | None = Field(default=None, max_length=200)

    @field_validator("ttft_ms", "total_ms", "queue_ms")
    @classmethod
    def _finite(cls, v: float | None) -> float | None:
        if v is None:
            return None
        if not math.isfinite(v) or v < 0:
            return None
        return float(v)


class ProbeIn(Lax):
    target: str = Field(min_length=1, max_length=500)
    ok: bool
    ts: Ts = None
    target_service: str | None = Field(default=None, max_length=200)
    latency_ms: float | None = None
    http_status: int | None = None
    error: str | None = Field(default=None, max_length=300)
    source_service: str | None = Field(default=None, max_length=200)


class EventIn(Lax):
    kind: str = Field(min_length=1, max_length=100)
    severity: str = "info"
    title: str = Field(min_length=1, max_length=500)
    ts: Ts = None
    service: str | None = Field(default=None, max_length=200)
    detail: dict[str, Any] = Field(default_factory=dict)
    dedup_key: str | None = Field(default=None, max_length=200)

    @field_validator("severity")
    @classmethod
    def _sev(cls, v: str) -> str:
        v = (v or "info").strip().lower()
        return v if v in SEVERITIES else "info"


class Batch(Lax):
    node: NodeIn
    services: list[ServiceIn] = Field(default_factory=list)
    samples: list[SampleIn] = Field(default_factory=list)
    requests: list[RequestIn] = Field(default_factory=list)
    probes: list[ProbeIn] = Field(default_factory=list)
    events: list[EventIn] = Field(default_factory=list)

    def item_count(self) -> int:
        return (
            len(self.services)
            + len(self.samples)
            + len(self.requests)
            + len(self.probes)
            + len(self.events)
        )


# ----------------------------------------------------------- Spezial-Endpunkte


class ScalerInstance(Lax):
    id: str = Field(max_length=100)
    state: str | None = Field(default=None, max_length=32)
    sessions: int | None = None
    started_at: Ts = None
    cpu_pct: float | None = None
    mem_used_bytes: float | None = None
    host: str | None = Field(default=None, max_length=255)
    port: int | None = None


class ScalerState(Lax):
    instances_running: int | None = None
    instances_desired: int | None = None
    instances_min: int | None = None
    instances_max: int | None = None
    instances_starting: int | None = None
    instances_draining: int | None = None
    sessions_active: int | None = None
    sessions_per_instance_target: int | None = None
    sessions_queued: int | None = None
    cpu_pct: float | None = None
    mem_used_bytes: float | None = None
    mem_total_bytes: float | None = None
    scale_cooldown_s_remaining: float | None = None
    last_scale_reason: str | None = Field(default=None, max_length=500)


class ScalerIn(Lax):
    node: NodeIn
    service: ServiceIn
    ts: Ts = None
    state: ScalerState = Field(default_factory=ScalerState)
    instances: list[ScalerInstance] = Field(default_factory=list)
    events: list[EventIn] = Field(default_factory=list)


class PrivacyDecision(Lax):
    action: Literal["allowed", "redacted", "blocked", "error"]
    ts: Ts = None
    category: str | None = Field(default=None, max_length=64)
    rule: str | None = Field(default=None, max_length=128)
    confidence: float | None = None
    query_redacted: str | None = Field(default=None, max_length=2000)
    query_raw: str | None = Field(default=None, max_length=2000)
    query_sha256: str | None = Field(default=None, max_length=64)
    query_len: int | None = None
    matched_spans: list[dict[str, Any]] = Field(default_factory=list)
    client_id: str | None = Field(default=None, max_length=128)
    upstream_latency_ms: float | None = None
    upstream_status: int | None = None
    engines: list[str] = Field(default_factory=list)


class PrivacyCounters(Lax):
    requests_total: float | None = None
    allowed_total: float | None = None
    blocked_total: float | None = None
    redacted_total: float | None = None
    errors_total: float | None = None
    upstream_errors_total: float | None = None
    blocked_by_category: dict[str, float] = Field(default_factory=dict)
    redacted_by_category: dict[str, float] = Field(default_factory=dict)
    upstream_latency_ms_sum: float | None = None
    upstream_latency_ms_count: float | None = None
    query_len_sum: float | None = None
    query_len_count: float | None = None


class PrivacyIn(Lax):
    node: NodeIn
    service: ServiceIn
    ts: Ts = None
    counters: PrivacyCounters = Field(default_factory=PrivacyCounters)
    decisions: list[PrivacyDecision] = Field(default_factory=list)


class IngestResult(BaseModel):
    accepted: int = 0
    rejected: int = 0
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    duplicate: bool = False
