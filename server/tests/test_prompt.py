"""Der Prompt-Generator: Formatierung und Anonymisierung."""
from __future__ import annotations

import re

from aimon.diagnostics.prompt import Redactor, _bytes, _duration, _fmt


def test_redactor_aus_laesst_alles_durch():
    r = Redactor(False)
    assert r.node("gpu-vm-01") == "gpu-vm-01"
    assert r.text("http://10.0.0.5:8000") == "http://10.0.0.5:8000"


def test_redactor_ist_stabil():
    """Derselbe Host muss im ganzen Text denselben Platzhalter bekommen,
    sonst ist der Bericht nicht mehr nachvollziehbar."""
    r = Redactor(True)
    a = r.node("gpu-vm-01")
    b = r.node("gpu-vm-01")
    c = r.node("gpu-vm-02")
    assert a == b
    assert a != c
    assert re.match(r"^node-\d+$", a)


def test_redactor_ersetzt_im_freitext():
    r = Redactor(True)
    alias = r.node("gpu-vm-01")
    out = r.text("GPU gpu-vm-01/gpu0 bei 91 Grad")
    assert "gpu-vm-01" not in out
    assert alias in out


def test_redactor_ersetzt_ips_und_hosts():
    r = Redactor(True)
    out = r.text("probe http://10.0.0.5:8000/health schlug fehl bei 192.168.1.9")
    assert "10.0.0.5" not in out
    assert "192.168.1.9" not in out


def test_redactor_verschachtelt_keine_aliase():
    """Ein bereits ersetzter Wert darf nicht noch einmal ersetzt werden,
    sonst zeigt die Legende Alias auf Alias."""
    r = Redactor(True)
    r.text("10.0.0.5")
    out = r.text(r.text("http://10.0.0.5/x"))
    aliases = [a for a, _ in r.legend()]
    for alias in aliases:
        assert not any(alias == o for _, o in r.legend()), \
            f"{alias} steht sowohl als Alias als auch als Original"
    assert "10.0.0.5" not in out


def test_service_alias_behaelt_die_art():
    r = Redactor(True)
    a = r.service("vllm@gpu-vm-01:8000")
    assert a.startswith("vllm-")


def test_legende_enthaelt_jede_zuordnung():
    r = Redactor(True)
    r.node("gpu-vm-01")
    r.service("vllm@gpu-vm-01:8000")
    leg = dict((o, a) for a, o in r.legend())
    assert "gpu-vm-01" in leg
    assert "vllm@gpu-vm-01:8000" in leg


def test_byte_formatierung():
    assert _bytes(0) == "0 B"
    assert _bytes(1536) == "1.5 KiB"
    assert _bytes(24 * 1024**3) == "24.0 GiB"


def test_dauer_formatierung():
    assert _duration(45) == "45 s"
    assert _duration(90) == "1 min 30 s"
    assert _duration(3700) == "1 h 1 min"
    assert _duration(90000) == "1 d 1 h"


def test_fehlende_werte_bleiben_sichtbar_fehlend():
    """Ein fehlender Messwert darf nie als 0 erscheinen."""
    assert _fmt(None) == "—"
    assert _fmt(0) == "0"
    assert _fmt(0.0) == "0"
