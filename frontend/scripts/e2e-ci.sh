#!/usr/bin/env bash
set -euo pipefail

repo_env="../.env"
generated_env=0

cleanup() {
  if [[ "$generated_env" == "1" ]]; then
    rm -f "$repo_env"
  fi
}
trap cleanup EXIT

if [[ "${CI:-}" == "true" && ! -f "$repo_env" ]]; then
  cp ../.env.example "$repo_env"
  generated_env=1
  python3 - "$repo_env" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
updates = {
    "POSTGRES_PASSWORD": "ci_only_test_password",
    "LITELLM_API_KEY": "",
    "LITELLM_API_BASE": "",
    "INGEST_ON_STARTUP": "false",
    "OMNICARE_INGEST_PROCEDURES": "false",
}
lines = path.read_text().splitlines()
seen = set()
result = []
for line in lines:
    candidate = line.lstrip("# ").split("=", 1)[0].strip()
    if candidate in updates:
        result.append(f"{candidate}={updates[candidate]}")
        seen.add(candidate)
    else:
        result.append(line)
for key, value in updates.items():
    if key not in seen:
        result.append(f"{key}={value}")
path.write_text("\n".join(result) + "\n")
PY
fi

if [[ "${CI:-}" == "true" ]]; then
  export POSTGRES_PASSWORD="ci_only_test_password"
  export LITELLM_API_KEY=""
  export LITELLM_API_BASE=""
fi

npx playwright test --reporter=line
