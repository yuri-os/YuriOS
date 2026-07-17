"""The weather seam (SPEC §7.5) — the parser pinned over httpx.MockTransport."""
from __future__ import annotations

import json

import httpx
import pytest

from yurios.world.tools.weather import FakeWeather, OpenMeteoProvider

GEO_HIT = {"results": [{"name": "Tokyo", "latitude": 35.68, "longitude": 139.69}]}
WX = {"current": {"temperature_2m": 17.2, "weather_code": 63, "wind_speed_10m": 9.4}}


def transport(geo=GEO_HIT, wx=WX):
    def handler(request: httpx.Request) -> httpx.Response:
        body = geo if "geocoding" in request.url.host else wx
        return httpx.Response(200, text=json.dumps(body))
    return httpx.MockTransport(handler)


async def test_open_meteo_two_calls_and_wmo_mapping():
    provider = OpenMeteoProvider(transport=transport())
    out = await provider.current("tokyo")
    assert out == {"city": "Tokyo", "temp_c": 17.2, "condition": "raining",
                   "wind_kmh": 9.4}


async def test_unknown_city_raises():
    provider = OpenMeteoProvider(transport=transport(geo={"results": []}))
    with pytest.raises(ValueError, match="unknown city"):
        await provider.current("atlantis")


async def test_unknown_wmo_code_reads_as_unsettled():
    wx = {"current": {"temperature_2m": 5.0, "weather_code": 42, "wind_speed_10m": 1.0}}
    provider = OpenMeteoProvider(transport=transport(wx=wx))
    out = await provider.current("tokyo")
    assert out["condition"] == "unsettled"


async def test_fake_weather_is_deterministic_and_raining():
    out = await FakeWeather().current("anywhere")
    assert out["condition"] == "raining" and out["city"] == "anywhere"
