<!-- Modified by ReByteAI in 2026 to integrate the Rebyte managed Agent API. -->

# ACME retail storefront

This is the supported Rebyte starter: Anthropic's shopping UI backed by a managed Rebyte
Agent and this example's commerce tools. Follow the repo-root
[`README`](../../README.md) once to register the MCP endpoint and create the Agent.

`run_demo.py` starts three local surfaces:

| Port | Surface | Boundary |
|---|---|---|
| `3000` | React storefront | Calls the FastAPI BFF; receives no organization API key |
| `8000` | FastAPI application/BFF | Holds the Rebyte key and serves the browser; never expose this port through the tunnel |
| `8100` | Authenticated MCP gateway | Exposes only `/mcp/`; this is the only tunnel target |

Configure the Rebyte Custom MCP connector with the temporary tunnel's `/mcp/` URL and
`REBYTE_MCP_GATEWAY_TOKEN` as its **Bearer token**. The gateway forwards accepted requests
to the loopback MCP server on `:8000`.

## Try the flow

Use these three turns in one conversation:

1. I'm taking my partner and our 6-year-old camping for the first time next month. We need
   a tent that is easy to handle and under $250.
2. Compare the top two options for space and ease of setup.
3. Add the family one to my cart, and tell me about returns.

The expected path is search -> product cards -> comparison -> cart and policy tools. Rebyte
keeps the managed Conversation; the MCP server keeps its cart and provenance under the
trusted Rebyte Conversation ID. The separate workspace ID identifies the Agent Sandbox.

## Files

| Path | Responsibility |
|---|---|
| `api/main.py` | FastAPI BFF, loopback `/mcp/` server, and storefront routes |
| `../demo_common/mcp_gateway.py` | Bearer-authenticated, MCP-only tunnel boundary |
| `api/mock_retail.py` | Fictional catalog, cart, orders, and policies |
| `storefront-web/` | React storefront and rendered agent components |
| `data/` | Fictional fixtures and local memory state |

The gateway authenticates MCP traffic, but the demo has no end-user authentication.
Product images are CC0 category images credited in
[`storefront-web/public/products/IMAGE-CREDITS.md`](storefront-web/public/products/IMAGE-CREDITS.md).
