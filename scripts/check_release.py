"""Fail-closed checks required before creating a release archive."""

import json
import subprocess
import sys
from pathlib import Path

import jsonschema


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    for name in ("intent_contract.schema.json", "workflow_contract.schema.json"):
        schema = json.loads((root / "schemas" / name).read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        if schema.get("additionalProperties") is not False:
            raise SystemExit(f"{name}: root must fail closed on additional properties")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"], cwd=root, check=False,
    )
    if result.returncode:
        raise SystemExit(result.returncode)
    print("release checks passed")


if __name__ == "__main__":
    main()
