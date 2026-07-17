"""The weather seam (SPEC §7.5) — a real lookup behind a Protocol, with a fake.

Open-Meteo is the reference backend because it is keyless: the default stack
still needs no account anywhere. It is also the *one* call in the default build
that leaves the machine — behind this seam, logged by the guard, and degrading
to an honest error string when the cable is pulled (she says she can't see the
sky, she doesn't crash).
"""
from __future__ import annotations

from typing import Protocol

import httpx

# WMO weather interpretation codes → words she can actually say.
_CONDITIONS = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "foggy", 51: "drizzling", 53: "drizzling", 55: "drizzling",
    61: "raining lightly", 63: "raining", 65: "raining hard",
    71: "snowing lightly", 73: "snowing", 75: "snowing hard",
    80: "showery", 81: "showery", 82: "showery",
    95: "stormy", 96: "stormy", 99: "stormy",
}


class WeatherProvider(Protocol):
    async def current(self, city: str) -> dict:
        """Return {"city", "temp_c", "condition", "wind_kmh"}. Raises on failure."""
        ...


class OpenMeteoProvider:
    """Geocode the city, then read the current conditions. Keyless, two GETs."""

    GEO = "https://geocoding-api.open-meteo.com/v1/search"
    WX = "https://api.open-meteo.com/v1/forecast"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        # `transport` is the test seam: httpx.MockTransport serves canned payloads
        # so the parser is pinned without the network (SPEC §13).
        self._transport = transport

    async def current(self, city: str) -> dict:
        async with httpx.AsyncClient(transport=self._transport, timeout=8.0) as client:
            geo = (await client.get(self.GEO, params={"name": city, "count": 1})).json()
            hits = geo.get("results") or []
            if not hits:
                raise ValueError(f"unknown city: {city}")
            lat, lon = hits[0]["latitude"], hits[0]["longitude"]
            name = hits[0].get("name", city)
            wx = (await client.get(self.WX, params={
                "latitude": lat, "longitude": lon,
                "current": "temperature_2m,weather_code,wind_speed_10m",
            })).json()
        cur = wx.get("current") or {}
        return {
            "city": name,
            "temp_c": cur.get("temperature_2m"),
            "condition": _CONDITIONS.get(cur.get("weather_code"), "unsettled"),
            "wind_kmh": cur.get("wind_speed_10m"),
        }


class FakeWeather:
    """Deterministic, offline — canon weather: it is always raining somewhere."""

    async def current(self, city: str) -> dict:
        return {"city": city, "temp_c": 17.0, "condition": "raining",
                "wind_kmh": 9.0}
