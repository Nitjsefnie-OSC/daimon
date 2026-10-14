# Mux library

`packages/mux` provides provider-neutral contracts, state operations, profiles and optional Anthropic, OpenAI and Gemini drivers. Installing this library does not change the existing host runtime or enable backend selection.

The wheel contains only `mux` runtime modules. Offline conformance helpers, SDK fakes, budget receipts and recording support belong to `packages/testing`; tests and fixtures stay outside the runtime wheel. Core and adapters retain their current implementation in this library cut.

Install the declared development group and run the library gates:

```sh
uv sync --all-packages --all-extras --group mux-test
uv run pyright packages/mux/mux
uv run lint-imports
uv run pytest packages/mux packages/testing/tests/conformance packages/testing/tests/test_distribution_boundaries.py packages/testing/tests/test_product_import_boundary.py -n 2
uv run pytest tests/parity/test_ma_call_ratchet.py
uv build --wheel --package daimon-mux
```

The ratchet preserves the existing host call ceiling. It falls when host ports move into the drivers. Existing runtime defaults and golden records remain unchanged. Driver replay tests also consume unchanged reviewed JSON recordings; an offline replay is not a live-provider result.

Host integration tests that require neutral resource stores, Gemini host storage, the turn bridge or the fleet-aware outcome oracle ship with those host components. Their original test sources remain in the integration branch; the library cut preserves their exact contents for the later host slice.
