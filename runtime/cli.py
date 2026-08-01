"""User-facing maintenance commands for the runtime."""

import argparse
import json
import subprocess
import sys


def doctor() -> int:
    checks = {}
    checks["python"] = {
        "ok": sys.version_info[:2] in ((3, 11), (3, 12)),
        "version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    }
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=15,
        )
        checks["docker"] = {
            "ok": result.returncode == 0,
            "version": result.stdout.strip() if result.returncode == 0 else None,
        }
    except (FileNotFoundError, subprocess.TimeoutExpired):
        checks["docker"] = {"ok": False, "version": None}
    try:
        from .llm_client import _api_key, _model, _provider, provider_capabilities

        provider = _provider()
        model = _model(provider)
        _api_key(provider)
        capabilities = provider_capabilities(provider)
        checks["llm"] = {
            "ok": capabilities.full_json_schema,
            "provider": provider,
            "model": model,
            "full_json_schema": capabilities.full_json_schema,
        }
    except Exception as exc:
        checks["llm"] = {"ok": False, "error": type(exc).__name__}
    ok = all(check["ok"] for check in checks.values())
    print(json.dumps({"status": "ready" if ok else "not_ready", "checks": checks}, indent=2))
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="iw-runtime")
    parser.add_argument("command", choices=("doctor",))
    args = parser.parse_args()
    if args.command == "doctor":
        raise SystemExit(doctor())


if __name__ == "__main__":
    main()
