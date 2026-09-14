# Rebyte Agents API integration

The retail storefront uses one pre-created API Agent, twenty original Commerce
client functions, explicit Web Search, and five per-Session Skills. The official
OpenAI Python SDK 3.13.0 connects to the configured Rebyte `/v1` endpoint.

## Setup

Run `python scripts/setup_rebyte_agent.py --validate`, then
`python scripts/setup_rebyte_agent.py --model gpt-5.6-luna`. Save the returned `agent_...` ID as
`REBYTE_AGENT_ID`. `--model` selects a model; `--agent-id` updates an existing
API Agent. The script reads `REBYTE_API_KEY` and `REBYTE_BASE_URL` from the
server environment or root `.env`. See the [setup guide](../README.md#get-started).

`agent.toml` retains the original tool contracts and local Skill paths.
`examples/retail/api/rebyte_config.py` converts its client tools to Agents API
function definitions and packages the checked-in Skills as inline ZIPs.
This is a Commerce-owned manifest, not the Toolkit CLI's native `agent.toml`.
The legacy `strict` flag is absent from the new Agents API function schema.

## Runtime boundary

`examples/retail/api/rebyte_agent.py` creates one Session for each browser
conversation and reuses it on later turns. Each Session has its own hosted
environment configuration and Skills. Its Sandbox is allocated only when an
environment tool is used; Web Search and client functions do not allocate it.
The Agent has no legacy Profile or Workspace binding.

The adapter subscribes to Session events before sending input. Text deltas
retain the storefront's `AgentEvent` protocol. Only `requires_action` authorizes
host execution through the original `ShoppingToolExecutor`, preserving tool
validation, provenance, cart limits, memory and presentation enrichment.
Tool results and a cancellation event are submitted in one atomic batch when
a clean presentation round ends the reply. Rebyte stores the actual tool
results without another model call; the next turn can read them. The host
waits for the Session to become idle before reporting completion.

The demo's browser-to-Session mapping and original storefront state remain
process-local; run one FastAPI worker. Restarting the demo loses those local
conversations. API Sessions and their files remain until explicitly deleted.
This integration does not require a storefront MCP server or an Anthropic key.

## Tool and Skill execution

Saved Agent tools are the twenty Commerce function definitions plus Web Search.
Each browser conversation creates a hosted Environment with five inline Skill ZIPs.
The ZIP bytes are deterministic for identical checked-in files. First Sandbox
initialization installs them; later Turns and resume do not reinstall them.

The model reads installed `SKILL.md` files with `exec_command` and follows their
instructions. Hosted environments also supply `write_stdin`, `apply_patch` and
`view_image`. There is no separate List Skill or Run Skill tool. Catalog, cart,
checkout and UI presentation still execute in Python through function handoffs.
The application must not execute built-in function-call Items itself.

## Verify

```sh
pytest examples/retail/api/tests/test_rebyte_agent.py
python scripts/check.py
python scripts/setup_rebyte_agent.py --validate
python scripts/test_rebyte_live.py --model gpt-5.6-luna
```

Local product verification uses the retail UI at `http://localhost:3000` and
FastAPI at `http://localhost:8000`: search, product presentation, cart add, and
checkout in the same browser conversation.

The live script reads the root `.env` or exported `REBYTE_API_KEY` and
`REBYTE_BASE_URL`. For local Relay, set the URL to `http://127.0.0.1:34567/v1`
and use a key for the local organization with enough model/compute credit.
It needs no pre-created Agent or browser Session. It drives the actual FastAPI
routes using an in-process HTTP transport and real Rebyte API calls, keeps test
memory in RAM, and deletes only its own API Agent and Sessions. A failed test
exits nonzero. It does not install or start another web server.

The Rebyte integration covers retail shopping. Merchant and other upstream
verticals retain their original Anthropic runtimes and setup.

## API references

- [Rebyte Agents API](https://rebyte.ai/docs/agents-api/overview)
- [OpenAI Agents API](https://developers.openai.com/api/docs/guides/agents-api/overview)
- [Function handoff](https://developers.openai.com/api/docs/guides/agents-api/tools/functions)
- [Smaller Node recipes and App Kit](https://github.com/ReByteAI/rebyte-agent-toolkit)

The official OpenAI SDK is the protocol client; Rebyte hosts models, compute,
storage and billing. The protocol value `openai_hosted` selects Rebyte compute
when sent to Rebyte. This path does not use the separate OpenAI Agents SDK.
