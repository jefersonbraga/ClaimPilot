# Security

ClaimPilot is a public demonstration that uses **synthetic data only**. There is no PHI and no login. Its security posture is designed for what it is: a public, rate-limited, budget-capped demo of an AI workflow. This document maps the controls to OWASP guidance and states the residual risks that were accepted on purpose.

## Reporting a vulnerability

Please open a **private security advisory** on GitHub (repository → *Security* → *Report a vulnerability*). Do not open a public issue.

## HTTP hardening (OWASP Secure Headers Project)

Every response, including rate-limit and size rejections, carries (`app/security.py`):

| Header | Value |
|---|---|
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains` (HTTPS only) |
| `Content-Security-Policy` | App: `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'; upgrade-insecure-requests`. There are no inline scripts or styles and no third parties. |
| | `/docs` only: the Swagger UI needs `cdn.jsdelivr.net` and one inline bootstrap script, so it gets a narrower, route-scoped exception |
| `X-Frame-Options` | `DENY` |
| `X-Content-Type-Options` | `nosniff` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |
| `Permissions-Policy` | camera, microphone, geolocation, payment, USB, sensors and topics disabled |
| `Cross-Origin-Opener-Policy` / `-Resource-Policy` | `same-origin` |
| `Cross-Origin-Embedder-Policy` | `require-corp` (app routes) |
| `Cache-Control` | `no-store` on API responses (they carry per-visitor history) |

The container also runs uvicorn with `--no-server-header`, and the unused `/redoc` UI is disabled.

## OWASP Top 10 (2021)

| Risk | Controls |
|---|---|
| **A01 Broken Access Control** | History is scoped to an anonymous visitor cookie, and only its SHA-256 hash is stored. Visitor B cannot list visitor A's runs (`tests/test_history.py`). A single decision is shareable by its **128-bit random** execution ID. Path parameters are validated against strict patterns. |
| **A02 Cryptographic Failures** | HTTPS enforced by HSTS. The cookie is `HttpOnly`, `Secure` and `SameSite=Lax`. No secrets are in the repository or the image: `.env` is git- and docker-ignored, and platform variables are used in production. |
| **A03 Injection** | SQL is fully parameterized. Every value the UI renders goes through an HTML escaper, backed by a CSP with no inline script. Claim fields are length-bounded. |
| **A04 Insecure Design** | Deterministic rules are authoritative. The LLM can only make an outcome *more* cautious. Uncertain, conflicting or ungrounded cases go to a human. |
| **A05 Security Misconfiguration** | Secure headers (above). Internal errors never reach clients: streamed failures return a generic message, and details are logged server-side. The server banner is suppressed. |
| **A06 Vulnerable Components** | `pip-audit` runs in CI on every push. Security floors are pinned in `requirements.txt`: a 2026 audit found 14 advisories in `starlette` < 1.3.1, fixed by upgrading to FastAPI 0.141 / Starlette 1.7. |
| **A07 Identification & Auth Failures** | No authentication by design (public demo). Visitor cookies are random, 192-bit, and replaced if malformed. |
| **A08 Software & Data Integrity** | CI gates every deploy (tests, evaluation gate, browser tests, Docker health). Railway deploys only after CI passes. |
| **A09 Logging & Monitoring** | Structured JSON log per decision (IDs, outcome, versions, latency, cost). No keys or cookie values are logged. There is a full decision replay per execution. |
| **A10 SSRF** | No user-controlled URLs. Model endpoints come only from server configuration. The per-request `provider` choice is an allowlist of configured providers. |

## OWASP API Security Top 10 (2023)

- **API1 / BOLA:** history endpoints return only the caller's own executions. Execution IDs are unguessable.
- **API4 / Unrestricted resource consumption:**
  - per-IP limit on analyses (10/min);
  - a general per-IP limit on every route (300/min);
  - daily analysis cap and daily LLM budget;
  - `max_tokens` on every model call;
  - 64 KB request-body limit (413; 411 when the length is missing);
  - bounded claim fields.
- **API8 / Misconfiguration:** see the secure headers. Cross-site form posts cannot trigger analyses, because the API accepts only `application/json` bodies (CSRF resistance, tested).

## OWASP Top 10 for LLM Applications (2025)

| Risk | Controls |
|---|---|
| **LLM01 Prompt Injection** | Claim text reaches the model, but the model **cannot act**: the final APPROVE/DENY comes from deterministic rules. Tested by assuming the injection *succeeds*: the model says APPROVE on a failing claim, and the system escalates. |
| **LLM02 Sensitive Information Disclosure** | Synthetic data only. The list view excludes free-text claim fields. The production path (README) calls for PHI minimization and a BAA-covered or in-boundary model. There is also an on-prem option. |
| **LLM05 Improper Output Handling** | Model output must validate against a strict schema, or the case falls back to human review. Citations must be verbatim excerpts of retrieved policies at the version in force. Output is escaped before rendering and never executed. |
| **LLM06 Excessive Agency** | The model has no tools and no side effects. It returns structured JSON, and code decides everything else. |
| **LLM07 System Prompt Leakage** | The system prompt holds no secrets. It is versioned in the repository. |
| **LLM09 Misinformation** | Grounding checks, a confidence threshold, a rule/model agreement check, and human review for anything uncertain. Measured across 21 cases and 3 real models, with 0 unsafe autonomous actions. |
| **LLM10 Unbounded Consumption** | Rate limits, daily analysis cap, daily USD budget, per-call token cap, and small prepaid provider balances. |

## Usage analytics and the admin dashboard

- **First-party only.** No third-party analytics scripts and no IP addresses; the only visitor key is the hashed anonymous cookie. Referrers are reduced to their host, and events older than 400 days are purged.
- **`/admin` does not exist (404) unless `ADMIN_TOKEN` is set with at least 24 characters.** The token is checked in constant time and accepted only as a `Bearer` header, never in the URL. Failed attempts are logged. Responses are `no-store` and `noindex`. The dashboard keeps the token in `sessionStorage` for the current tab only.
- The owner's "don't count this browser" flag is an `HttpOnly`, `SameSite=Strict` cookie, set only by an authenticated call.

## Residual risks (accepted for a public demo)

- **No authentication.** Anyone can run analyses within the limits. Production would use SSO and role-based access (analyst, supervisor, auditor).
- **Client IP comes from `X-Forwarded-For`**, so per-IP limits can be sidestepped by spoofing. The daily caps and budget still bound total spend.
- **Limits are in-memory, per instance.** That is correct for the single-replica deployment. Multiple replicas would need a shared store.
- **`/docs` is public**, with a CSP exception scoped to that route. `/metrics` exposes global aggregates only.
- **The audit store is SQLite on a platform volume**, with no application-level encryption. Production would use a managed database with encryption, retention and access auditing.
- **Share-by-link:** anyone holding an execution ID can view that one decision. It contains synthetic data only.
