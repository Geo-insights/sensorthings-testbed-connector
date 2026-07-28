from __future__ import annotations

import logging
from typing import Any

import requests as _requests
from confluent_kafka import Consumer, KafkaError, KafkaException
from confluent_kafka.error import SerializationError
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroDeserializer
from confluent_kafka.serialization import MessageField, SerializationContext

from app.config import settings

logger = logging.getLogger(__name__)


class KafkaTGVConsumer:
    """
    Pull-on-demand Kafka consumer for the TGV officelab-climate topic.

    Connects to Confluent Cloud via SASL_SSL, fetches the Avro schema from the
    Schema Registry on connect, and deserializes messages using AvroDeserializer.

    Intended for use as a context manager or via explicit connect()/close() calls.
    One consumer instance per HTTP request; not a long-running background thread.

    Usage::

        with KafkaTGVConsumer() as consumer:
            records = consumer.consume_batch(max_messages=500, timeout=5.0)
    """

    def __init__(self) -> None:
        self._consumer: Consumer | None = None
        self._deserializer: AvroDeserializer | None = None
        self._ser_context: SerializationContext | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """
        Validate settings, fetch the Avro schema from Schema Registry,
        create the Kafka Consumer, and subscribe to the configured topic.

        Raises:
            RuntimeError: If any required setting is missing.
            requests.HTTPError: If the schema registry request fails.
            confluent_kafka.KafkaException: If consumer creation fails.
        """
        self._validate_settings()

        sr_client = SchemaRegistryClient({
            "url": settings.kafka_tgv_schema_registry_url,
            "basic.auth.user.info": (
                f"{settings.kafka_tgv_schema_registry_username}:"
                f"{settings.kafka_tgv_schema_registry_password}"
            ),
        })

        schema_str = self._fetch_schema_str()
        self._deserializer = AvroDeserializer(sr_client, schema_str)
        self._ser_context = SerializationContext(settings.kafka_tgv_topic, MessageField.VALUE)

        self._consumer = Consumer({
            "group.id": settings.kafka_tgv_consumer_group,
            "client.id": settings.kafka_tgv_client_id,
            "bootstrap.servers": settings.kafka_tgv_bootstrap_servers,
            "sasl.mechanisms": "PLAIN",
            "security.protocol": "SASL_SSL",
            "sasl.username": settings.kafka_tgv_api_key,
            "sasl.password": settings.kafka_tgv_api_password,
            "auto.offset.reset": "latest",
            "enable.auto.commit": "false",
        })
        self._consumer.subscribe(
            [settings.kafka_tgv_topic],
            on_assign=self._on_assign,
        )
        logger.info("KafkaTGVConsumer connected to topic %s", settings.kafka_tgv_topic)

    def close(self) -> None:
        """Close the consumer and commit final offsets."""
        if self._consumer is not None:
            try:
                self._consumer.close()
            except Exception as exc:
                logger.warning("Error closing Kafka consumer: %s", exc)
            finally:
                self._consumer = None

    def __enter__(self) -> "KafkaTGVConsumer":
        self.connect()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Core poll
    # ------------------------------------------------------------------

    def consume_batch(
        self,
        max_messages: int = 1000,
        timeout: float = 5.0,
    ) -> list[dict[str, Any]]:
        """
        Poll up to max_messages from Kafka within timeout seconds.

        Returns:
            List of deserialized record dicts (GreenVillageRecord structure).

        Raises:
            RuntimeError: If connect() has not been called.
            confluent_kafka.KafkaException: On message-level Kafka errors.

        Messages that fail Avro deserialization are logged and skipped.
        Offsets are NOT committed — call :meth:`commit` explicitly after
        the batch has been fully processed (pushed to FROST / dead-lettered).
        """
        if self._consumer is None:
            raise RuntimeError("KafkaTGVConsumer.connect() must be called before consume_batch()")

        try:
            msgs = self._consumer.consume(num_messages=max_messages, timeout=timeout)
        except KafkaError as exc:
            raise KafkaException(exc) from exc

        values: list[dict[str, Any]] = []
        for msg in msgs:
            if msg.error():
                raise KafkaException(msg.error())
            try:
                value = self._deserializer(msg.value(), self._ser_context)
                if value is not None:
                    values.append(value)
            except SerializationError as exc:
                logger.warning(
                    "Deserialization error on topic=%s partition=%s offset=%s — skipping: %s",
                    msg.topic(), msg.partition(), msg.offset(), exc,
                )

        logger.debug("Consumed batch of %d records (not yet committed)", len(values))
        return values

    def commit(self) -> None:
        """Commit current offsets to Kafka. Call after the batch has been fully processed."""
        if self._consumer is None:
            raise RuntimeError("KafkaTGVConsumer.connect() must be called before commit()")
        self._consumer.commit()
        logger.debug("Kafka offsets committed")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _validate_settings(self) -> None:
        required = [
            ("KAFKA_TGV_BOOTSTRAP_SERVERS", settings.kafka_tgv_bootstrap_servers),
            ("KAFKA_TGV_API_KEY", settings.kafka_tgv_api_key),
            ("KAFKA_TGV_API_PASSWORD", settings.kafka_tgv_api_password),
            ("KAFKA_TGV_SCHEMA_REGISTRY_URL", settings.kafka_tgv_schema_registry_url),
            ("KAFKA_TGV_SCHEMA_REGISTRY_USERNAME", settings.kafka_tgv_schema_registry_username),
            ("KAFKA_TGV_SCHEMA_REGISTRY_PASSWORD", settings.kafka_tgv_schema_registry_password),
            ("KAFKA_TGV_TOPIC", settings.kafka_tgv_topic),
        ]
        missing = [name for name, value in required if not value]
        if missing:
            raise RuntimeError(f"Missing required Kafka settings: {', '.join(missing)}")

    def _fetch_schema_str(self) -> str:
        """
        Fetch the latest Avro schema string from Schema Registry.
        Subject convention: <topic>-value (Confluent default).
        """
        url = (
            f"{settings.kafka_tgv_schema_registry_url}/subjects/"
            f"{settings.kafka_tgv_topic}-value/versions/latest"
        )
        response = _requests.get(
            url,
            auth=(
                settings.kafka_tgv_schema_registry_username,
                settings.kafka_tgv_schema_registry_password,
            ),
            timeout=15.0,
        )
        response.raise_for_status()
        return response.json()["schema"]

    @staticmethod
    def _on_assign(consumer: Consumer, partitions: list) -> None:
        # Build partition summary without calling repr() on TopicPartition objects
        # to avoid a Python 3.14 SystemError in confluent-kafka's __repr__ format string.
        try:
            summary = ", ".join(
                f"{getattr(p, 'topic', '?')}[{getattr(p, 'partition', '?')}]"
                for p in partitions
            )
        except Exception:
            summary = f"<{len(partitions)} partition(s)>"
        logger.debug("Kafka partition assignment: %s", summary)
