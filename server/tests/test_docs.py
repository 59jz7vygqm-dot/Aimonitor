"""Die Dokumentation der Regeln muss zum Code passen.

Eine Liste erkannter Probleme, die nicht stimmt, ist schlimmer als keine:
man verlaesst sich darauf, dass etwas ueberwacht wird, und das tut es dann
nicht. Deshalb prueft ein Test das, nicht die Sorgfalt.
"""
from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
RULES_PY = REPO / "server" / "aimon" / "diagnostics" / "rules.py"
RULES_MD = REPO / "docs" / "rules.md"


def rule_ids_im_code() -> set[str]:
    src = RULES_PY.read_text()
    return set(re.findall(r'Finding\(\s*"([a-z][a-z0-9_.]*)"', src))


def test_jede_regel_ist_dokumentiert():
    doc = RULES_MD.read_text()
    fehlt = sorted(r for r in rule_ids_im_code() if f"`{r}`" not in doc)
    assert not fehlt, f"nicht in docs/rules.md: {fehlt}"


def test_keine_erfundenen_regeln_in_der_doku():
    doc = RULES_MD.read_text()
    im_code = rule_ids_im_code()
    # Nur Eintraege aus den Tabellenspalten pruefen, nicht jeden Backtick im
    # Fliesstext (dort stehen auch Umgebungsvariablen und Dateinamen).
    genannt = set(re.findall(r'^\| `([a-z][a-z0-9_]*\.[a-z0-9_.]+)`', doc, re.M))
    erfunden = sorted(genannt - im_code)
    assert not erfunden, f"dokumentiert, aber nicht implementiert: {erfunden}"
