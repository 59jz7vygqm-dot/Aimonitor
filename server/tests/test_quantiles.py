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
    assert pr(hours=6) == (60, "rollup_60")
    assert pr(days=7) == (60, "rollup_60")
    assert pr(days=30) == (3600, "rollup_3600")
    assert pr(days=365) == (3600, "rollup_3600")


def test_aufloesung_respektiert_max_points():
    now = dt.datetime.now(dt.timezone.utc)
    start = now - dt.timedelta(hours=1)
    # Wenige Punkte gewünscht -> gröbere Quelle
    assert pick_resolution(start, now, 10)[1] == "rollup_60"
    assert pick_resolution(start, now, 2000)[1] == "raw"
