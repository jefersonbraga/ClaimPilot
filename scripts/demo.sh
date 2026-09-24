#!/usr/bin/env bash
# ClaimPilot 2–3 minute demo. Requires: a running API (uvicorn or docker compose), curl, python3. No jq needed.
#   ./scripts/demo.sh                      # against http://localhost:8000
#   BASE_URL=http://host:8000 ./scripts/demo.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UNKNOWN="$ROOT/data/claims/04_ambiguous_emergency_unknown.json"
CONFIRMED="$ROOT/data/claims/04b_ambiguous_emergency_confirmed.json"

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
pause() { [ -t 0 ] && [ -z "${NO_PAUSE:-}" ] && read -r -p "   [enter] " _ || true; }

# Print selected fields of a JSON document read from stdin.
show() {
  python3 -c '
import json, sys
doc = json.load(sys.stdin)
for key in sys.argv[1:]:
    val = doc.get(key)
    if isinstance(val, list) and val and isinstance(val[0], dict):
        print(f"{key}:")
        for item in val:
            print("   -", " | ".join(str(v) for k, v in item.items() if k in ("policy_id", "version", "excerpt", "tool", "outcome", "detail")))
    elif isinstance(val, list):
        print(f"{key}:")
        for item in val:
            print("   -", item)
    else:
        print(f"{key}: {val}")
' "$@"
}

post_claim() { curl -sf -X POST "$BASE_URL/claims/analyze" -H 'Content-Type: application/json' -d @"$1"; }

step "0. Service health"
curl -sf "$BASE_URL/health" | show status llm_provider model workflow_version prompt_version policies_loaded

step "1. Claim arrives: knee MRI, \$1,840, GOLD_PPO, no prior auth, emergency status UNKNOWN (null)"
python3 -c 'import json,sys; c=json.load(open(sys.argv[1])); print({k: c[k] for k in ("claim_id","procedure","amount","plan","prior_authorization","emergency_indicator","date_of_service")})' "$UNKNOWN"
pause

step "2. ClaimPilot investigates — and refuses to guess"
FIRST="$(post_claim "$UNKNOWN")"
echo "$FIRST" | show recommendation deterministic_outcome confidence risk_level missing_information human_review_reasons policy_evidence
EXEC_ID="$(echo "$FIRST" | python3 -c 'import json,sys; print(json.load(sys.stdin)["execution_id"])')"
pause

step "3. Decision replay for $EXEC_ID — policies, versions, tools, routing (no chain-of-thought)"
curl -sf "$BASE_URL/executions/$EXEC_ID" | show workflow_version prompt_version model retrieved_policy_versions tool_summary routing_trail latency_ms input_tokens output_tokens estimated_cost
pause

step "4. Analyst confirms it was an emergency (emergency_indicator=true) and re-submits"
SECOND="$(post_claim "$CONFIRMED")"
echo "$SECOND" | show recommendation deterministic_outcome confidence risk_level human_review_required policy_evidence
pause

step "5. Audit history for CLM-92811 — every execution is kept"
curl -sf "$BASE_URL/claims/CLM-92811/audit" | python3 -c '
import json, sys
for r in json.load(sys.stdin):
    ts, eid, rec = r["timestamp"][:19], r["execution_id"], r["recommendation"]
    emergency = str(r["claim"]["emergency_indicator"])
    print("  ", ts, eid, "emergency=" + emergency.ljust(5), "->", rec)'

step "6. Behaviour across 20 cases (run locally): python -m evals.run"
echo "   Done. Interactive API docs: $BASE_URL/docs"
