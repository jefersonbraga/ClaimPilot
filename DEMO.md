# ClaimPilot — 2–3 minute demo

**The point the demo makes:** when a policy exception *might* apply but a fact is unknown, ClaimPilot does not guess. It escalates, names the missing fact, and records everything. Once the fact is supplied, it produces a different, evidence-backed decision.

All data is synthetic.

## Setup (before the demo)

```bash
# Option A — local
source .venv/bin/activate
uvicorn app.main:app

# Option B — Docker
docker compose up --build
```

Everything below assumes `http://localhost:8000`. Only `curl` and `python3` are needed; `python3 -m json.tool` does the pretty-printing, so `jq` isn't required.

To run the whole demo in one go instead: `./scripts/demo.sh` (set `NO_PAUSE=1` to skip the pauses).

---

## 1. Service is up (~10s)

```bash
curl -s localhost:8000/health | python3 -m json.tool
```

Point out `workflow_version`, `prompt_version` and `model`. These are stamped on every decision.

## 2. The claim arrives (~20s)

```bash
cat data/claims/04_ambiguous_emergency_unknown.json
```

> A $1,840 knee MRI under GOLD_PPO, dated 2026-09-10. There is **no prior authorization**, and `emergency_indicator` is **null**, meaning *unknown*. That is not the same as "no".

## 3. ClaimPilot investigates and refuses to guess (~40s)

```bash
curl -s -X POST localhost:8000/claims/analyze \
  -H 'Content-Type: application/json' \
  -d @data/claims/04_ambiguous_emergency_unknown.json | tee /tmp/first.json | python3 -m json.tool
```

Walk through the response:

| Field | What to say |
|---|---|
| `policy_evidence` | It found **POL-MRI-001 v2.0**: prior auth is required above $1,500. The date of service picked v2.0; v1.0 used $2,000. It also found **POL-EMRG-002**: documented emergencies are exempt. Both excerpts are verbatim and verified against the policy text. |
| `tool_results` | `check_prior_authorization` = **FAIL** (none on file). `check_emergency_exemption` = **INDETERMINATE**, because the exemption *might* apply but the status is unknown. |
| `deterministic_outcome` | **INDETERMINATE**. The rules alone can't decide. |
| `recommendation` | **HUMAN_REVIEW**. The system does not make an autonomous decision. |
| `missing_information` | `["emergency_indicator"]`. This is exactly what the analyst needs to find out. |
| `human_review_reasons` | Every trigger in plain English: missing data, indeterminate rules, low confidence. |

## 4. Decision replay (~30s)

```bash
EXEC_ID=$(python3 -c 'import json; print(json.load(open("/tmp/first.json"))["execution_id"])')
curl -s localhost:8000/executions/$EXEC_ID | python3 -m json.tool
```

> Anyone can reconstruct this decision later: the model, prompt and workflow versions, which policies were retrieved (and at which version), each tool's result, the model's own recommendation, the reasons for escalation, and the path through the graph (`routing_trail`), plus latency, tokens and cost. There is no chain-of-thought, only operational evidence.

## 5. The analyst supplies the missing fact (~30s)

The analyst confirms with the provider that this was an emergency:

```bash
curl -s -X POST localhost:8000/claims/analyze \
  -H 'Content-Type: application/json' \
  -d @data/claims/04b_ambiguous_emergency_confirmed.json | python3 -m json.tool
```

> Same claim, one fact changed. `check_emergency_exemption` is now **PASS** (retrospective authorization is due within 72h), the rules return **PASS**, and the recommendation is **APPROVE**, citing POL-MRI-001 v2.0 and POL-EMRG-002 v1.0.

Optional counter-example: the emergency is *not* confirmed.

```bash
sed 's/"emergency_indicator": true/"emergency_indicator": false/' data/claims/04b_ambiguous_emergency_confirmed.json \
  | curl -s -X POST localhost:8000/claims/analyze -H 'Content-Type: application/json' -d @- \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["recommendation"], "-", d["reasoning_summary"])'
```

→ **DENY**: prior authorization is required and no exemption applies.

## 6. Full audit trail for the claim (~10s)

```bash
curl -s localhost:8000/claims/CLM-92811/audit \
  | python3 -c 'import json,sys; [print(r["execution_id"], r["claim"]["emergency_indicator"], "->", r["recommendation"]) for r in json.load(sys.stdin)]'
```

## 7. Behaviour across many cases (~30s)

```bash
python -m evals.run
```

> 20 labelled cases run through the same workflow. The numbers come from the audit records, not from hardcoded values. The metric to watch is **unsafe autonomous actions = 0**. This run uses a deterministic mock model, so it validates the controls. `--provider openai` measures a real model. The suite also found a real bug, a retrieval-ordering issue, which is described in the README under "What the evaluation found".

---

### If there's time for one question

**"What stops the LLM from just approving things?"** The final APPROVE or DENY comes from the deterministic rules, not from the model. The model can only make an outcome more conservative. `tests/test_evals.py` runs all 20 cases with a model that approves everything at 0.99 confidence, and unsafe autonomous actions stay at 0.
