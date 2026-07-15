"""Component registry mapping source types to their processing pipelines."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.pipeline.base import Decoder, Decapsulator, Deserializer, Normalizer, Parser


@dataclass(frozen=True)
class PipelineComponents:
    """Bundle of processing components for a data source."""

    decoder: Decoder | None = None
    deserializer: Deserializer | None = None
    decapsulator: Decapsulator | None = None
    parser: Parser | None = None
    normalizer: Normalizer | None = None


class ComponentRegistry:
    """Maps source type strings to their pipeline components."""

    def __init__(self) -> None:
        self._registry: dict[str, PipelineComponents] = {}

    def register(self, source_type: str, components: PipelineComponents) -> None:
        self._registry[source_type] = components

    def get(self, source_type: str) -> PipelineComponents | None:
        return self._registry.get(source_type)

    def available_sources(self) -> list[str]:
        return list(self._registry.keys())


# Module-level singleton
registry = ComponentRegistry()
