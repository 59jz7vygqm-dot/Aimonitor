"""Die UI darf nicht veralten.

Ohne Cache-Header entscheidet der Browser selbst, wie lange er eine
JavaScript-Datei behaelt. Nach einem Update sieht man dann die alte
Oberflaeche und sucht den Fehler an der falschen Stelle. Diese Tests halten
fest, dass jede ausgelieferte Datei neu nachgefragt wird.
"""
from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def http():
    from aimon.main import WEB_DIR, app

    if not WEB_DIR.is_dir():  # pragma: no cover
        pytest.skip("kein web/-Verzeichnis")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://test") as c:
        yield c


@pytest.mark.parametrize("path", [
    "/",
    "/favicon.svg",
    "/static/js/app.js",
    "/static/css/app.css",
    "/static/js/views/overview.js",
])
async def test_kein_stilles_caching(http, path):
    r = await http.get(path)
    assert r.status_code == 200, path
    assert r.headers.get("cache-control") == "no-cache", path


async def test_unveraendert_gibt_304(http):
    """"no-cache" heisst nachfragen, nicht neu laden.

    Ist die Datei gleich geblieben, antwortet der Server mit 304 und ohne
    Inhalt. Sonst waere der Header teuer.
    """
    first = await http.get("/static/js/app.js")
    etag = first.headers["etag"]
    again = await http.get("/static/js/app.js",
                           headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert not again.content
