"""Histogramm-Quantile und Zeitraum-Parsing."""
from __future__ import annotations

import datetime as dt
import math

import pytest

from aimon.tsdb.query import _hist_quantile, pick_resolution


def test_interpolation_im_bucket():
    # kumulativ: <=0.1 -> 10, <=0.5 -> 60, <=1.0 -> 90, +Inf -> 100
    b = [(0.1, 10), (0.5, 60), (1.0, 90), (float("inf"), 100)]
    # p50 -> Ziel 50 liegt zwischen 10 und 60: 0.1 + (40/50)*0.4
    assert _hist_quantile(b, 0.5) == pytest.approx(0.42)
    # p90 -> Ziel 90 trifft die Bucketgrenze exakt
    assert _hist_quantile(b, 0.9) == pytest.approx(1.0)
    # p99 landet im +Inf-Bucket: die höchste endliche Grenze ist die ehrlichste
    # Aussage, die das Histogramm hergibt
    assert _hist_quantile(b, 0.99) == pytest.approx(1.0)


def test_leeres_histogramm_gibt_nichts_zurueck():
    assert _hist_quantile([], 0.5) is None
    assert _hist_quantile([(1.0, 0)], 0.5) is None


def test_einziger_bucket():
    assert _hist_quantile([(2.5, 42)], 0.5) == pytest.approx(1.25)


def test_unsortierte_buckets_werden_sortiert():
    b = [(1.0, 90), (0.1, 10), (float("inf"), 100), (0.5, 60)]
    assert _hist_quantile(b, 0.5) == pytest.approx(0.42)


def test_aufloesung_waechst_mit_dem_zeitraum():
    now = dt.datetime.now(dt.timezone.utc)

    def pr(**kw):
        return pick_resolution(now - dt.timedelta(**kw), now, 400)

    assert pr(hours=1) == (0, "raw")
    assert pr(hours=6) == (0, "raw")
    assert pr(days=7) == (60, "rollup_60")
    assert pr(days=30) == (60, "rollup_60")
    assert pr(days=365) == (3600, "rollup_3600")


def test_quelle_haengt_an_der_fensterbreite_nicht_an_der_punktzahl():
    """Wer wenige Punkte will – etwa für eine Sparkline – soll gröbere
    Buckets über denselben Daten bekommen, nicht plötzlich eine andere
    Quelle. Sonst verschwindet die Kurve, solange der Rollup hinterherhinkt."""
    now = dt.datetime.now(dt.timezone.utc)
    start = now - dt.timedelta(hours=1)
    for points in (10, 60, 400, 2000):
        bucket, source = pick_resolution(start, now, points)
        assert source == "raw", f"{points} Punkte -> {source}"
        assert bucket == 0


def test_lange_fenster_nutzen_die_verdichteten_daten():
    now = dt.datetime.now(dt.timezone.utc)

    def src(**kw):
        return pick_resolution(now - dt.timedelta(**kw), now, 60)[1]

    assert src(hours=6) == "raw"
    assert src(hours=7) == "rollup_60"
    assert src(days=30) == "rollup_60"
    assert src(days=60) == "rollup_3600"
    assert src(days=400) == "rollup_3600"
