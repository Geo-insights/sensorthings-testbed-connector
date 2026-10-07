"""Tests for the KNMI weather station source (#14).

Covers:
- parse_station_measurements() with valid station data produces >=10 readings
- Wind speed conversion: 3.2 m/s -> 11.52 km/h (3.2 * 3.6)
- Wind gust conversion: same m/s -> km/h pattern
- All mapped parameters produce correct canonical names and units
- Unknown/None fields are skipped
- build_entity_set() produces valid entity set
- KNMIWeatherPollingSource is a RestPollingSource subclass with uses_dynamic_discovery = True
- KNMIWeatherNormalizer mapping test
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.knmi_weather_source import KNMIWeatherPollingSource
from app.services.rest_polling_source import RestPollingSource
from app.sources.knmi_weather import build_entity_set, parse_station_measurements
from app.sources.normalizers import KNMIWeatherNormalizer
from app.sta.canonical import CanonicalDatastream

_TS = datetime(2026, 10, 7, 14, 0, 0, tzinfo=UTC)

# A full station measurement payload matching the Buienradar feed shape
_FULL_STATION: dict = {
    "stationid": 6260,
    "stationname": "Meetstation De Bilt",
    "lat": 52.10,
    "lon": 5.18,
    "timestamp": "2026-10-07T14:00:00",
    "temperature": 18.5,
    "groundtemperature": 16.2,
    "feeltemperature": 17.1,
    "humidity": 72.0,
    "windspeed": 3.2,
    "winddirection": "ZW",
    "winddirectiondegrees": 225,
    "windgusts": 6.1,
    "airpressure": 1013.5,
    "visibility": 28000,
    "sunpower": 412.0,
    "precipitation": 0.0,
    "rainFallLastHour": 0.5,
    "rainFallLast24Hour": 2.1,
}


# ---------------------------------------------------------------------------
# KNMIWeatherNormalizer — canonical names and units
# ---------------------------------------------------------------------------


class TestKNMIWeatherNormalizer:
    def test_temperature_canonical(self):
        norm = KNMIWeatherNormalizer(temperature=18.5)
        readings = norm.to_readings(
            sensor_id="knmi-6260",
            sensor_name="KNMI De Bilt sensor",
            thing_name="KNMI De Bilt",
            timestamp=_TS,
            location="nl",
        )
        assert len(readings) == 1
        r = readings[0]
        assert r.observed_property == CanonicalDatastream.TEMPERATURE.value
        assert r.unit == "°C"
        assert r.observed_property_name == "Air temperature"
        assert r.value == pytest.approx(18.5)

    def test_ground_temperature_canonical(self):
        norm = KNMIWeatherNormalizer(groundtemperature=16.2)
        readings = norm.to_readings(
            sensor_id="knmi-6260",
            sensor_name="s",
            thing_name="t",
            timestamp=_TS,
        )
        assert len(readings) == 1
        r = readings[0]
        assert r.observed_property == CanonicalDatastream.GROUND_TEMPERATURE.value
        assert r.unit == "°C"

    def test_feel_temperature_canonical(self):
        norm = KNMIWeatherNormalizer(feeltemperature=17.1)
        readings = norm.to_readings(
            sensor_id="knmi-6260",
            sensor_name="s",
            thing_name="t",
            timestamp=_TS,
        )
        assert len(readings) == 1
        assert readings[0].observed_property == CanonicalDatastream.FEEL_TEMPERATURE.value

    def test_humidity_canonical(self):
        norm = KNMIWeatherNormalizer(humidity=72.0)
        readings = norm.to_readings(
            sensor_id="knmi-6260",
            sensor_name="s",
            thing_name="t",
            timestamp=_TS,
        )
        assert len(readings) == 1
        r = readings[0]
        assert r.observed_property == CanonicalDatastream.HUMIDITY.value
        assert r.unit == "%"

    def test_wind_speed_kmh_canonical(self):
        """windspeedkmh field maps to WIND_SPEED with km/h unit."""
        norm = KNMIWeatherNormalizer(windspeedkmh=11.52)
        readings = norm.to_readings(
            sensor_id="knmi-6260",
            sensor_name="s",
            thing_name="t",
            timestamp=_TS,
        )
        assert len(readings) == 1
        r = readings[0]
        assert r.observed_property == CanonicalDatastream.WIND_SPEED.value
        assert r.unit == "km/h"
        assert r.value == pytest.approx(11.52)

    def test_wind_direction_canonical(self):
        norm = KNMIWeatherNormalizer(winddirectiondegrees=225)
        readings = norm.to_readings(
            sensor_id="knmi-6260",
            sensor_name="s",
            thing_name="t",
            timestamp=_TS,
        )
        assert len(readings) == 1
        r = readings[0]
        assert r.observed_property == CanonicalDatastream.WIND_DIRECTION.value
        assert r.unit == "deg"

    def test_wind_gust_kmh_canonical(self):
        """windgustskmh field maps to WIND_GUST with km/h unit."""
        norm = KNMIWeatherNormalizer(windgustskmh=21.96)
        readings = norm.to_readings(
            sensor_id="knmi-6260",
            sensor_name="s",
            thing_name="t",
            timestamp=_TS,
        )
        assert len(readings) == 1
        r = readings[0]
        assert r.observed_property == CanonicalDatastream.WIND_GUST.value
        assert r.unit == "km/h"

    def test_air_pressure_canonical(self):
        norm = KNMIWeatherNormalizer(airpressure=1013.5)
        readings = norm.to_readings(
            sensor_id="knmi-6260",
            sensor_name="s",
            thing_name="t",
            timestamp=_TS,
        )
        assert len(readings) == 1
        r = readings[0]
        assert r.observed_property == CanonicalDatastream.AIR_PRESSURE.value
        assert r.unit == "hPa"

    def test_visibility_canonical(self):
        norm = KNMIWeatherNormalizer(visibility=28000.0)
        readings = norm.to_readings(
            sensor_id="knmi-6260",
            sensor_name="s",
            thing_name="t",
            timestamp=_TS,
        )
        assert len(readings) == 1
        r = readings[0]
        assert r.observed_property == CanonicalDatastream.VISIBILITY.value
        assert r.unit == "m"

    def test_sunpower_maps_to_solar_radiation(self):
        norm = KNMIWeatherNormalizer(sunpower=412.0)
        readings = norm.to_readings(
            sensor_id="knmi-6260",
            sensor_name="s",
            thing_name="t",
            timestamp=_TS,
        )
        assert len(readings) == 1
        r = readings[0]
        assert r.observed_property == CanonicalDatastream.SOLAR_RADIATION.value
        assert r.unit == "W/m2"

    def test_rainfalllasthour_maps_to_precipitation(self):
        norm = KNMIWeatherNormalizer(rainfalllasthour=0.5)
        readings = norm.to_readings(
            sensor_id="knmi-6260",
            sensor_name="s",
            thing_name="t",
            timestamp=_TS,
        )
        assert len(readings) == 1
        r = readings[0]
        assert r.observed_property == CanonicalDatastream.PRECIPITATION.value
        assert r.unit == "mm"

    def test_all_fields_none_produces_empty_readings(self):
        norm = KNMIWeatherNormalizer(
            temperature=None,
            groundtemperature=None,
            feeltemperature=None,
            humidity=None,
            windspeedkmh=None,
            winddirectiondegrees=None,
            windgustskmh=None,
            airpressure=None,
            visibility=None,
            sunpower=None,
            rainfalllasthour=None,
        )
        readings = norm.to_readings(
            sensor_id="knmi-6260",
            sensor_name="s",
            thing_name="t",
            timestamp=_TS,
        )
        assert readings == []

    def test_partial_none_skips_only_null_fields(self):
        norm = KNMIWeatherNormalizer(temperature=20.0, humidity=None, airpressure=1015.0)
        readings = norm.to_readings(
            sensor_id="knmi-6260",
            sensor_name="s",
            thing_name="t",
            timestamp=_TS,
        )
        assert len(readings) == 2
        props = {r.observed_property for r in readings}
        assert "temperature" in props
        assert "air_pressure" in props
        assert "humidity" not in props

    def test_extra_fields_ignored(self):
        """Normalizer silently drops unknown vendor fields (governance rule)."""
        norm = KNMIWeatherNormalizer.model_validate({
            "temperature": 18.5,
            "winddirection": "ZW",    # text direction — not mapped
            "precipitation": 0.0,     # cumulative field — not mapped
            "rainFallLast24Hour": 2.1,  # not mapped
        })
        readings = norm.to_readings(
            sensor_id="knmi-6260",
            sensor_name="s",
            thing_name="t",
            timestamp=_TS,
        )
        assert len(readings) == 1
        assert readings[0].observed_property == "temperature"


# ---------------------------------------------------------------------------
# parse_station_measurements — parsing and unit conversion
# ---------------------------------------------------------------------------


class TestParseStationMeasurements:
    def test_full_station_produces_at_least_10_readings(self):
        readings = parse_station_measurements([_FULL_STATION])
        assert len(readings) >= 10

    def test_wind_speed_conversion_ms_to_kmh(self):
        """3.2 m/s -> 11.52 km/h (3.2 * 3.6)."""
        readings = parse_station_measurements([_FULL_STATION])
        wind_readings = [r for r in readings if r.observed_property == "wind_speed"]
        assert len(wind_readings) == 1
        assert wind_readings[0].value == pytest.approx(3.2 * 3.6)
        assert wind_readings[0].unit == "km/h"

    def test_wind_gust_conversion_ms_to_kmh(self):
        """6.1 m/s -> 21.96 km/h (6.1 * 3.6)."""
        readings = parse_station_measurements([_FULL_STATION])
        gust_readings = [r for r in readings if r.observed_property == "wind_gust"]
        assert len(gust_readings) == 1
        assert gust_readings[0].value == pytest.approx(6.1 * 3.6)
        assert gust_readings[0].unit == "km/h"

    def test_temperature_value_passed_through(self):
        readings = parse_station_measurements([_FULL_STATION])
        temp = [r for r in readings if r.observed_property == "temperature"]
        assert len(temp) == 1
        assert temp[0].value == pytest.approx(18.5)
        assert temp[0].unit == "°C"

    def test_humidity_value_passed_through(self):
        readings = parse_station_measurements([_FULL_STATION])
        hum = [r for r in readings if r.observed_property == "humidity"]
        assert len(hum) == 1
        assert hum[0].value == pytest.approx(72.0)

    def test_air_pressure_value(self):
        readings = parse_station_measurements([_FULL_STATION])
        pres = [r for r in readings if r.observed_property == "air_pressure"]
        assert len(pres) == 1
        assert pres[0].value == pytest.approx(1013.5)
        assert pres[0].unit == "hPa"

    def test_visibility_value(self):
        readings = parse_station_measurements([_FULL_STATION])
        vis = [r for r in readings if r.observed_property == "visibility"]
        assert len(vis) == 1
        assert vis[0].value == pytest.approx(28000.0)
        assert vis[0].unit == "m"

    def test_solar_radiation_from_sunpower(self):
        readings = parse_station_measurements([_FULL_STATION])
        rad = [r for r in readings if r.observed_property == "solar_radiation"]
        assert len(rad) == 1
        assert rad[0].value == pytest.approx(412.0)
        assert rad[0].unit == "W/m2"

    def test_precipitation_uses_rainfall_last_hour(self):
        readings = parse_station_measurements([_FULL_STATION])
        precip = [r for r in readings if r.observed_property == "precipitation"]
        assert len(precip) == 1
        assert precip[0].value == pytest.approx(0.5)  # rainFallLastHour
        assert precip[0].unit == "mm"

    def test_sensor_id_prefixed_with_knmi(self):
        readings = parse_station_measurements([_FULL_STATION])
        assert all(r.sensor_id == "knmi-6260" for r in readings)

    def test_thing_name_includes_station_name(self):
        readings = parse_station_measurements([_FULL_STATION])
        assert all("De Bilt" in r.thing_name for r in readings)

    def test_missing_wind_speed_skipped(self):
        station = {**_FULL_STATION, "windspeed": None, "windgusts": None}
        readings = parse_station_measurements([station])
        wind_props = {r.observed_property for r in readings}
        assert "wind_speed" not in wind_props
        assert "wind_gust" not in wind_props

    def test_missing_temperature_skipped(self):
        station = {**_FULL_STATION, "temperature": None}
        readings = parse_station_measurements([station])
        temp = [r for r in readings if r.observed_property == "temperature"]
        assert temp == []

    def test_empty_list_returns_empty(self):
        assert parse_station_measurements([]) == []

    def test_non_dict_entries_skipped(self):
        readings = parse_station_measurements(["not_a_dict", None, 42])  # type: ignore[list-item]
        assert readings == []

    def test_station_without_id_skipped(self):
        station = {**_FULL_STATION}
        del station["stationid"]
        readings = parse_station_measurements([station])
        assert readings == []

    def test_multiple_stations_all_parsed(self):
        station2 = {**_FULL_STATION, "stationid": 6380, "stationname": "Meetstation Maastricht"}
        readings = parse_station_measurements([_FULL_STATION, station2])
        sensor_ids = {r.sensor_id for r in readings}
        assert "knmi-6260" in sensor_ids
        assert "knmi-6380" in sensor_ids

    def test_timestamp_parsed_from_payload(self):
        readings = parse_station_measurements([_FULL_STATION])
        assert readings
        ts = readings[0].timestamp
        assert ts.year == 2026
        assert ts.month == 10
        assert ts.day == 7
        assert ts.hour == 14

    def test_location_is_nl(self):
        readings = parse_station_measurements([_FULL_STATION])
        assert all(r.location == "nl" for r in readings)


# ---------------------------------------------------------------------------
# build_entity_set
# ---------------------------------------------------------------------------


class TestBuildEntitySet:
    def test_returns_required_keys(self):
        entity_set = build_entity_set(6260, "Meetstation De Bilt", 52.10, 5.18)
        for key in ("site_key", "site_name", "thing", "location", "sensors", "observed_properties"):
            assert key in entity_set

    def test_site_key_is_nl(self):
        entity_set = build_entity_set(6260, "De Bilt", 52.10, 5.18)
        assert entity_set["site_key"] == "nl"

    def test_thing_name_format(self):
        entity_set = build_entity_set(6260, "Meetstation De Bilt", 52.10, 5.18)
        assert entity_set["thing"]["name"] == "KNMI Meetstation De Bilt"

    def test_source_property_is_knmi_weather(self):
        entity_set = build_entity_set(6260, "De Bilt", 52.10, 5.18)
        assert entity_set["thing"]["properties"]["source"] == "knmi_weather"

    def test_location_coordinates_geojson_order(self):
        """GeoJSON coordinates are [lon, lat]."""
        entity_set = build_entity_set(6260, "De Bilt", 52.10, 5.18)
        coords = entity_set["location"]["location"]["coordinates"]
        assert coords == [5.18, 52.10]

    def test_sensor_id_prefixed_with_knmi(self):
        entity_set = build_entity_set(6260, "De Bilt", 52.10, 5.18)
        sensor = entity_set["sensors"][0]
        assert sensor["sensor_id"] == "knmi-6260"

    def test_string_station_id_accepted(self):
        entity_set = build_entity_set("6260", "De Bilt", 52.10, 5.18)
        assert entity_set["sensors"][0]["sensor_id"] == "knmi-6260"

    def test_knmi_station_id_in_properties(self):
        entity_set = build_entity_set(6380, "Maastricht", 50.91, 5.76)
        assert entity_set["thing"]["properties"]["knmi_station_id"] == "6380"

    def test_observed_properties_are_canonical(self):
        from app.sta.canonical import resolve

        entity_set = build_entity_set(6260, "De Bilt", 52.10, 5.18)
        for key in entity_set["observed_properties"]:
            assert resolve(key) is not None, f"observed_property {key!r} is not canonical"


# ---------------------------------------------------------------------------
# KNMIWeatherPollingSource
# ---------------------------------------------------------------------------


class TestKNMIWeatherPollingSource:
    def test_is_rest_polling_source_subclass(self):
        assert issubclass(KNMIWeatherPollingSource, RestPollingSource)

    def test_source_name(self):
        source = KNMIWeatherPollingSource()
        assert source.source_name == "knmi_weather"

    def test_uses_dynamic_discovery(self):
        source = KNMIWeatherPollingSource()
        assert source.uses_dynamic_discovery is True

    def test_is_disabled_by_default(self):
        """KNMI_WEATHER_ENABLED defaults to false (env not set in tests)."""
        source = KNMIWeatherPollingSource()
        assert not source.is_enabled()

    def test_poll_interval_at_least_10(self):
        source = KNMIWeatherPollingSource()
        assert source.poll_interval() >= 10

    def test_poll_interval_default(self):
        """Default poll interval is 600s (10 min) per config default."""
        source = KNMIWeatherPollingSource()
        assert source.poll_interval() == 600

    def test_entity_sets_empty_before_discovery(self):
        source = KNMIWeatherPollingSource()
        assert source.entity_sets() == []

    def test_entity_sets_populated_after_discovery(self):
        source = KNMIWeatherPollingSource()
        source._discovered_stations["6260"] = {
            "name": "Meetstation De Bilt",
            "lat": 52.10,
            "lon": 5.18,
        }
        source._discovered_stations["6380"] = {
            "name": "Meetstation Maastricht",
            "lat": 50.91,
            "lon": 5.76,
        }
        sets = source.entity_sets()
        assert len(sets) == 2
        names = {s["thing"]["name"] for s in sets}
        assert "KNMI Meetstation De Bilt" in names
        assert "KNMI Meetstation Maastricht" in names
