"""Data transformation pipeline ABCs and registry."""

from app.pipeline.bridge import BridgeReadingParser, JsonDecoder
from app.pipeline.kafka_tgv import (
    AvroUnionDeserializer,
    TGVDecapsulator,
    TGVMeasurementParser,
)
from app.pipeline.registry import PipelineComponents, registry

# Register built-in source pipelines
registry.register(
    "kafka_tgv",
    PipelineComponents(
        deserializer=AvroUnionDeserializer(),
        decapsulator=TGVDecapsulator(),
    ),
)

registry.register(
    "bridge",
    PipelineComponents(
        decoder=JsonDecoder(),
        parser=BridgeReadingParser(),
    ),
)
