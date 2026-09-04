# Copyright 2026 ReByteAI
# SPDX-License-Identifier: Apache-2.0

import tomllib
from pathlib import Path
from runpy import run_path

import pytest

from shopping_agent import ShoppingAgentConfig
from shopping_agent.enrichment import PRESENTATION_COMPONENTS
from shopping_agent.tools.registry import build_tools

SCRIPT = run_path(str(Path(__file__).resolve().parents[1] / "scripts/render_rebyte_agent.py"))
MCP_SERVER_ID_PLACEHOLDER = SCRIPT["MCP_SERVER_ID_PLACEHOLDER"]
render_agent_manifest = SCRIPT["render_agent_manifest"]
write_agent_manifest = SCRIPT["write_agent_manifest"]
strict_client_tool_schema = SCRIPT["strict_client_tool_schema"]

# RFC-style example assembled at test time; this is not a registered Rebyte connector.
EXAMPLE_SERVER_ID = "-".join(("123e4567", "e89b", "42d3", "a456", "426614174000"))


def test_render_agent_manifest_replaces_the_only_placeholder() -> None:
    rendered = render_agent_manifest(
        f'capabilities = ["custom:{MCP_SERVER_ID_PLACEHOLDER}"]\n',
        EXAMPLE_SERVER_ID.upper(),
    )

    assert rendered == f'capabilities = ["custom:{EXAMPLE_SERVER_ID}"]\n'


@pytest.mark.parametrize(
    "server_id",
    ["", "not-a-uuid", "00000000-0000-0000-0000-000000000000"],
)
def test_render_agent_manifest_rejects_invalid_server_ids(server_id: str) -> None:
    with pytest.raises(ValueError, match="must be a UUID"):
        render_agent_manifest(MCP_SERVER_ID_PLACEHOLDER, server_id)


def test_write_agent_manifest_does_not_overwrite_without_force(tmp_path: Path) -> None:
    template = tmp_path / "agent.template.toml"
    output = tmp_path / ".rebyte" / "agent.toml"
    template.write_text(MCP_SERVER_ID_PLACEHOLDER, encoding="utf-8")

    write_agent_manifest(EXAMPLE_SERVER_ID, template_path=template, output_path=output)

    with pytest.raises(FileExistsError):
        write_agent_manifest(EXAMPLE_SERVER_ID, template_path=template, output_path=output)

    write_agent_manifest(EXAMPLE_SERVER_ID, template_path=template, output_path=output, force=True)
    assert output.read_text(encoding="utf-8") == EXAMPLE_SERVER_ID


def test_agent_client_tools_match_the_shopping_presentation_contracts() -> None:
    manifest = tomllib.loads(SCRIPT["DEFAULT_TEMPLATE"].read_text(encoding="utf-8"))
    client_tools = {tool["name"]: tool for tool in manifest["client_tools"]}
    source_tools = {
        tool["name"]: tool
        for tool in build_tools(ShoppingAgentConfig(), skill_names=[])
        if tool["name"] in PRESENTATION_COMPONENTS
    }

    assert client_tools.keys() == source_tools.keys()
    for name, source in source_tools.items():
        assert client_tools[name] == {
            "type": "function",
            "name": name,
            "description": source["description"],
            "strict": True,
            "parameters": strict_client_tool_schema(source["input_schema"]),
        }


def test_strict_client_tool_schema_preserves_supported_string_limits() -> None:
    normalized = strict_client_tool_schema(
        {
            "type": "object",
            "properties": {
                "label": {"type": "string", "minLength": 1, "maxLength": 80},
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 4,
                },
            },
            "required": ["label", "items"],
            "additionalProperties": False,
        }
    )

    assert normalized["properties"]["label"] == {
        "type": "string",
        "minLength": 1,
        "maxLength": 80,
    }
    assert normalized["properties"]["items"]["minItems"] == 1
    assert normalized["properties"]["items"]["maxItems"] == 4
