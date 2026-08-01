"""Manage runtime secrets without printing their values."""

import argparse
import getpass

from .secrets import SecretResolver

ALLOWED_NAMES = ("llm_api_key", "openai_api_key", "anthropic_api_key", "audit_signing_key")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("set", "status", "delete"))
    parser.add_argument("name", choices=ALLOWED_NAMES)
    args = parser.parse_args()
    resolver = SecretResolver()
    backend = resolver._keyring()
    if args.action == "set":
        value = getpass.getpass(f"Value for {args.name}: ")
        if len(value) < 6:
            raise SystemExit("secret is unexpectedly short; no change made")
        resolver.set(args.name, value)
        print(f"stored {args.name} in OS keyring")
    elif args.action == "status":
        exists = backend.get_password(resolver.service, args.name) is not None
        print(f"{args.name}: {'configured' if exists else 'missing'}")
    else:
        backend.delete_password(resolver.service, args.name)
        print(f"deleted {args.name} from OS keyring")


if __name__ == "__main__":
    main()
