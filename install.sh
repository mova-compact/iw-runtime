#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"

python3 -c 'import sys; assert sys.version_info[:2] in ((3,11),(3,12)), "Python 3.11 or 3.12 required"'
docker info --format 'Docker {{.ServerVersion}}' >/dev/null

test -d .venv || python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.txt
test -f .env || cp .env.example .env

echo "Installation complete."
echo "1. Edit .env and choose your provider/model."
echo "2. Store the API key: .venv/bin/python -m runtime.secret_cli set llm_api_key"
echo "3. Check readiness: .venv/bin/python -m runtime.cli doctor"
echo "4. Run the sample: .venv/bin/python examples/example_run.py"
