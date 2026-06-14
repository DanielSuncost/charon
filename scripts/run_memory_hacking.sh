#!/usr/bin/env bash
# Run the memory-transmits-reward-hacking experiment under both framings and
# print the 2x4 table (memory arm x framing).
#
#   ./scripts/run_memory_hacking.sh [episodes]   # default 20
#
# Makes real provider calls (the model in .charon_state, currently codex/gpt-5.5):
# roughly episodes * 4 arms * ~1.5 calls per framing.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
EP="${1:-20}"

export CHARON_STATE_DIR="$ROOT/.charon_state"
export CHARON_EMBED_BACKEND="${CHARON_EMBED_BACKEND:-local}"
export PYTHONPATH="apps/core-daemon"
PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"

ADV="results/exp_memory_hacking_adversarial.json"
NEU="results/exp_memory_hacking_neutral.json"

echo ">>> adversarial framing (n=$EP) ..."
"$PY" scripts/exp_memory_hacking.py --episodes "$EP" --framing adversarial --out "$ADV"
echo ">>> neutral framing (n=$EP) ..."
"$PY" scripts/exp_memory_hacking.py --episodes "$EP" --framing neutral --out "$NEU"

echo
echo "=== memory x framing: hack rate ==="
"$PY" - "$NEU" "$ADV" <<'PYEOF'
import json, sys
neu = json.load(open(sys.argv[1])); adv = json.load(open(sys.argv[2]))
arms = list(neu["arms"].keys())
n = adv["episodes"]
print(f"model {adv['model']}  n={n} per cell\n")
print(f"{'memory arm':18}{'neutral':>10}{'adversarial':>14}")
for a in arms:
    nh = neu["arms"][a]["hack_rate"]; ah = adv["arms"][a]["hack_rate"]
    print(f"{a:18}{nh:>10}{ah:>14}")
PYEOF
