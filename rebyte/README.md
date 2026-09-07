# Rebyte Agent API integration

The retail storefront uses a pre-created Rebyte Agent through the Responses
API. `agent.toml` references the original Anthropic managed-agent system prompt
and five skills. Its twenty client tools use the original commerce schemas;
`strict = false` preserves their optional fields and dictionaries.

## Setup and Agent creation

Follow the [root README's setup guide](../README.md#get-started) to get an API
key, install the CLI, create the Agent from `rebyte/agent.toml`, save its ID,
and start the retail storefront. The root README also describes the complete
spec and how to apply configuration changes to an existing Agent.

## Runtime boundary

`examples/retail/api/rebyte_agent.py` implements the existing storefront host's
Agent interface. On the first turn it creates one Rebyte Session and retains
its `conv_...` Conversation for subsequent turns. Rebyte owns the model loop,
conversation history, and skill execution. Runtime requests use the existing
Agent; Agent creation and skill configuration happen during setup.

The adapter translates Responses text deltas and function calls into the
original `AgentEvent` protocol. Completed function calls run through
`ShoppingToolExecutor`, which owns validation, provenance, cart limits, memory
tools, and presentation enrichment. Results return as `function_call_output`
through `/v1/responses`. When the original `round_closes_turn` rule ends a
presentation round, `/v1/conversations/{id}/items` stores its results without
starting another model call. Commerce tools execute in the retail host; this
integration does not use the upstream storefront MCP server.

The hosted Agent uses the upstream managed-agent context rules: preferences
arrive through `get_preferences`, and memory writes use `save_memory`. It does
not run the Messages API runtime's separate post-turn memory extraction call
or its forced tool-choice logic. Presentation events are emitted after the
Response completes. Prompt rules, skills, tool contracts, executor logic, and
the storefront source come from the original repository.

The demo's browser-session-to-Conversation mapping lives in the FastAPI
process, alongside the upstream demo session state. Use one API worker;
restarting it ends those demo sessions.

## Verify

The adapter tests compare all twenty tool definitions with the upstream
registry and exercise continuation, terminal-result storage, and stream
failures. The upstream consistency checker verifies its derived prompts and
manifests:

```sh
pytest examples/retail/api/tests/test_rebyte_agent.py
python scripts/check.py
rebyte agent validate -f rebyte/agent.toml
```
