"""DSN-Aufbau. Ein Passwort darf die Adresse nicht zerlegen können."""
from __future__ import annotations

import pytest

from aimon.config import _database_url


def test_explizite_url_gewinnt(monkeypatch):
    monkeypatch.setenv("AIMON_DATABASE_URL", "postgresql://x@y:1/z")
    monkeypatch.setenv("AIMON_DB_HOST", "ignoriert")
    assert _database_url() == "postgresql://x@y:1/z"


def test_sonderzeichen_im_passwort_werden_kodiert(monkeypatch):
    """Der Fall, der in der Praxis zuschlägt: `openssl rand -base64 32`
    erzeugt mit hoher Wahrscheinlichkeit ein '/', und ein rohes '/' im
    Passwort beendet den Host-Teil der URL."""
    monkeypatch.delenv("AIMON_DATABASE_URL", raising=False)
    monkeypatch.setenv("AIMON_DB_HOST", "db")
    monkeypatch.setenv("AIMON_DB_PASSWORD", "c8RjEbKqsXNnBuBqSB/x+y==")
    url = _database_url()
    assert "%2F" in url
    assert "/x+y" not in url
    # Nach dem Passwort muss genau ein @ und dann Host:Port/DB folgen.
    assert url.endswith("@db:5432/aimon")


@pytest.mark.parametrize("pw", [
    "einfach", "mit/slash", "mit@at", "mit:doppelpunkt", "mit#raute",
    "mit?frage", "mit%prozent", "Kv/9Qx+Tz3n8ab==", "ümläute", "mit leer",
])
def test_jedes_passwort_ergibt_eine_lesbare_adresse(monkeypatch, pw):
    """asyncpg muss jede erzeugte Adresse zerlegen können."""
    from asyncpg import connect_utils

    monkeypatch.delenv("AIMON_DATABASE_URL", raising=False)
    monkeypatch.setenv("AIMON_DB_HOST", "db")
    monkeypatch.setenv("AIMON_DB_PORT", "5432")
    monkeypatch.setenv("AIMON_DB_USER", "aimon")
    monkeypatch.setenv("AIMON_DB_PASSWORD", pw)
    monkeypatch.setenv("AIMON_DB_NAME", "aimon")

    addrs, params = connect_utils._parse_connect_dsn_and_args(
        dsn=_database_url(), host=None, port=None, user=None, password=None,
        passfile=None, database=None, ssl=None, direct_tls=False,
        server_settings=None, target_session_attrs="any", krbsrvname=None,
        gsslib=None,
    )
    assert addrs[0] == ("db", 5432)
    assert params.user == "aimon"
    assert params.password == pw, "das Passwort muss unverändert ankommen"
    assert params.database == "aimon"


def test_defaults_ohne_umgebung(monkeypatch):
    for k in ("AIMON_DATABASE_URL", "AIMON_DB_HOST", "AIMON_DB_PORT",
              "AIMON_DB_USER", "AIMON_DB_PASSWORD", "AIMON_DB_NAME"):
        monkeypatch.delenv(k, raising=False)
    assert _database_url() == "postgresql://aimon:aimon@localhost:5432/aimon"


def test_passwort_wird_im_log_maskiert(monkeypatch):
    """Die Startmeldung zeigt die Adresse - aber nie das Passwort."""
    from aimon import db as db_mod
    from aimon.config import settings

    monkeypatch.setattr(settings, "database_url",
                        "postgresql://aimon:c8RjEbKqsXNnBuBqSB%2Fx@db:5432/aimon")
    out = db_mod.masked_dsn()
    assert "c8RjEbKqsXNnBuBqSB" not in out
    assert "***" in out
    assert "db:5432" in out
    assert out.endswith("/aimon")


def test_maskierung_haelt_kaputte_adresse_aus(monkeypatch):
    from aimon import db as db_mod
    from aimon.config import settings

    monkeypatch.setattr(settings, "database_url", "das ist keine url")
    assert isinstance(db_mod.masked_dsn(), str)
