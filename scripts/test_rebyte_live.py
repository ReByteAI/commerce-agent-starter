"""Exercise the retail HTTP host with a real Rebyte Agent and isolated test resources."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import load_dotenv
from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))


async def main(model: str) -> None:
    load_dotenv(ROOT / ".env", override=False)
    base = os.getenv("REBYTE_BASE_URL", "https://api.rebyte.ai/v1").rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    from retail.api.rebyte_config import agent_parameters, session_environment

    # Identical checked-in inputs produce identical Skill ZIP bytes.
    assert session_environment() == session_environment()
    async with AsyncOpenAI(
        api_key=os.environ["REBYTE_API_KEY"], base_url=base, max_retries=0
    ) as client:
        config = agent_parameters(model)
        config["name"] = f"Commerce live test {uuid4()}"
        saved = await client.beta.agents.create(**config)
        os.environ["REBYTE_AGENT_ID"] = saved.id
        sessions = []
        try:
            from commerce_common.memory import InMemoryMemoryStore
            from retail.api.main import agent, app

            # No writes to the developer's persistent shopper-memory file.
            agent.memory = replace(agent.memory, store=InMemoryMemoryStore())
            agent._client = client
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as host:
                response = await host.post("/api/session", json={"user_id": "demo-user"})
                response.raise_for_status()
                browser_id = response.json()["session_id"]
                headers = {"X-Session-Id": browser_id}

                async def prompt(text: str) -> list[dict]:
                    async with asyncio.timeout(600):
                        response = await host.post(
                            "/api/chat", json={"message": text}, headers=headers
                        )
                    response.raise_for_status()
                    events = []
                    for frame in response.text.split("\n\n"):
                        kind = next(
                            (line[7:] for line in frame.splitlines() if line.startswith("event: ")),
                            None,
                        )
                        data = next(
                            (line[6:] for line in frame.splitlines() if line.startswith("data: ")),
                            None,
                        )
                        if kind and data:
                            events.append({"type": kind, "data": json.loads(data)})
                    errors = [event for event in events if event["type"] == "error"]
                    assert not errors, errors
                    assert any(event["type"] == "turn_complete" for event in events), events
                    print(
                        json.dumps(
                            {
                                "prompt": text,
                                "events": len(events),
                                "tools": [
                                    event["data"]["tool"]
                                    for event in events
                                    if event["type"] == "tool_call"
                                ],
                            }
                        ),
                        flush=True,
                    )
                    return events

                found = await prompt(
                    "Read your installed search-discovery SKILL.md before searching. "
                    "Find family camping tents under $250 in the catalog and show product cards."
                )
                cards = [
                    e for e in found if e["type"] == "ui" and e["data"]["component"] == "products"
                ]
                assert cards, "No product cards"
                # AR-1201 is a checked-in catalog fixture; the model must obtain provenance first.
                await prompt(
                    "Find catalog product AR-1201 and add exactly one to my cart. "
                    "I confirm this item and quantity; use the catalog tools as needed."
                )
                cart_response = await host.get("/api/cart", headers=headers)
                cart_response.raise_for_status()
                cart = cart_response.json()
                assert cart["items"] and sum(item["quantity"] for item in cart["items"]) == 1, cart
                checkout = await prompt("Check out my cart and show the checkout summary.")
                assert any(
                    e["type"] == "ui" and e["data"]["component"] == "checkout" for e in checkout
                )
                native_id = agent._sessions[browser_id]
                native = await client.beta.agents.sessions.retrieve(native_id)
                assert native.status == "idle"
                assert len(native.environment.skills) == 5
                items = [item async for item in client.beta.agents.sessions.items.list(native_id)]
                assert any(item.type == "command_execution" for item in items), (
                    "Skill was not read in Sandbox"
                )
                assert any(item.type == "function_call_output" for item in items), (
                    "No persisted host outputs"
                )
                print(
                    json.dumps(
                        {
                            "passed": True,
                            "agent": saved.id,
                            "session": native_id,
                            "search": True,
                            "cart": True,
                            "checkout": True,
                            "skills": 5,
                        }
                    ),
                    flush=True,
                )
        finally:
            # Find only resources belonging to this run's uniquely created Agent, even on failure.
            async for candidate in client.beta.agents.sessions.list():
                if candidate.agent.id == saved.id:
                    sessions.append(candidate.id)
            for session_id in sessions:
                await client.beta.agents.sessions.delete(session_id)
            await client.beta.agents.delete(saved.id)
            print(json.dumps({"cleanup": True, "sessions_deleted": len(sessions)}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="gpt-5.6-luna")
    asyncio.run(main(parser.parse_args().model))
