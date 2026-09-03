import { Container, getContainer } from "@cloudflare/containers";

interface Env {
  ASSETS: Fetcher;
  COMMERCE_CONTAINER: DurableObjectNamespace<CommerceContainer>;
  DEMO_ALLOWED_HOSTS: string;
  REBYTE_AGENT_ID: string;
  REBYTE_API_KEY: string;
  REBYTE_BASE_URL: string;
  REBYTE_MCP_GATEWAY_TOKEN: string;
}

export class CommerceContainer extends Container<Env> {
  defaultPort = 8080;
  sleepAfter = "10m";
  enableInternet = true;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.envVars = {
      DEMO_ALLOWED_HOSTS: env.DEMO_ALLOWED_HOSTS,
      REBYTE_AGENT_ID: env.REBYTE_AGENT_ID,
      REBYTE_API_KEY: env.REBYTE_API_KEY,
      REBYTE_BASE_URL: env.REBYTE_BASE_URL,
    };
  }
}

function isApiPath(pathname: string): boolean {
  return pathname === "/api" || pathname.startsWith("/api/");
}

function isMcpPath(pathname: string): boolean {
  return pathname === "/mcp" || pathname.startsWith("/mcp/");
}

function unauthorized(): Response {
  return new Response("Unauthorized", {
    status: 401,
    headers: { "WWW-Authenticate": "Bearer" },
  });
}

function requestForContainer(request: Request, mcp: boolean): Request {
  const headers = new Headers(request.headers);
  const url = new URL(request.url);

  // FastMCP's mounted transport permits its configured loopback host and port.
  // The actual TCP connection still targets this Container's default port (8080).
  url.protocol = "http:";
  url.hostname = "127.0.0.1";
  url.port = mcp ? "8200" : "8080";
  headers.set("Host", url.host);
  if (mcp) headers.delete("Authorization");

  return new Request(url, {
    method: request.method,
    headers,
    body: request.body,
    redirect: request.redirect,
  });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const pathname = new URL(request.url).pathname;
    const mcp = isMcpPath(pathname);

    if (mcp) {
      if (!env.REBYTE_MCP_GATEWAY_TOKEN) {
        return new Response("MCP authentication is not configured", { status: 503 });
      }
      if (request.headers.get("Authorization") !== `Bearer ${env.REBYTE_MCP_GATEWAY_TOKEN}`) {
        return unauthorized();
      }
    }

    if (mcp || isApiPath(pathname)) {
      const instanceName = env.REBYTE_AGENT_ID || "commerce-agent-starter";
      const container = getContainer(env.COMMERCE_CONTAINER, instanceName);
      return container.fetch(requestForContainer(request, mcp));
    }

    return env.ASSETS.fetch(request);
  },
} satisfies ExportedHandler<Env>;
