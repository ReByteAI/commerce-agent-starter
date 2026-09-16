"""Resolve the local Commerce manifest into Agent and Session API parameters."""

from __future__ import annotations

import base64
import io
import tomllib
import zipfile
from pathlib import Path
from typing import Any

from commerce_common.skills import load_skill_dir

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "rebyte" / "agent.toml"


def agent_parameters(model: str | None = None, *, defer_functions: bool = True) -> dict[str, Any]:
    spec = tomllib.loads(MANIFEST.read_text())
    return {
        "name": spec["name"],
        "model": spec["llm"] if model is None else model,
        "instructions": (MANIFEST.parent / spec["prompt_file"]).read_text(),
        "tools": ([{"type": "tool_search"}] if defer_functions else [])
        + [
            {
                **{key: tool[key] for key in ("type", "name", "description", "parameters")},
                "defer_loading": defer_functions,
            }
            for tool in spec["client_tools"]
        ]
        + [{"type": "web_search"}],
    }


def session_environment() -> dict[str, Any]:
    spec = tomllib.loads(MANIFEST.read_text())
    skills = []
    for reference in spec["skills"]:
        directory = ROOT / reference["path"]
        skill = load_skill_dir(directory)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for file in sorted(directory.rglob("*")):
                if file.is_file():
                    info = zipfile.ZipInfo(
                        file.relative_to(directory).as_posix(), (1980, 1, 1, 0, 0, 0)
                    )
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o100644 << 16
                    bundle.writestr(info, file.read_bytes())
        skills.append(
            {
                "type": "inline",
                "name": skill.name,
                "description": skill.description,
                "source": {
                    "type": "base64",
                    "media_type": "application/zip",
                    "data": base64.b64encode(archive.getvalue()).decode(),
                },
            }
        )
    return {"type": "openai_hosted", "skills": skills}
