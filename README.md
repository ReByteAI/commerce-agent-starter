<!-- Modified by ReByteAI in 2026 to integrate the Rebyte managed Agent API. -->

# Commerce Agent Starter

Run Anthropic's ACME shopping agent and storefront UI on Rebyte's managed Agent API.
The browser keeps the original generative UI; Rebyte owns the conversation, model loop,
Skills, and MCP orchestration.

```text
ACME React UI -> FastAPI -> OpenAI SDK -> Rebyte Responses API
                                      -> managed Agent -> storefront MCP -> mock retailer
```

The supported starter path is [`examples/retail/`](examples/retail/). The other commerce
agents and verticals remain as upstream reference code.

**Hosted demo:** [commerce-agent-starter.cctools.workers.dev](https://commerce-agent-starter.cctools.workers.dev)
(`@rebyte.ai` access).

## Run it

You need Python 3.11+, Node 22, pnpm, `cloudflared` (or another public HTTPS endpoint),
organization admin access, and a
[Rebyte organization API key](https://app.rebyte.ai/settings/api-keys).

```bash
git clone https://github.com/ReByteAI/commerce-agent-starter.git
cd commerce-agent-starter
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
(cd examples && npm ci)
cp .env.example .env
```

Put your API key in the repo-root `.env`, leave `REBYTE_AGENT_ID` empty, and generate a
random gateway secret with `openssl rand -hex 32`. Put that output in `.env` as
`REBYTE_MCP_GATEWAY_TOKEN`. Then start both local server processes:

```bash
python scripts/run_demo.py retail --api-only
```

This starts the FastAPI application/BFF on `:8000` and an authenticated, MCP-only gateway
on `:8100`. In another terminal, give only the gateway a temporary HTTPS URL:

```bash
cloudflared tunnel --url http://127.0.0.1:8100 \
  --http-host-header 127.0.0.1:8100
```

Open [Organization Connectors](https://app.rebyte.ai/settings/connectors#organization),
choose **Custom MCP**, and enter the tunnel URL followed by `/mcp/`. Select **Bearer
token** and paste the same `REBYTE_MCP_GATEWAY_TOKEN` value. Copy the server UUID after
the connector is added.

The tunnel must point to `:8100`, never `:8000`. The gateway exposes only `/mcp/`, requires
the bearer secret, and forwards accepted MCP traffic to the loopback API. The browser calls
the BFF on `:8000`; it never receives the Rebyte organization API key.

Render the organization-specific manifest, install the public Rebyte CLI, and create the
Agent:

```bash
export REBYTE_MCP_SERVER_ID="paste-the-server-uuid"
python scripts/render_rebyte_agent.py "$REBYTE_MCP_SERVER_ID"
pnpm add --global \
  https://github.com/ReByteAI/rebyte-agent-toolkit/releases/latest/download/rebyte-cli.tgz
set -a; source .env; set +a
rebyte agent validate -f .rebyte/agent.toml
rebyte agent create -f .rebyte/agent.toml
```

Put the returned Agent ID in `.env` as `REBYTE_AGENT_ID`, then reload the edited file in
the terminal where you will run the demo. This second load replaces the intentionally
blank value exported before Agent creation. Stop the API-only process, keep the tunnel
running, and start the complete demo:

```bash
set -a; source .env; set +a
python scripts/run_demo.py retail
```

Open <http://localhost:3000> and try:

> I need a two-person tent under $250 that is easy to set up.

The API key stays in FastAPI. The browser receives only the demo's event stream.
`.env` and the rendered `.rebyte/agent.toml` are gitignored.

## Cloudflare deployment

The hosted demo serves the exported Next.js storefront from a Cloudflare Worker and
routes `/api/*` and `/mcp/*` to one Cloudflare Container running FastAPI. The browser is
protected by Cloudflare Access. `/mcp/*` bypasses Access so the Rebyte Control Plane can
reach it, but the Worker still requires `REBYTE_MCP_GATEWAY_TOKEN` before forwarding any
MCP request.

Before the first production deploy, create these Cloudflare Access applications for the
Worker hostname:

1. Protect `your-worker.workers.dev` with an Allow policy for the people who may use the
   demo. This protects both the storefront and `/api/*`; the API has no separate end-user
   login.
2. Add the more-specific `your-worker.workers.dev/mcp/*` application with a Bypass policy
   so Rebyte can connect. The Worker Bearer token remains required on this path.

```bash
pnpm install
pnpm check
pnpm exec wrangler secret put REBYTE_API_KEY
pnpm exec wrangler secret put REBYTE_MCP_GATEWAY_TOKEN
pnpm run deploy
```

After deployment, an unauthenticated request to `/` must redirect to Cloudflare Access,
and an unauthenticated request to `/mcp/` must return `401`.

This reference deployment keeps browser sessions and Rebyte Conversation bindings in the
Container's memory. A Container restart clears them; refresh the page to begin a new
session. Back these stores with durable storage before using the pattern in production.

Set `REBYTE_AGENT_ID` and the production host in [`wrangler.jsonc`](wrangler.jsonc).
Never put either secret in a `NEXT_PUBLIC_*` variable; they belong only in Worker secrets
and are passed from the Worker into the trusted Container.

## Starter files

| Path | Responsibility |
|---|---|
| [`rebyte/agent.template.toml`](rebyte/agent.template.toml) | Managed Agent model, prompt, Skills, and custom MCP capability |
| [`scripts/render_rebyte_agent.py`](scripts/render_rebyte_agent.py) | Inserts the Control Plane's MCP UUID into `.rebyte/agent.toml` |
| [`examples/demo_common/rebyte_responses.py`](examples/demo_common/rebyte_responses.py) | Adapts the Rebyte Responses stream to the existing UI event protocol |
| [`examples/demo_common/mcp_gateway.py`](examples/demo_common/mcp_gateway.py) | Authenticated MCP-only gateway for the temporary public tunnel |
| [`examples/retail/api/`](examples/retail/api/) | Server-side BFF, loopback MCP endpoint, and mock commerce backend |
| [`examples/retail/storefront-web/`](examples/retail/storefront-web/) | Original ACME storefront and generative shopping UI |

## Safety

The data and company are fictional. Checkout renders a summary but does not place an order
or charge money. The included gateway authenticates the development MCP tunnel, but the
starter has no end-user authentication. Add user identity and authorization before
connecting real systems. Stop the temporary tunnel after local testing.

## Attribution

This repository is adapted from
[Anthropic's `commerce-agents`](https://github.com/anthropics/commerce-agents). Anthropic's
copyright and Apache 2.0 license are retained; see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
Anthropic does not endorse or maintain this derivative work.
