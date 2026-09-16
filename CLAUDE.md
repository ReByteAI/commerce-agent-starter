# commerce-agents

For agents working in this repo, commerce-builder plugin users included. The public
reference for commerce agents on Claude: a shopping agent and a merchant agent on three
paths each, four vertical examples, and a Claude Code plugin.

## Layout

- `commerce-common/commerce_common/`: what both roles share; its `__init__` lists the modules.
- `shopping-agent/core/shopping_agent/`: types, `StorefrontBackend`, config, prompt, `tools/`, gates, enrichment, executor.
- `merchant-agent/core/merchant_agent/`: the merchant equivalents, plus `changes.py` and `analysis.py`.
- `*/skills/`: five flows per role, one `SKILL.md` each.
- `*/runtime-messages-api/`: `ShoppingAgent`, `MerchantAgent`, the merchant analysis delegate.
- `*/runtime-agent-sdk/`: each agent as `ClaudeAgentOptions`, with a console.
- `*/managed-agents/`: the manifest directory (with the derived `system.md`) and the role's MCP server.
- `examples/demo_common/` and `examples/web-shared/`: what the verticals' APIs and web apps share; `examples/` is the npm workspace.
- `examples/<vertical>/`: `api/`, `data/`, `storefront-web/`, `merchant-web/`; ports 8000-8003, 3000-3003, 3100-3103.
- `plugins/commerce-builder/`: six skills, four commands; `.claude-plugin/marketplace.json` points at it.
- `docs/`: `safety.md`, `backends.md`, `deployment.md`. `scripts/`: install, demo, smoke, screenshots, check, deploy, verify.
- `tests/`: the suites that span packages (both roles on all three paths); each package keeps its own `tests/`.
- `rebyte/README.md` documents the integration. `rebyte/agent.toml`: the pre-created Rebyte Agent's prompt, skills, and original commerce contracts as client tools. `examples/retail/api/rebyte_agent.py` executes those tools in the retail host and posts results through Agents Session events. `rebyte_config.py` resolves the local manifest into Agent definitions and per-Session inline Skills; `scripts/setup_rebyte_agent.py` creates or updates the API Agent. `scripts/test_rebyte_live.py` drives real Agents API execution through isolated in-process retail HTTP routes and cleans up its test resources.
  Rebyte Agent definitions use `tool_search` with all twenty Commerce functions
  deferred. The live script's `--tool-loading eager|deferred` compares both
  configurations; `--report` records timing, token usage and trace resource IDs.

`requirements.txt` installs the seven packages and their pinned dependencies (`requirements-dev.txt`
adds pytest and ruff); `scripts/install.sh` runs it.

## Design rules

- One model owns the conversation; a rule goes in a tool description, the prompt, or a skill by how often it applies.
- The upstream runtimes keep the static prompt and `tools[]` identical on every
  turn; per-request data goes in the fenced block after the breakpoint. Rebyte
  retains the full saved definitions while exposing deferred functions to the
  model as the Session discovers them.
- UI is presentation tool calls, validated and filled in on the server, streamed as `ui` events.
- Third-party content is fenced data; writes are provenance-gated and capped in code; `checkout` charges nothing; merchant writes apply only through host approval.
- Core is domain-neutral; a vertical adds UI through `PresentationExtension` and keeps the rest to itself.
- Each mechanism is defined once, in `commerce_common` or a role core, and shared by all three paths.

## Fictional and original

No real company, brand, product, or person appears: the only company is ACME and its
lines; every brand, prompt, schema, and figure is invented here. Two exceptions:
deployment and integration targets (the README's "MCP connectors" section; platform and
SDK names in `docs/deployment.md`, the README's deploying section, and the platform tests),
and CC0 category photos listed in the `IMAGE-CREDITS.md` beside them. When in doubt,
redesign rather than rename.

## Conventions

- Python 3.11+, `ruff` (root `ruff.toml`), `pytest` (root `pytest.ini`), type hints, `pydantic` schemas; web apps are Next.js and TypeScript.
- Skill descriptions name the request class, without sample utterances; tool descriptions say when the tool applies; examples stay schematic.
- A change to prompt text, a tool description, a skill, or a fence notice re-derives `system.md`; `scripts/check.py` compares them.
- Prose: plain declarative sentences; one term per thing; each fact once, naming its module; each role in its own terms; a README says what a thing is, how to run it, and where its interfaces are; no history, dates, or process narrative; cut before restyling.
- A new module updates this file and its README.

## Verify

```bash
ruff check . && ruff format --check . && pytest && python scripts/check.py
python scripts/verify_all.py          # adds deploy dry runs and web builds
```
