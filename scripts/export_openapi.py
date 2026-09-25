"""Write core-api's OpenAPI document to contracts/core-api.openapi.json (the web client's contract).

uv run python scripts/export_openapi.py          # regenerate
uv run python scripts/export_openapi.py --check  # CI: fail if the committed file is stale
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from core_api.main import build_app
from core_api.settings import CoreApiSettings
from wassup_core.settings import Environment

TARGET = Path(__file__).resolve().parents[1] / "contracts" / "core-api.openapi.json"


def render() -> str:
    app = build_app(CoreApiSettings(environment=Environment.TEST, auth_mode="test"), engine=None)
    schema = app.openapi()
    schema["info"]["version"] = "v1"  # the contract version, not the build version
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main() -> int:
    rendered = render()
    if "--check" in sys.argv:
        if not TARGET.exists() or TARGET.read_text() != rendered:
            print(
                f"{TARGET.name} is stale: run scripts/export_openapi.py and commit it",
                file=sys.stderr,
            )
            return 1
        print(f"{TARGET.name} is up to date")
        return 0
    TARGET.write_text(rendered)
    print(f"wrote {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
