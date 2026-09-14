"""Create or update the Commerce API Agent from the checked-in local manifest."""

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))
from retail.api.rebyte_config import agent_parameters, session_environment  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model")
    parser.add_argument("--agent-id", help="Update this existing API Agent")
    parser.add_argument("--validate", action="store_true")
    args = parser.parse_args()
    config = agent_parameters(args.model)
    environment = session_environment()
    if args.validate:
        print(
            json.dumps(
                {
                    "model": config["model"],
                    "tools": len(config["tools"]),
                    "skills": [s["name"] for s in environment["skills"]],
                }
            )
        )
        return
    load_dotenv(ROOT / ".env", override=False)
    base = os.environ.get("REBYTE_BASE_URL", "https://api.rebyte.ai").rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    client = OpenAI(api_key=os.environ["REBYTE_API_KEY"], base_url=base, max_retries=0)
    with client:
        agent = (
            client.beta.agents.create(**config)
            if args.agent_id is None
            else client.beta.agents.update(args.agent_id, **config)
        )
    print(agent.id)


if __name__ == "__main__":
    main()
