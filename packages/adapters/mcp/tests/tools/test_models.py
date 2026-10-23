"""list_models answers "what models can I use / what does luna cost?" (friction #30)."""

from daimon.adapters.mcp.tools.models import (
    _list_models_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.agent_model_names import resolve_model
from mux.contracts.ids import ModelRef


def test_list_models_names_every_catalog_model_with_its_price_and_every_name_resolves():
    listed = _list_models_impl()
    by_id = {row["id"]: row for row in listed if isinstance(row, dict)}
    assert by_id["gpt-6-luna"]["name"] == "GPT-6 Luna" and "$" in str(by_id["gpt-6-luna"]["price"])
    for model_id in ("claude-opus-5-5", "claude-opus-5-5-fast", "gpt-6-astra", "gemini-4-argon"):
        assert model_id in by_id
    for row in by_id.values():
        model = ModelRef(provider=row["provider"], id=row["id"])  # type: ignore[arg-type]
        assert resolve_model(str(row["name"])) == model
