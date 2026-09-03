# Copyright 2026 ReByteAI
# SPDX-License-Identifier: Apache-2.0

"""Render the Rebyte Agent manifest with an organization-owned MCP server UUID."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE = REPO_ROOT / "rebyte" / "agent.template.toml"
DEFAULT_OUTPUT = REPO_ROOT / ".rebyte" / "agent.toml"
MCP_SERVER_ID_PLACEHOLDER = "__REBYTE_MCP_SERVER_ID__"
MCP_SERVER_ID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    re.IGNORECASE,
)


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
