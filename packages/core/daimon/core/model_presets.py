"""Local model choices; preset keys never go to the provider as model IDs."""

from collections.abc import Mapping
from typing import Literal, Required, cast

from typing_extensions import TypedDict


class ModelConfig(TypedDict, total=False):
    id: Required[str]
    speed: Literal["standard", "fast"] | None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | dict[str, str] | None
    inference_geo: Literal["us", "global"] | None


type ModelSelection = str | ModelConfig


OPUS_FAST = "claude-opus-5-5-fast"
OPUS = "claude-opus-5-5"


def resolve_model(model: ModelSelection) -> ModelSelection:
    """Expand the local fast choice, preserving authored effort and geography."""
    if isinstance(model, str):
        return {"id": OPUS, "speed": "fast"} if model == OPUS_FAST else model
    if model["id"] == OPUS_FAST:
        return {**model, "id": OPUS, "speed": "fast"}
    return model


def model_choice_id(model: object) -> str:
    """Selection/tariff key for a native SDK model or authoring value."""
    if isinstance(model, str):
        return model
    if isinstance(model, Mapping):
        config = cast(Mapping[str, object], model)
        model_id, speed = config["id"], config.get("speed")
    else:
        model_id, speed = getattr(model, "id", None), getattr(model, "speed", None)
    if model_id == OPUS and speed == "fast":
        return OPUS_FAST
    return str(model_id)
