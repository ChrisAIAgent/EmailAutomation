#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
export TACWORK_ROOT="${TACWORK_ROOT:-$ROOT/../TACWork}"

cd "$ROOT"
node scripts/mac-setup.mjs --root "$ROOT"
cd "$ROOT/desktop"
exec npm run dev
