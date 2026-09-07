# Commerce Agent — Rebyte Integration

Run Anthropic's ACME retail shopping demo with the **Rebyte Agent API**. The
storefront supports product discovery, comparisons, cart updates, checkout
summaries, and customer memory. Rebyte runs the hosted Agent; the Python app
executes its commerce tools and streams the results into the original UI.

This repository includes the complete Agent spec, prompt, skills, tool
implementations, mock catalog, and storefront. The retail shopping demo needs
one Rebyte API key and one Agent created from that spec.

The application is adapted from
[Anthropic's commerce-agents](https://github.com/anthropics/commerce-agents).
ACME and its data are fictional. Checkout displays a summary; it does not place
an order or charge a card.

## Get started

You need Git, Python **3.11+**, Node.js **22+** with npm, and pnpm for the Rebyte
CLI. Use the same terminal throughout setup. After cloning, run commands from
the repository root.

### 1. Get a Rebyte API key

Open [Rebyte Platform → API Keys](https://app.rebyte.ai/platform/api-keys).
Sign in or use **Sign up** to create an account, then select your organization.
An organization administrator can choose **Create API Key**, give it a name
such as `commerce-demo`, and copy the `rbk_...` value shown once after creation.
If you are not an administrator, ask your organization's administrator to
create the key.

The demo needs `tasks:read` and `tasks:write`; organization keys created through
this dialog include both scopes. The Agent will belong to that organization.
Keep the key in the server environment:

```sh
export REBYTE_API_KEY="rbk_your_key"
```

### 2. Clone this integration and install dependencies

```sh
git clone https://github.com/ReByteAI/commerce-agent-starter.git
cd commerce-agent-starter

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
(cd examples && npm ci)

pnpm add --global https://github.com/ReByteAI/rebyte-agent-toolkit/releases/latest/download/rebyte-cli.tgz
rebyte --version
```

Use Rebyte CLI **v0.1.8 or later**. The install command downloads the CLI from
GitHub Releases. If pnpm reports that its global bin directory is missing, run
`pnpm setup`, open a new terminal, and repeat the environment and activation
commands before installing the CLI.

### 3. Create your Agent from the included spec

The complete definition is [`rebyte/agent.toml`](rebyte/agent.toml). Validate it
locally, then create the hosted Agent:

```sh
rebyte agent validate -f rebyte/agent.toml
rebyte agent create -f rebyte/agent.toml
```

Validation needs no key. Creation reads `REBYTE_API_KEY` from your shell and
posts the resolved spec to Rebyte. The CLI prints:

```text
Created Agent <agent-id> (ACME Shopping Agent)
```

Copy the returned ID into the next command:

```sh
export REBYTE_AGENT_ID="YOUR_RETURNED_AGENT_ID"
```

Create the Agent once during setup. Starting the app or opening a new browser
conversation reuses this Agent; it does not create another Agent.

### 4. Save the demo configuration

In this fresh clone, write the two values to the repository-root `.env`:

```sh
printf 'REBYTE_API_KEY=%s\nREBYTE_AGENT_ID=%s\n' \
  "$REBYTE_API_KEY" "$REBYTE_AGENT_ID" > .env
chmod 600 .env
```

[`.env.example`](.env.example) lists the available settings. FastAPI loads
`.env` automatically, so later app starts only need the virtualenv activated.
The CLI reads shell environment variables; export `REBYTE_API_KEY` again when
running CLI commands in a new terminal. Keep `.env` private; it is gitignored.

| Variable | Purpose |
|---|---|
| `REBYTE_API_KEY` | Your organization's API key, kept in FastAPI. |
| `REBYTE_AGENT_ID` | The ID printed by `rebyte agent create`. |
| `REBYTE_BASE_URL` | Optional API host; defaults to `https://api.rebyte.ai`. |

### 5. Start the storefront

```sh
python scripts/run_demo.py retail
```

Open **[http://localhost:3000](http://localhost:3000)**. This starts the retail
API on port **8000** and the storefront on port **3000**. Try these three turns:

1. `A tent for a first family camping trip, under $250`
2. `Add it to my cart`
3. `Check out`

You should see product cards, the cart update, and a checkout summary in the
same conversation. The browser calls the local API; FastAPI holds the Rebyte
credentials and executes the original commerce tools. This path needs no
Anthropic API key, storefront MCP server, or Cloudflare setup.

## What the Agent spec contains

[`rebyte/agent.toml`](rebyte/agent.toml) is the complete, reusable spec passed to
both `create` and `apply`:

| Part | Included configuration or implementation |
|---|---|
| Identity and model | `ACME Shopping Agent`, description, and `llm = "deepseek-v4-pro"`. |
| System prompt | `prompt_file` references the original [managed-agent prompt](shopping-agent/managed-agents/shopping-agent/system.md); the CLI reads it relative to the spec. |
| Skills | Five `[[skills]]` entries point to this public repository's [shopping skills](shopping-agent/skills/); `capabilities = ["skills"]` enables Rebyte to run them. |
| Client tools | Twenty `[[client_tools]]` entries contain the original names, descriptions, and parameter schemas. `strict = false` preserves optional fields and dictionaries. |
| Tool execution | The original [ShoppingToolExecutor](shopping-agent/core/shopping_agent/executor.py) runs in the Python host. The spec declares tools; their implementations stay in this repository. |

The CLI creates the Agent's configuration, including its prompt, skill
references, and client tool definitions. The
[Rebyte adapter](examples/retail/api/rebyte_agent.py) then calls that Agent
through Responses and posts each tool result back. You do not need to create
tools or copy prompts manually in the Rebyte UI.

To change the model or Agent configuration, edit the full spec, validate it,
and apply it to the existing Agent:

```sh
export REBYTE_API_KEY="rbk_your_key"
export REBYTE_AGENT_ID="YOUR_RETURNED_AGENT_ID"
rebyte agent validate -f rebyte/agent.toml
rebyte agent apply "$REBYTE_AGENT_ID" -f rebyte/agent.toml
```

`apply` replaces the Agent configuration with the complete file. Keep client
tool definitions aligned with their Python implementations. See the
[Agent configuration reference](https://rebyte.ai/docs/cli/agent-configuration)
for the spec format and [`rebyte/README.md`](rebyte/README.md) for the runtime
boundary and result handoff.

## Troubleshooting

- **Validation rejects a dictionary schema:** check `rebyte --version` and
  install the current CLI with the command above.
- **401 or missing key:** export `REBYTE_API_KEY` for the CLI; check the root
  `.env` for FastAPI. Restart the demo after changing its configuration.
- **403:** use an organization key with `tasks:read` and `tasks:write`.
- **Agent not found:** use the ID returned by `create` and a key from the same
  organization and API environment.
- **Catalog loads but chat fails:** confirm both required variables are set.
  Catalog browsing does not call Rebyte. Check the API logs in the terminal;
  the upstream host can still mention Anthropic in a generic credential error.
- **An edited `.env` value has no effect:** an already-exported shell value
  takes precedence over `.env`. Update that shell variable too, then restart.

## Other examples and development

The Rebyte integration is the **retail shopping** path. The retail merchant
portal, other verticals, and original Anthropic runtimes are also present;
those paths use `ANTHROPIC_API_KEY`. Their setup is documented in
[`examples/`](examples/), [`shopping-agent/`](shopping-agent/managed-agents/),
and [`merchant-agent/`](merchant-agent/managed-agents/).

For integration development:

```sh
pip install -r requirements-dev.txt
ruff check . && ruff format --check . && pytest && python scripts/check.py
```

The adapter tests check all twenty tool contracts against the upstream
registry and cover continuation, terminal-result storage, and stream failures.

## License

Based on Anthropic's commerce-agents reference implementation, copyright 2026
Anthropic PBC. Rebyte integration copyright 2026 ReByteAI. Licensed under the
[Apache License 2.0](LICENSE).
