"""KNMI EDR parameter codes (#48).

The source reads the EDR collection 10-minute-in-situ-meteorological-observations.
KNMI's variable table for it: ``qg`` is global solar radiation (W/m2), ``rg`` is
the rain gauge's precipitation intensity (mm/h), ``gff`` the 10-minute maximum
wind gust at 10 m (m/s) and ``R1H`` the past hour's rainfall (mm). Until
2026-11 the source stored ``rg`` as solar radiation, and asked for gust and
rainfall under codes the dataset does not have (``fxx``, ``r1h``), and not even
in its request.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest
import requests

import app.services.knmi_weather_source as knmi_source_module
from app.services.knmi_weather_source import KNMIWeatherPollingSource
from app.sources.knmi_weather import (
    _DESCRIPTIONS,
    _KNMI_EDR_PARAM_MAP,
    EDR_LOCATION_ID,
    EDR_PARAMETER_NAMES,
    build_entity_set,
    parse_edr_locations,
)
from app.sources.normalizers import KNMIWeatherNormalizer

_T = "2026-11-27T10:50:00Z"
STATION = ("0-20000-0-06260", 5.1797, 52.0989)  # De Bilt


def _collection(**values: float) -> dict[str, Any]:
    """An /area CoverageCollection with one station's ``values`` (parameter code → value) at ``_T``."""
    nd = {"type": "NdArray", "dataType": "float", "axisNames": ["t", "y", "x"], "shape": [1, 1, 1]}
    sid, lon, lat = STATION
    return {
        "type": "CoverageCollection",
        "coverages": [
            {
                "type": "Coverage",
                EDR_LOCATION_ID: sid,
                "domain": {"axes": {"x": {"values": [lon]}, "y": {"values": [lat]}, "t": {"values": [_T]}}},
                "ranges": {code: {**nd, "values": [value]} for code, value in values.items()},
            }
        ],
    }


def _by_property(**values: float) -> dict[str, tuple[float, str]]:
    _, readings = parse_edr_locations(_collection(**values))
    return {r.observed_property: (round(r.value, 3), r.unit) for r in readings}


def test_qg_is_solar_radiation_and_rg_is_rain_rate():
    """protects=#48: global solar radiation comes from qg (W/m2) and the rain gauge's intensity rg goes to rain_rate;
    fails_when=rg is read as solar radiation again (rain intensity published as W/m2);
    no_claim=the value ranges KNMI serves."""
    assert _by_property(qg=152.0, rg=1.2) == {
        "solar_radiation": (152.0, "W/m2"),
        "rain_rate": (1.2, "mm/h"),
    }


def test_rain_intensity_alone_gives_no_solar_radiation():
    """protects=a rainy night publishes rain rate, not 'solar radiation';
    fails_when=any mapping sends rg to solar_radiation;
    no_claim=how the history is repaired (#48, a separate script)."""
    assert "solar_radiation" not in _by_property(rg=3.4)


def test_gust_and_hourly_rainfall_use_the_dataset_codes():
    """protects=wind gust from gff (m/s to km/h) and past-hour rainfall from R1H (mm);
    fails_when=the codes are fxx / r1h, which the dataset does not have (both datastreams stayed empty);
    no_claim=fx, the 19-minute gust KNMI also serves."""
    assert _by_property(gff=10.0, R1H=0.4) == {
        "wind_gust": (36.0, "km/h"),
        "precipitation": (0.4, "mm"),
    }
    assert _by_property(fxx=10.0, r1h=0.4) == {}


def test_the_request_asks_for_every_mapped_code_and_only_those():
    """protects=the EDR parameter-name list is the parser's map, so a mapped code is always requested;
    fails_when=the request list and the map drift apart again (gust and rainfall were mapped, never requested);
    no_claim=that the EDR API accepts each name (checked against the live API before deploying)."""
    assert EDR_PARAMETER_NAMES.split(",") == list(_KNMI_EDR_PARAM_MAP)
    assert set(_KNMI_EDR_PARAM_MAP) == {"ta", "tg", "ff", "dd", "gff", "pp", "rh", "vv", "qg", "rg", "R1H"}


def test_every_mapped_code_has_a_datastream_in_the_entity_set():
    """protects=each property the parser produces is registered for each station (rain_rate is new);
    fails_when=a mapped field resolves to a property the entity set lacks (its readings get no datastream);
    no_claim=the datastream names FROST gives them."""
    produced = {
        KNMIWeatherNormalizer.NAME_TRANSFORM[field].value for field, _ in _KNMI_EDR_PARAM_MAP.values()
    }
    entity_set = build_entity_set(STATION[0], "KNMI-52.10-5.18", STATION[2], STATION[1])

    assert produced <= set(entity_set["sensors"][0]["observed_properties"])
    assert produced <= set(_DESCRIPTIONS)


class _EdrResponse:
    def __init__(self, body: Any) -> None:
        self._body = body
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._body


def test_the_source_sends_the_parameter_list(monkeypatch):
    """protects=the /area request carries EDR_PARAMETER_NAMES;
    fails_when=the source keeps a hard-coded list of its own;
    no_claim=the rest of the URL (tests/test_knmi_station_ids.py)."""
    urls: list[str] = []

    def fake_send(self: requests.Session, prepared: Any, **kwargs: Any) -> _EdrResponse:
        urls.append(prepared.url)
        return _EdrResponse(_collection(ta=10.0))

    monkeypatch.setattr(requests.Session, "send", fake_send)
    monkeypatch.setattr(
        knmi_source_module,
        "settings",
        dataclasses.replace(
            knmi_source_module.settings,
            knmi_weather_enabled=True,
            knmi_api_key="test-key",
            knmi_weather_api_url="https://edr.test/edr/v1/collections/obs",
        ),
    )
    source = KNMIWeatherPollingSource()

    async def no_locations(url: str, params: Any = None) -> Any:
        return {"type": "FeatureCollection", "features": []}

    source._client.get = no_locations  # type: ignore[method-assign]

    asyncio.run(source._fetch_edr())

    (url,) = urls
    assert f"&parameter-name={EDR_PARAMETER_NAMES}&" in url


@pytest.mark.parametrize("code", ["fxx", "r1h"])
def test_codes_the_dataset_does_not_have_are_not_requested(code):
    """protects=no request asks for a code missing from KNMI's variable table;
    fails_when=fxx or r1h come back into the map;
    no_claim=codes KNMI may add later."""
    assert code not in EDR_PARAMETER_NAMES.split(",")
