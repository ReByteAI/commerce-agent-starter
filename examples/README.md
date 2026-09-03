<!-- Modified by ReByteAI in 2026 to integrate the Rebyte managed Agent API. -->

# examples

Four vertical demos, each running both agents over one catalog: `retail/` (ACME),
`travel/` (ACME Travel), `telecom/` (ACME Mobile), and `entertainment/` (ACME Tickets).
`python scripts/run_demo.py <vertical>` starts one; each vertical's README lists its ports,
prompts to try on both surfaces, and what it adds to the libraries.

## Layout

| Path | Contents |
|---|---|
| `demo_common/` | Host code the four APIs share: app and middleware (`host.py`), session store (`sessions.py`), storefront routes (`storefront.py`), merchant router (`merchant.py`), Rebyte Responses bridge (`rebyte_responses.py`), authenticated MCP gateway (`mcp_gateway.py`), memory routes and fixture seeder (`memory.py`), mock-backend helpers (`*_fixtures.py`) |
| `web-shared/` | The npm package the eight web apps import: the API client, the session and turn hooks, the event types (`protocol.ts` mirrors `commerce_common/streaming.py`), the transcript and inspector components, shared primitives and icons, and the two app frames (`storefront/`, `portal/`) |
| `package.json` | The npm workspace: `web-shared` plus every `*/storefront-web` and `*/merchant-web` (`npm ci` installs all of them) |
| `<vertical>/api/` | One FastAPI process: the two mock backends, the two agent configs (`agent_config.py`), the vertical's own routes and presentation extensions, and the merchant router mounted under `/api/merchant` |
| `<vertical>/data/` | The fixtures both backends load, listed in the vertical's README |
| `<vertical>/storefront-web/`, `<vertical>/merchant-web/` | The Next.js apps: this vertical's cards, views, and tokens over `web-shared` |

Sessions, carts, and staged changes live in one process's memory in `demo_common`, so the
examples run one worker.

`web-shared` holds the session, streaming, and rendering plumbing once. Each app holds its
own components: `components/generative/` has one entry per presentation tool, typed by the
app's `lib/types.ts`, so the four frontends are four builds of the same payload schemas
(`shopping_agent/tools/presentation.py`, `merchant_agent/tools/presentation.py`); a
deployment's frontend is a fifth.

## Showcase pages

Every web app serves `/showcase`, which renders each of its components from
`lib/showcase-fixtures.ts`; it needs neither the API nor a key, and `run_demo.py` prints
the storefront's showcase URL when the demo is up. Open it when changing a component.

## Identity

A storefront session starts by naming a profile from `data/users.json` (`POST /api/session`);
a merchant session binds to the one merchant the process serves. Every later request carries
only the session id, in `X-Session-Id`, and the routes read the principal from it.

## Environment variables

| Variable | Effect | Read in | Default |
|---|---|---|---|
| `REBYTE_API_KEY` | Organization API key used by the retail FastAPI BFF; never expose it through a `NEXT_PUBLIC_` variable | `demo_common/rebyte_responses.py` | required for retail chat |
| `REBYTE_AGENT_ID` | Managed Agent called by the retail BFF | `demo_common/rebyte_responses.py` | required after Agent creation |
| `REBYTE_BASE_URL` | Rebyte API origin | `demo_common/rebyte_responses.py` | `https://api.rebyte.ai` |
| `REBYTE_MCP_GATEWAY_TOKEN` | At least 32 characters; the Custom MCP connector sends it to the retail MCP gateway as a Bearer token | `demo_common/mcp_gateway.py` | required for retail |
| `COMMERCE_MCP_UPSTREAM_URL` | Exact loopback `/mcp/` URL the gateway may forward to; `run_demo.py` sets it from the actual API port | `demo_common/mcp_gateway.py` | `http://127.0.0.1:8000/mcp/` |
| `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` | Chat credentials; the environment wins over `<vertical>/.env`, which wins over the repo-root `.env` | `demo_common/host.py` | unset (client credential chain) |
| `COMMERCE_DEMO_AUTH` | `sdk` skips the `.env` files and clears the key variables so the client's credential chain is used; `run_demo.py --federated` sets it | `demo_common/host.py` | unset |
| `DEMO_ALLOWED_HOSTS` | Comma-separated Host values the API answers to besides `localhost` and `127.0.0.1` | `demo_common/host.py` | unset |
| `DEMO_LOG_LEVEL` | `INFO` writes one line per model call; `DEBUG` adds each request and response | `demo_common/host.py` | `INFO` |
| `MERCHANT_REQUIRE_HOST_APPROVAL` | `0` lets an approval typed in chat apply a change; `1` requires the preview card's button | `demo_common/host.py` | `1` |
| `MERCHANT_ANALYSIS_CODE_EXECUTION` | `1` mounts the hosted code execution tool in the retail analysis delegate | `retail/api/agent_config.py` | `0` |
| `MERCHANT_ANALYSIS_MODEL` | The retail analysis delegate's model | `retail/api/agent_config.py` | unset (main model) |
| `NEXT_PUBLIC_API_URL` | Where a web app sends its requests; `run_demo.py` sets it to the port the API came up on | `<app>/lib/api.ts` | `http://localhost:<API_PORT>` |

The API reads its variables at startup; a web app takes the `NEXT_PUBLIC_` values when it
is built or its dev server starts.

For the retail starter, `run_demo.py` starts the application/BFF on `:8000` and its
authenticated MCP-only gateway on `:8100`. The browser calls only `:8000`. A temporary
public tunnel for the Rebyte connector must point only to `:8100`, with `/mcp/` as the
connector path and `REBYTE_MCP_GATEWAY_TOKEN` as its Bearer token. Never tunnel `:8000`;
that process owns the organization API key and the browser never receives it.

## Rebyte Responses bridge

`demo_common.rebyte_responses.RebyteResponsesAdapter` uses the official OpenAI Python SDK
against Rebyte's Responses API and emits the `AgentEvent` protocol consumed by `web-shared`.
Create one adapter per API process and pass the host's session id on every turn:

```python
adapter = RebyteResponsesAdapter.from_env(
    presentation_hooks={"storefront/present_products": enrich_products}
)
async for event in adapter.stream_turn(record.session_id, request.message):
    yield to_sse(event)
```

Set `REBYTE_API_KEY`, `REBYTE_AGENT_ID`, and optionally `REBYTE_BASE_URL`. The first turn
creates a Rebyte Conversation; later turns for that local session reuse it. A production
session store can persist that id and restore it with `bind_conversation`. A presentation
hook receives `(local_session_id, completed_call)` with decoded arguments and output, and
returns validated, canonically enriched `AgentEvent.ui` events. MCP output remains plain,
model-facing text. The retail BFF reconstructs host-only UI events by replaying safe reads
and locally enriching presentation calls; it never repeats cart or memory writes. The
Conversation-derived scope is available through `runtime_scope(local_session_id)`.
