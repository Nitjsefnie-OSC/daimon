"""Run apps/harness on loopback with its real OpenAI Agents SDK loop, in its own process.

Launched by ``test_harness_conformance.py`` so the restart case can SIGKILL a real process
and start another on the same home. Prints ``PORT=<n>`` once it is listening.

Coupling to apps/harness is confined to ``real_loop_adapter`` below.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
from pathlib import Path

import uvicorn
from daimon_harness.model_adapter import AzureModelAdapter, ModelAdapter
from daimon_harness.runtime import TurnContext
from daimon_harness.service import create_app
from pydantic import JsonValue


def real_loop_adapter(fake_model_url: str) -> ModelAdapter:
    """The harness's real model loop (AzureModelAdapter.run), aimed at the fake model.

    Why this shape: test mode refuses an ``AzureModelAdapter`` instance and any
    ``AZURE_OPENAI_API_KEY`` in the environment, and the production adapter reads its endpoint
    from the environment. So the instance is built while two placeholder variables point at the
    fake server, the variables are removed again, and a plain wrapper object (not an
    ``AzureModelAdapter``) delegates ``run`` to it. This is the same trick apps/harness's own
    interop tests use. If the adapter's constructor, its environment variable names or the
    ``run`` signature change (see the ``model_adapter`` reshape in progress), only this
    function needs updating.
    """
    os.environ["AZURE_OPENAI_ENDPOINT"] = fake_model_url
    os.environ["AZURE_OPENAI_API_KEY"] = "offline-placeholder"
    try:
        sdk = AzureModelAdapter()
    finally:
        del os.environ["AZURE_OPENAI_ENDPOINT"], os.environ["AZURE_OPENAI_API_KEY"]

    class RealLoop:
        async def run(self, context: TurnContext, history: list[JsonValue]) -> list[JsonValue]:
            return await sdk.run(context, history)

    return RealLoop()


async def serve(home: Path, fake_model_url: str, token: str) -> None:
    app = create_app(home=home, token=token, adapter=real_loop_adapter(fake_model_url))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    while not server.started:
        if task.done():
            await task
        await asyncio.sleep(0.01)
    print(f"PORT={listener.getsockname()[1]}", flush=True)
    await task


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--fake-model-url", required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    asyncio.run(serve(args.home, args.fake_model_url, args.token))


if __name__ == "__main__":
    main()
