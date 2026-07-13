import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.routes.connector import router as connector_router
from app.routes.health import router as health_router
from app.services.sensorthings_client import client

logger = logging.getLogger("connector.kafka")


def _kafka_push_cycle(max_messages: int = 500, timeout: float = 5.0) -> dict:
    """Run one Kafka consume → FROST push cycle (blocking)."""
    from app.services.kafka_tgv_consumer import KafkaTGVConsumer
    from app.sources.tgv_kafka_mapping import avro_batch_to_sensor_readings

    with KafkaTGVConsumer() as consumer:
        records = consumer.consume_batch(max_messages=max_messages, timeout=timeout)
    if not records:
        return {"pulled": 0}
    readings = avro_batch_to_sensor_readings(records)
    result = client.push_observations(readings)
    return {"pulled": len(records), "mapped": len(readings), "pushed": result.get("total_sent", 0)}


async def _kafka_ingest_loop():
    """Background loop: consume Kafka and push to FROST every KAFKA_TGV_POLL_SECONDS."""
    poll_seconds = max(10, int(settings.kafka_tgv_poll_seconds))
    logger.info("Kafka ingest loop started (every %ds)", poll_seconds)
    while True:
        try:
            summary = await run_in_threadpool(_kafka_push_cycle)
            if summary.get("pulled", 0) > 0:
                logger.info(
                    "Kafka push: pulled=%s mapped=%s pushed=%s",
                    summary.get("pulled"), summary.get("mapped"), summary.get("pushed"),
                )
        except Exception:
            logger.exception("Kafka ingest cycle failed")
        await asyncio.sleep(poll_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = []
    if settings.kafka_tgv_enabled:
        tasks.append(asyncio.create_task(_kafka_ingest_loop()))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(
    title=settings.connector_name,
    version="0.1.0",
    description="Climate-adaptation proof of concept for connecting TGV and Blijdorp sensors to a central OGC SensorThings API server.",
    lifespan=lifespan,
)

SENSOR_IMAGES_DIR = Path("data/sensor_images")
SENSOR_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static/sensor-images", StaticFiles(directory=str(SENSOR_IMAGES_DIR)), name="sensor-images")

app.include_router(health_router)
app.include_router(connector_router)


@app.get("/frost/status")
def frost_status() -> dict:
    return client.check_frost_status()


@app.get("/frost/capabilities")
def frost_capabilities() -> dict:
    return client.check_capabilities()
