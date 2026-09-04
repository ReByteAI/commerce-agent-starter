# Copyright 2026 ReByteAI
# SPDX-License-Identifier: Apache-2.0

"""Render the Rebyte Agent manifest with an organization-owned MCP server UUID."""

from __future__ import annotations

import argparse
import copy
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE = REPO_ROOT / "rebyte" / "agent.template.toml"
DEFAULT_OUTPUT = REPO_ROOT / ".rebyte" / "agent.toml"
MCP_SERVER_ID_PLACEHOLDER = "__REBYTE_MCP_SERVER_ID__"
MCP_SERVER_ID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    re.IGNORECASE,
)
STRICT_CLIENT_TOOL_SCHEMA_KEYWORDS = frozenset(
    {
        "$schema",
        "$defs",
        "definitions",
        "$ref",
        "type",
        "description",
        "enum",
        "const",
        "anyOf",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "multipleOf",
        "maximum",
        "exclusiveMaximum",
        "minimum",
        "exclusiveMinimum",
        "minItems",
        "maxItems",
    }
)


def strict_client_tool_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return the OpenAI strict form of one repository tool input schema.

    Strict function schemas require every object property in ``required``. Properties
    that were optional remain optional in meaning by accepting JSON null; the host drops
    null values before handing arguments to the original commerce executor.
    """

    normalized = copy.deepcopy(schema)

    def visit(node: Any) -> None:
        if not isinstance(node, dict):
            return
        for keyword in tuple(node):
            if keyword not in STRICT_CLIENT_TOOL_SCHEMA_KEYWORDS:
                del node[keyword]
        properties = node.get("properties")
        if node.get("type") == "object" and isinstance(properties, dict):
            originally_required = set(node.get("required", []))
            node["required"] = list(properties)
            for name, child in properties.items():
                visit(child)
                if name not in originally_required and isinstance(child, dict):
                    child_type = child.get("type")
                    if isinstance(child_type, str) and isinstance(child.get("enum"), list):
                        child["anyOf"] = [
                            {"type": child.pop("type"), "enum": child.pop("enum")},
                            {"type": "null"},
                        ]
                    elif isinstance(child_type, str):
                        child["type"] = [child_type, "null"]
                    elif isinstance(child_type, list) and "null" not in child_type:
                        child["type"] = [*child_type, "null"]
        items = node.get("items")
        if isinstance(items, dict):
            visit(items)
        any_of = node.get("anyOf")
        if isinstance(any_of, list):
            for branch in any_of:
                visit(branch)
        for definitions_keyword in ("$defs", "definitions"):
            definitions = node.get(definitions_keyword)
            if isinstance(definitions, dict):
                for definition in definitions.values():
                    visit(definition)

    visit(normalized)
    return normalized


def render_agent_manifest(template: str, mcp_server_id: str) -> str:
    """Return ``template`` with exactly one validated MCP server UUID inserted."""
    server_id = mcp_server_id.strip().lower()
    if MCP_SERVER_ID_PATTERN.fullmatch(server_id) is None:
        raise ValueError("MCP server ID must be a UUID returned by the Rebyte Control Plane")
    occurrences = template.count(MCP_SERVER_ID_PLACEHOLDER)
    if occurrences != 1:
        raise ValueError(
            f"agent template must contain the MCP server placeholder exactly once; found {occurrences}"
        )
    return template.replace(MCP_SERVER_ID_PLACEHOLDER, server_id)


def write_agent_manifest(
    mcp_server_id: str,
    *,
    template_path: Path = DEFAULT_TEMPLATE,
    output_path: Path = DEFAULT_OUTPUT,
    force: bool = False,
) -> Path:
    """Render the manifest and write it without replacing an existing file by default."""
    rendered = render_agent_manifest(template_path.read_text(encoding="utf-8"), mcp_server_id)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "w" if force else "x"
    with output_path.open(mode, encoding="utf-8") as output:
        output.write(rendered)
    return output_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mcp_server_id", help="UUID from the Rebyte Control Plane")
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing .rebyte/agent.toml",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        output = write_agent_manifest(args.mcp_server_id, force=args.force)
    except (FileExistsError, OSError, ValueError) as error:
        raise SystemExit(f"Error: {error}") from error
    print(f"Rendered {output.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
