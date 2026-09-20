#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
exec node scripts/mac-stack.mjs stop --root "$ROOT" --development
