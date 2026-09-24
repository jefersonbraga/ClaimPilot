# ClaimPilot — Architecture & Design Documentation

This document describes how ClaimPilot is built, using UML and Mermaid diagrams. Every diagram matches the code in this repository; file references are given so each one can be checked.

| # | Diagram | Type | Question it answers |
|---|---|---|---|
| 1 | [System context](#1-system-context) | C4-style context | Who uses ClaimPilot and what does it depend on? |
| 2 | [Component view](#2-component-view) | UML component (flowchart) | What are the modules and how do they depend on each other? |
| 3 | [Domain model](#3-domain-model-uml-class-diagram) | UML class | What are the data contracts? |
| 4 | [Service classes](#4-service-classes-uml-class-diagram) | UML class | Which classes do the work, and through which interfaces? |
| 5 | [Workflow state machine](#5-workflow-state-machine-langgraph) | UML state | What path can a claim take? |
| 6 | [Analyze request](#6-analyze-request-uml-sequence) | UML sequence | What happens during `POST /claims/analyze`? |
| 7 | [Rule engine](#7-deterministic-rule-engine-uml-activity) | UML activity | How is the deterministic verdict computed? |
| 8 | [Escalation decision](#8-guardrails--escalation-decision-uml-activity) | UML activity | Why does a case go to a human? |
| 9 | [Ambiguous claim lifecycle](#9-ambiguous-claim-lifecycle-uml-sequence) | UML sequence | What happens when an analyst supplies the missing data? |
| 10 | [Decision replay](#10-decision-replay-uml-sequence) | UML sequence | How is a past decision reconstructed? |
| 11 | [Policy versioning](#11-policy-versioning-timeline) | Gantt | Which policy version applies on which date? |
| 12 | [Data model](#12-data-model-er) | ER | How are policies, reference data and audit records structured? |
| 13 | [Deployment](#13-deployment-view) | UML deployment (flowchart) | How does it run, today and in production? |
| 14 | [Evaluation pipeline](#14-evaluation-pipeline) | Flowchart | How are the quality metrics produced? |

---

## 1. System context

ClaimPilot sits **beside** the claims adjudication engine, not in place of it. It only sees the exceptions the engine could not resolve.

```mermaid
flowchart LR
    analyst(["👤 Claims analyst"])
    engine["Claims adjudication engine<br/><i>existing, deterministic</i>"]
    subgraph boundary["ClaimPilot (this project)"]
        cp["ClaimPilot<br/>AI-assisted exception investigation"]
    end
    llm["LLM provider / model gateway<br/><i>OpenAI-compatible</i>"]
    sor["Systems of record<br/><i>enrollment · authorizations · claims history</i><br/>(synthetic JSON in the MVP)"]
    pol["Policy repository<br/><i>versioned documents</i><br/>(Markdown in the MVP)"]
    audit[("Decision audit store")]

    engine -- "exception claims<br/>(long tail)" --> cp
    analyst -- "investigate, supply missing info,<br/>replay decisions" --> cp
    cp -- "recommendation + evidence<br/>or HUMAN_REVIEW + reasons" --> analyst
    cp -- "structured prompt<br/>(no chain-of-thought returned)" --> llm
    cp -- "deterministic lookups" --> sor
    cp -- "retrieve policies in force<br/>on date of service" --> pol
    cp -- "append execution record" --> audit
```

---

## 2. Component view

Arrows show *uses / depends on*. The workflow is the only component that knows the order of steps. Tools, retrieval and the LLM client don't depend on one another.

```mermaid
flowchart TB
    subgraph api["API layer — app/main.py"]
        routes["FastAPI routes<br/>/claims/analyze · /claims/{id}/audit<br/>/executions/{id} · /metrics · /health"]
        ui["Demo workbench — app/static/<br/>plain HTML/CSS/JS at /<br/>(calls the public routes only)"]
    end

    subgraph wf["Orchestration — app/workflows/"]
        wfgraph["claim_graph.py<br/>LangGraph StateGraph<br/>ClaimInvestigator facade"]
        guard["guardrails.py<br/>grounding · conflicts · risk · escalation"]
    end

    subgraph det["Deterministic layer"]
        tools["tools/claim_tools.py<br/>eligibility · history · PA ·<br/>emergency exemption · network · financial"]
        store["retrieval/policy_store.py<br/>versioned PolicyStore + TF-IDF"]
    end

    subgraph ai["AI layer — app/llm/"]
        client["client.py<br/>LLMClient protocol<br/>OpenAICompatibleLLM · MockLLM"]
        prompts["prompts.py<br/>PROMPT_VERSION + templates"]
    end

    subgraph obs["Observability — app/observability/"]
        audit["audit.py<br/>AuditStore (SQLite) + JSON log"]
    end

    models["models/domain.py<br/>Pydantic contracts"]
    config["config.py<br/>Settings · WORKFLOW_VERSION"]
    data[("data/<br/>policies · members ·<br/>prior_auths · claim_history")]
    evals["evals/run.py<br/>evaluation harness"]

    ui -.->|fetch| routes
    routes --> wfgraph
    routes --> audit
    evals --> wfgraph
    evals --> audit
    wfgraph --> guard
    wfgraph --> tools
    wfgraph --> store
    wfgraph --> client
    wfgraph --> prompts
    wfgraph --> audit
    tools --> data
    store --> data
    client --> prompts

    routes -.-> models
    wfgraph -.-> models
    guard -.-> models
    tools -.-> models
    client -.-> models
    wfgraph -.-> config
    client -.-> config
```

---

## 3. Domain model (UML class diagram)

The Pydantic contracts in `app/models/domain.py`. `ClaimDecision` is what the API returns. `ExecutionRecord` is what the audit stores, and it is a superset that includes versions, the routing trail and the LLM's own opinion.

```mermaid
classDiagram
    direction LR

    class Claim {
        +str claim_id
        +str? member_id
        +str? provider
        +str? procedure
        +float? amount
        +str? diagnosis_code
        +str? plan
        +bool? prior_authorization
        +date? date_of_service
        +bool? emergency_indicator
        +bool? retro_auth_requested
        +IN|OUT? provider_network
        +str? region
    }
    note for Claim "Business-required fields are Optional:\nmissing data is a business outcome (escalate),\nnot an HTTP 422.\nemergency_indicator = None means UNKNOWN."

    class Policy {
        +str policy_id
        +str version
        +str title
        +date effective_from
        +date? effective_to
        +str source
        +dict applies_to
        +dict? rule
        +str content
        +ref() str
        +is_effective_on(date) bool
    }

    class RetrievedPolicy {
        +str policy_id
        +str version
        +str title
        +float score
        +str content
    }

    class PolicyEvidence {
        +str policy_id
        +str version
        +str excerpt
    }

    class ToolResult {
        +str tool
        +RuleOutcome outcome
        +str detail
        +dict data
        +list~str~ policy_refs
    }

    class Citation {
        +str policy_id
        +str excerpt
    }

    class LLMAnalysis {
        +Recommendation recommendation
        +float confidence
        +str reasoning_summary
        +list~Citation~ citations
        +list~str~ missing_information
    }

    class LLMUsage {
        +str model
        +str provider
        +int input_tokens
        +int output_tokens
        +float latency_ms
        +float estimated_cost
    }

    class ClaimDecision {
        +str execution_id
        +str claim_id
        +Recommendation recommendation
        +float confidence
        +RiskLevel risk_level
        +str reasoning_summary
        +list~PolicyEvidence~ policy_evidence
        +list~ToolResult~ tool_results
        +RuleOutcome deterministic_outcome
        +bool human_review_required
        +list~str~ human_review_reasons
        +list~str~ missing_information
        +int estimated_tokens
        +float estimated_cost
        +float latency_ms
    }

    class ExecutionRecord {
        +str execution_id
        +str claim_id
        +datetime timestamp
        +str model
        +str llm_provider
        +str prompt_version
        +str workflow_version
        +Claim claim
        +list~str~ retrieved_policy_ids
        +dict retrieved_policy_versions
        +list~str~ supplemental_policy_ids
        +list~str~ retrieval_queries
        +list~PolicyEvidence~ policy_evidence
        +bool evidence_grounded
        +list~str~ grounding_failures
        +list~str~ tools_executed
        +list~str~ tool_summary
        +list~ToolResult~ tool_results
        +RuleOutcome deterministic_outcome
        +Recommendation? llm_recommendation
        +str? llm_error
        +float confidence
        +RiskLevel risk_level
        +list~str~ risk_factors
        +Recommendation recommendation
        +str reasoning_summary
        +bool human_review_required
        +list~str~ human_review_reason
        +list~str~ missing_information
        +list~str~ routing_trail
        +float latency_ms
        +float llm_latency_ms
        +int input_tokens
        +int output_tokens
        +float estimated_cost
    }

    class Recommendation {
        <<enumeration>>
        APPROVE
        DENY
        HUMAN_REVIEW
    }
    class RiskLevel {
        <<enumeration>>
        LOW
        MEDIUM
        HIGH
    }
    class RuleOutcome {
        <<enumeration>>
        PASS
        FAIL
        INDETERMINATE
    }

    Policy ..> RetrievedPolicy : projected by retrieval
    RetrievedPolicy ..> PolicyEvidence : verified citation becomes
    LLMAnalysis "1" *-- "0..*" Citation
    Citation ..> PolicyEvidence : grounded → evidence
    ToolResult --> RuleOutcome
    LLMAnalysis --> Recommendation
    ClaimDecision "1" *-- "0..*" PolicyEvidence
    ClaimDecision "1" *-- "0..*" ToolResult
    ClaimDecision --> Recommendation
    ClaimDecision --> RiskLevel
    ExecutionRecord "1" *-- "1" Claim
    ExecutionRecord "1" *-- "0..*" ToolResult
    ExecutionRecord "1" *-- "0..*" PolicyEvidence
    ExecutionRecord ..> ClaimDecision : superset of
    ExecutionRecord ..> LLMUsage : tokens / cost / latency from
```

---

## 4. Service classes (UML class diagram)

The behaviour-carrying classes. Dependencies are injected into `build_graph` (LLM, policy store, audit store, settings). That injection is what lets tests swap in `MockLLM` or `ScriptedLLM` and an in-memory audit store without mocking frameworks.

```mermaid
classDiagram
    direction TB

    class LLMClient {
        <<interface>>
        +str provider
        +str model
        +complete(system, user) tuple~str,int,int~
    }
    class OpenAICompatibleLLM {
        +str provider
        +str model
        -OpenAI _client
        +complete(system, user)
    }
    class MockLLM {
        +provider = "mock"
        +model = "mock-analyst-v1"
        +complete(system, user)
    }
    class ScriptedLLM {
        <<test double>>
        +provider = "scripted"
        +str response
        +complete(system, user)
    }
    LLMClient <|.. OpenAICompatibleLLM
    LLMClient <|.. MockLLM
    LLMClient <|.. ScriptedLLM

    class PolicyStore {
        +list~Policy~ policies
        -dict _idf
        -list _doc_vecs
        +effective_policies(on) list~Policy~
        +applicable_policies(claim, region) list~Policy~
        +retrieve(query, claim, top_k, region, min_score) list~RetrievedPolicy~
        +get(policy_id, version) Policy?
    }

    class AuditStore {
        -Connection _conn
        -Lock _lock
        +save(ExecutionRecord)
        +get(execution_id) ExecutionRecord?
        +for_claim(claim_id) list~ExecutionRecord~
        +all() list~ExecutionRecord~
    }

    class ClaimInvestigator {
        +LLMClient llm
        +CompiledStateGraph graph
        +investigate(Claim) ClaimDecision
    }

    class RiskAssessment {
        <<dataclass>>
        +RiskLevel risk_level
        +list~str~ risk_factors
        +list~str~ review_reasons
        +list~PolicyEvidence~ evidence
        +bool grounded
        +list~str~ grounding_problems
    }

    class Settings {
        <<frozen dataclass>>
        +str llm_provider
        +str llm_model
        +str? llm_base_url
        +str? llm_api_key
        +__post_init__() resolves provider profile
        +float price_input_per_1k
        +float price_output_per_1k
        +float confidence_threshold
        +int retrieval_top_k
        +int supplemental_top_k
        +float supplemental_min_score
        +str audit_db_path
        +resolved_provider() str
    }

    class claim_tools {
        <<module>>
        +check_member_eligibility(claim, policies) ToolResult
        +get_claim_history(claim, policies) ToolResult
        +check_prior_authorization(claim, policies) ToolResult
        +check_emergency_exemption(claim, policies, waives) ToolResult
        +check_network(claim, policies) ToolResult
        +calculate_financial_threshold(claim, policies) ToolResult
    }

    class guardrails {
        <<module>>
        +EXPECTED_FROM_RULES dict
        +RISK_SIGNALS dict
        +risk_factors(tool_results) list~str~
        +findings(tool_results) list~ToolResult~
        +ground_citations(analysis, retrieved)
        +aggregate_rule_outcome(outcomes) RuleOutcome
        +evaluate(...) RiskAssessment
    }

    ClaimInvestigator o-- LLMClient
    ClaimInvestigator ..> PolicyStore : injected into graph
    ClaimInvestigator ..> AuditStore : injected into graph
    ClaimInvestigator ..> Settings
    ClaimInvestigator ..> claim_tools : nodes call
    ClaimInvestigator ..> guardrails : nodes call
    guardrails ..> RiskAssessment : creates
    claim_tools ..> PolicyStore : receives applicable policies from
```

---

## 5. Workflow state machine (LangGraph)

`app/workflows/claim_graph.py` (`WORKFLOW_VERSION = claim-investigation-wf/1.2.0`). There are two decision points, and both are pure functions of the state:
- `after_validation` short-circuits to a human when the claim can't even be identified;
- `human_review_router` sends the claim to a human if **any** guardrail produced a reason.

```mermaid
stateDiagram-v2
    [*] --> validate_claim

    validate_claim --> identity_check
    state identity_check <<choice>>
    identity_check --> escalate_to_human : member_id or procedure missing
    identity_check --> retrieve_policy : identifiable

    retrieve_policy --> check_eligibility
    check_eligibility --> check_authorization
    check_authorization --> refine_retrieval
    refine_retrieval --> analyze_claim
    analyze_claim --> evaluate_risk

    evaluate_risk --> human_review_router
    state human_review_router <<choice>>
    human_review_router --> generate_recommendation : review_reasons is empty
    human_review_router --> escalate_to_human : one or more review_reasons

    generate_recommendation --> record_audit
    escalate_to_human --> record_audit
    record_audit --> [*]

    note right of retrieve_policy
        metadata filter (plan, procedure,
        region, date of service)
        then TF-IDF ranking, top-k
    end note
    note right of check_authorization
        produces the authoritative
        rule verdict PASS / FAIL / INDETERMINATE
    end note
    note left of refine_retrieval
        2nd retrieval pass, query built from
        tool findings (non-PASS results and
        risk signals); skipped when none
    end note
    note right of analyze_claim
        the ONLY non-deterministic step
        (one LLM call, structured JSON)
    end note
    note left of generate_recommendation
        APPROVE if rules PASS
        DENY if rules FAIL
        (only reachable when the model agrees)
    end note
```

### State that flows through the graph (`ClaimState`)

Fields marked ⊕ use an additive reducer: each node *appends* to them, so the routing trail and tool results build up across nodes.

```mermaid
flowchart LR
    s0["execution_id · started_at · claim"] --> s1["missing_fields"]
    s1 --> s2["region · applicable_policy_refs · retrieved · ⊕ retrieval_queries"]
    s2 --> s3["⊕ tool_results · ⊕ effective_outcomes · rule_outcome"]
    s3 --> s3b["retrieved (+ supplemental) · supplemental_policy_ids · ⊕ retrieval_queries"]
    s3b --> s4["analysis · llm_usage · llm_error"]
    s4 --> s5["risk (RiskAssessment)"]
    s5 --> s6["decision (ClaimDecision)"]
    trail["⊕ routing_trail — appended by every node"] -.-> s6
```

---

## 6. Analyze request (UML sequence)

A complete `POST /claims/analyze` for a claim that passes all guardrails.

```mermaid
sequenceDiagram
    autonumber
    actor Analyst
    participant API as FastAPI<br/>main.py
    participant INV as ClaimInvestigator
    participant G as LangGraph
    participant PS as PolicyStore
    participant T as claim_tools
    participant LLM as LLMClient
    participant GR as guardrails
    participant AU as AuditStore

    Analyst->>API: POST /claims/analyze (Claim JSON)
    API->>API: Pydantic validation (422 on malformed types)
    API->>INV: investigate(claim)
    INV->>G: invoke(initial state + execution_id)

    G->>G: validate_claim → missing_fields
    G->>T: find_member(member_id) → region
    G->>PS: applicable_policies(claim, region)
    G->>PS: retrieve(build_query(claim), top_k)
    PS-->>G: RetrievedPolicy[] (with versions)

    G->>T: check_member_eligibility / get_claim_history
    T-->>G: ToolResult[] (+ policy_refs POLICY@version)
    G->>T: check_prior_authorization / check_network
    opt a requirement FAILs
        G->>T: check_emergency_exemption(waives=…)
    end
    G->>T: calculate_financial_threshold
    G->>GR: aggregate_rule_outcome → PASS / FAIL / INDETERMINATE

    G->>GR: findings(tool_results)
    opt tools surfaced findings (non-PASS or risk signal)
        G->>PS: retrieve(query from finding details, top_k=2, min_score=0.15)
        PS-->>G: supplemental policies (appended if new)
    end

    G->>LLM: complete(SYSTEM_PROMPT, claim + policies + tool results + verdict)
    LLM-->>G: raw JSON + token counts
    G->>G: parse_analysis → LLMAnalysis (or llm_error)

    G->>GR: evaluate(rule_outcome, tools, analysis, retrieved, threshold)
    GR->>GR: ground_citations (verbatim excerpt check)
    GR-->>G: RiskAssessment(review_reasons = [])

    G->>G: human_review_router → generate_recommendation
    G->>AU: save(ExecutionRecord)
    G->>G: log_event("claim_investigated", …)
    G-->>INV: final state
    INV-->>API: ClaimDecision
    API-->>Analyst: 200 ClaimDecision (evidence, tool results, no chain-of-thought)
```

---

### Live progress (same endpoint, streamed)

When the client sends `Accept: application/x-ndjson`, the route runs the same graph with `graph.stream(..., stream_mode="updates")`. It emits one line per completed node, then the decision. The demo UI uses this to light up each step as it really completes. Every other client still gets the single JSON `ClaimDecision`.

```mermaid
sequenceDiagram
    participant UI as Workbench UI
    participant API as POST /claims/analyze
    participant G as LangGraph (graph.stream)

    UI->>API: claim + Accept: application/x-ndjson
    API->>G: stream(initial state, stream_mode="updates")
    loop each completed node
        G-->>API: {node: delta}
        API-->>UI: {"event":"node","node":"retrieve_policy","trail":"…"}
        UI->>UI: mark step done, pulse the next one
    end
    API-->>UI: {"event":"decision","decision":{…}}
    UI->>API: GET /executions/{id}  (recorded path for replay)
```

---

## 7. Deterministic rule engine (UML activity)

What `check_eligibility` and `check_authorization` do together. The key design choices:
- **unknown is not the same as no**;
- **disagreeing policies stop the automation**;
- **an exemption's outcome replaces the failed requirement**.

```mermaid
flowchart TD
    start((start)) --> elig{Member active on<br/>date of service?}
    elig -- "not found / data missing" --> eI[INDETERMINATE]
    elig -- "no" --> eF[FAIL]
    elig -- "yes, plan mismatch" --> eI
    elig -- "yes" --> eP[PASS]

    eP & eF & eI --> hist[get_claim_history]
    hist --> lim{Visit limit rule<br/>applies and exhausted?}
    lim -- yes --> hF[FAIL]
    lim -- no --> hP["PASS<br/>(duplicates flagged as risk, not failure)"]

    hF & hP --> pa{Procedure exempt from PA?<br/>POL-LAB-009}
    pa -- yes --> paP[PASS]
    pa -- no --> thr{Applicable PA threshold<br/>rules agree?}
    thr -- "no (e.g. MRI-001 v2 $1,500<br/>vs IMG-010 $1,000)" --> paC["INDETERMINATE<br/>policy_conflict"]
    thr -- yes --> req{Amount over threshold?}
    req -- no --> paP
    req -- yes --> auth{Valid PA on file?}
    auth -- yes --> paP
    auth -- "claim says yes,<br/>no record" --> paI[INDETERMINATE]
    auth -- no --> paF[FAIL]

    paF --> ex{emergency_indicator}
    ex -- "true" --> rt{"retro-auth requested<br/>within 72h?<br/>(POL-EMRG-002 rule)"}
    rt -- "true" --> exP["PASS (waived)"]
    rt -- "false" --> exF
    rt -- "null (unknown)" --> exR["INDETERMINATE<br/>missing: retro_auth_requested"]
    ex -- "false" --> exF[FAIL]
    ex -- "null (unknown)" --> exI["INDETERMINATE<br/>missing: emergency_indicator"]

    paP & paC & paI & exP & exF & exI & exR --> net["check_network<br/>(same exemption pattern for HMO out-of-network)"]
    net --> fin["calculate_financial_threshold<br/>(high value → risk signal)"]
    fin --> agg{aggregate effective outcomes}
    agg -- "any FAIL" --> vF([rules = FAIL])
    agg -- "else any INDETERMINATE" --> vI([rules = INDETERMINATE])
    agg -- "all PASS" --> vP([rules = PASS])
```

---

## 8. Guardrails & escalation decision (UML activity)

`guardrails.evaluate()` doesn't stop at the first trigger. It collects **every** reason, so the analyst sees the full picture. Routing then comes down to one question: *is the list empty?* A final safety invariant is defence in depth: even if a future change forgot to add a reason, a HIGH-risk, INDETERMINATE or ungrounded case can never become an autonomous decision. `tests/test_guardrails.py::test_safety_matrix_no_combination_lets_the_model_override_rules` checks every combination.

```mermaid
flowchart TD
    in([rule_outcome + tool_results + LLMAnalysis]) --> r1

    r1{Missing data?<br/>claim fields · tool-detected · model-detected} -- yes --> a1["+ 'Required information is missing: …'"]
    r1 -- no --> r2
    a1 --> r2

    r2{Rule verdict INDETERMINATE?<br/>or tool INDETERMINATE not explained<br/>by missing data?} -- yes --> a2["+ 'Deterministic rule verdict is INDETERMINATE'<br/>+ '… could not reach a deterministic result'"]
    r2 -- no --> r3
    a2 --> r3

    r3{Risk factors?<br/>high value · duplicate · policy conflict} -- yes --> a3["+ 'HIGH risk: …'"]
    r3 -- no --> r4
    a3 --> r4

    r4{LLM output valid?} -- no --> a4["+ 'LLM analysis unavailable or invalid; falling back…'"]
    r4 -- yes --> r5
    a4 --> done

    r5{Every citation retrieved,<br/>≥ 5 words AND verbatim in policy text?} -- no --> a5["+ 'Ungrounded citation (possible hallucination)'"]
    r5 -- yes --> r6
    a5 --> r6

    r6{At least one verified citation?} -- no --> a6["+ 'not supported by any verifiable citation'"]
    r6 -- yes --> r7
    a6 --> r7

    r7{confidence ≥ 0.80?} -- no --> a7["+ 'Model confidence X below threshold'"]
    r7 -- yes --> r8
    a7 --> r8

    r8{LLM recommendation vs rules} -- "LLM = HUMAN_REVIEW,<br/>rules decisive" --> a8["+ 'model requested review (honored)'"]
    r8 -- "LLM contradicts rules" --> a9["+ 'model cannot override business rules'"]
    r8 -- agree --> done
    a8 --> done
    a9 --> done

    done{review_reasons empty?}
    done -- yes --> inv{"Safety invariant:<br/>HIGH risk, INDETERMINATE<br/>or not grounded?"}
    inv -- yes --> ai["+ 'Safety invariant: autonomous decision blocked'"] --> hr
    inv -- no --> ok(["generate_recommendation<br/>APPROVE / DENY from rules"])
    done -- no --> hr(["escalate_to_human<br/>HUMAN_REVIEW + all reasons"])

    style a9 fill:#fde2e2,stroke:#c0392b
    style a5 fill:#fde2e2,stroke:#c0392b
    style hr fill:#fff4d6,stroke:#b7791f
    style ok fill:#e3f6e8,stroke:#2f855a
```

### Risk level derivation

```mermaid
flowchart LR
    x{any risk factor?} -- yes --> H[HIGH]
    x -- no --> y{any review reason<br/>or rules ≠ PASS?}
    y -- yes --> M[MEDIUM]
    y -- no --> L[LOW]
```

---

## 9. Ambiguous claim lifecycle (UML sequence)

This is the scenario the demo is built around. The system refuses to guess and names the exact missing fact. Once the analyst supplies it, the rerun produces a different decision with its own evidence. Each run is a separate, replayable execution.

```mermaid
sequenceDiagram
    autonumber
    actor A as Analyst
    participant CP as ClaimPilot
    participant T as Rule engine
    participant L as LLM
    participant AU as Audit

    A->>CP: CLM-92811 MRI_KNEE $1,840, no PA, emergency_indicator = null
    CP->>T: PA threshold (POL-MRI-001 v2.0 → $1,500)
    T-->>CP: FAIL — PA required, none on file
    CP->>T: emergency exemption (POL-EMRG-002 v1.0)
    T-->>CP: INDETERMINATE — missing emergency_indicator
    Note over CP: refine_retrieval: findings = PA FAIL + exemption INDETERMINATE<br/>(both policies already retrieved — nothing added)
    CP->>L: analyze (verdict = INDETERMINATE)
    L-->>CP: HUMAN_REVIEW, confidence 0.55, cites MRI-001 + EMRG-002
    CP->>AU: EXE-1 (HUMAN_REVIEW, reasons, evidence)
    CP-->>A: HUMAN_REVIEW — "Required information is missing: emergency_indicator"

    Note over A: Analyst checks the ER record with the provider

    alt emergency confirmed
        A->>CP: same claim, emergency_indicator = true, retro_auth_requested = true
        CP->>T: exemption → PASS (emergency documented, retro-auth requested)
        CP->>L: analyze (verdict = PASS)
        L-->>CP: APPROVE 0.92, cites MRI-001 v2.0 + EMRG-002 v1.0
        CP->>AU: EXE-2 (APPROVE)
        CP-->>A: APPROVE with evidence
    else not an emergency
        A->>CP: same claim, emergency_indicator = false
        CP->>T: exemption → FAIL
        CP->>L: analyze (verdict = FAIL)
        L-->>CP: DENY 0.90, cites MRI-001 v2.0
        CP->>AU: EXE-2 (DENY)
        CP-->>A: DENY with evidence
    end

    A->>CP: GET /claims/CLM-92811/audit
    CP-->>A: [EXE-1 HUMAN_REVIEW, EXE-2 APPROVE|DENY]
```

---

## 10. Decision replay (UML sequence)

`GET /executions/{id}` answers *"how was this decision made?"* without exposing chain-of-thought. The replay returns only what the system actually used: evidence, tool results, versions and routing.

```mermaid
sequenceDiagram
    actor Auditor
    participant API as FastAPI
    participant AU as AuditStore (SQLite)

    Auditor->>API: GET /executions/EXE-84bd08932ca2
    API->>AU: get(execution_id)
    AU-->>API: ExecutionRecord (JSON)
    API-->>Auditor: 200 replay

    Note right of Auditor: What the replay contains
    Note right of Auditor: • outcome first: recommendation · review reasons · missing info · risk · confidence<br/>• model · prompt_version · workflow_version<br/>• rule verdict vs LLM recommendation<br/>• verified evidence · grounding failures<br/>• retrieved policies + exact versions · which came from the 2nd pass · queries used<br/>• tool_summary (one line per tool) + full tool results<br/>• routing_trail · latency · tokens · cost · claim snapshot
```

---

## 11. Policy versioning timeline

Policies are chosen by the **date of service**, not the date of analysis. A $1,840 knee MRI needs no PA in October 2025 (v1.0, threshold $2,000) but does need one in September 2026 (v2.0, threshold $1,500). The regional addendum overlaps v2.0 from March 2026, which creates the deliberate conflict case.

```mermaid
gantt
    title Synthetic policy effective periods
    dateFormat YYYY-MM-DD
    axisFormat %b %Y

    section Knee MRI (GOLD_PPO)
    POL-MRI-001 v1.0 — PA over $2,000      :done,   mri1, 2025-01-01, 2025-12-31
    POL-MRI-001 v2.0 — PA over $1,500      :active, mri2, 2026-01-01, 2026-12-31

    section Regional addendum (NORTHEAST)
    POL-IMG-010 v1.0 — PA over $1,000      :crit,   img,  2026-03-01, 2026-12-31

    section Cross-cutting
    POL-EMRG-002 emergency exemption       :        emrg, 2025-01-01, 2026-12-31
    POL-ELIG-003 eligibility               :        elig, 2025-01-01, 2026-12-31
    POL-FIN-004 high-value review          :        fin,  2025-01-01, 2026-12-31
```

*Open-ended policies (no `effective_to`) are drawn to the end of 2026 for readability.*

---

## 12. Data model (ER)

The policy file is the single source of truth for **both** audiences. The prose goes to the LLM, and the `rule` block goes to the deterministic tools. The audit table stores the full record as JSON, indexed by claim.

```mermaid
erDiagram
    POLICY {
        string policy_id PK
        string version PK
        string title
        date effective_from
        date effective_to "nullable = open-ended"
        string source
        json applies_to "plans / procedures / regions; empty = all"
        json rule "type + params, e.g. prior_auth_threshold amount 1500"
        text content "prose sent to the LLM"
    }
    MEMBER {
        string member_id PK
        string plan
        string status
        date coverage_start
        date coverage_end
        string region
    }
    PRIOR_AUTH {
        string auth_id PK
        string member_id FK
        string procedure
        string status
        date valid_from
        date valid_to
    }
    CLAIM_HISTORY {
        string claim_id PK
        string member_id FK
        string procedure
        date date_of_service
        float amount
        string status
        int units "visits represented"
    }
    EXECUTIONS {
        string execution_id PK
        string claim_id "indexed"
        string timestamp
        json record "full ExecutionRecord"
        string visitor "hash of anonymous cookie (history scope)"
    }

    MEMBER ||--o{ PRIOR_AUTH : "holds"
    MEMBER ||--o{ CLAIM_HISTORY : "has"
    MEMBER ||--o{ EXECUTIONS : "claims investigated"
    POLICY }o--o{ EXECUTIONS : "retrieved / cited (policy_id@version)"
```

---

## 13. Deployment view

### MVP (as built)

```mermaid
flowchart LR
    subgraph host["Developer machine / any Docker host"]
        subgraph c["container: claimpilot (python:3.12-slim, non-root)"]
            uv["uvicorn :8000"] --> app["FastAPI + LangGraph"]
            app --> files[/"/app/data (baked-in policies + reference JSON)"/]
        end
        vol[("volume: audit-data<br/>/data/claimpilot.db")]
        app --> vol
    end
    user(["curl / Swagger UI"]) -->|HTTP :8000| uv
    app -.->|"HTTPS, optional<br/>(LLM_API_KEY set)"| llm["OpenAI-compatible endpoint"]
    app -.->|stdout JSON logs| logs[/"docker logs"/]
```

### Target production shape (not built — see README "What I would change for production")

```mermaid
flowchart LR
    analyst(["Analyst UI / work queue"]) -->|SSO / OIDC| gw["API gateway<br/>authn · RBAC · rate limits"]
    engine["Claims engine<br/>exception feed"] -->|mTLS| gw
    gw --> svc["ClaimPilot service<br/>(N replicas)"]
    svc --> q[["Queue / async workers"]]
    svc --> mg["Enterprise model gateway<br/>PHI filtering · cost · kill switch"]
    mg --> model["LLM (BAA / in-boundary)"]
    svc --> pg[("Postgres + pgvector<br/>governed policy repo")]
    svc --> sor["Systems of record APIs"]
    svc --> worm[("Immutable audit store<br/>retention · legal hold")]
    svc --> obs["Metrics · traces · model monitoring"]
    kms["KMS / secrets manager"] -.-> svc
```

---

## 14. Evaluation pipeline

`python -m evals.run` runs each case through the **same** workflow the API uses, then reads the audit record back. Metrics are computed from what the system actually recorded, not from what the harness thinks it sent. `--provider deepseek|groq|openai` runs the identical pipeline against a real model and refuses to run without that provider's key.

```mermaid
flowchart LR
    cases[("evals/cases.json<br/>21 labelled cases")] --> loop{{for each case}}
    loop --> inv["ClaimInvestigator.investigate()<br/>(real graph, chosen provider)"]
    inv --> rec[("in-memory AuditStore<br/>ExecutionRecord")]
    rec --> cmp["compare with labels"]
    cases -. expected_recommendation<br/>expected_policy_ids .-> cmp

    cmp --> m1["recommendation accuracy"]
    cmp --> m2["policy retrieval rate<br/>(expected ⊆ retrieved)"]
    cmp --> m3["grounded response rate"]
    cmp --> m4["correct escalation rate"]
    cmp --> m5["unsafe autonomous actions<br/>(release gate: must be 0)"]
    cmp --> m6["invalid structured outputs"]
    cmp --> m7["latency avg · p50 · p95<br/>tokens in/out · cost per claim"]

    m1 & m2 & m3 & m4 & m5 & m6 & m7 --> out["console report + per-case diagnostics<br/>+ evals/results/latest.json"]
```
