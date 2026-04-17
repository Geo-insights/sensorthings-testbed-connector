from __future__ import annotations

from datetime import UTC, datetime

from app.models import SensorReading


def generate_demo_readings() -> list[SensorReading]:
    now = datetime.now(UTC)
    thing_name = "Brabantse Delta Wastewater Demo"
    return [
        SensorReading(
            sensor_id="pressure-inlet-01",
            sensor_name="Inlet Pressure Sensor",
            observed_property="pressure",
            unit="bar",
            value=1.82,
            timestamp=now,
            thing_name=thing_name,
        ),
        SensorReading(
            sensor_id="pressure-outlet-01",
            sensor_name="Outlet Pressure Sensor",
            observed_property="pressure",
            unit="bar",
            value=1.37,
            timestamp=now,
            thing_name=thing_name,
        ),
        SensorReading(
            sensor_id="flow-main-01",
            sensor_name="Main Flow Sensor",
            observed_property="flow",
            unit="m3/h",
            value=24.6,
            timestamp=now,
            thing_name=thing_name,
        ),
        SensorReading(
            sensor_id="pump-rpm-01",
            sensor_name="Pump RPM Sensor",
            observed_property="pump_speed",
            unit="rpm",
            value=1450,
            timestamp=now,
            thing_name=thing_name,
        ),
    ]
