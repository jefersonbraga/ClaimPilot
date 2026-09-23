---
policy_id: POL-FIN-004
version: "1.0"
title: High-value claim review
effective_from: 2025-01-01
source: Synthetic Payment Integrity Guidelines, section 2
applies_to: {}
rule:
  type: high_value_review
  amount: 10000
---
Claims with a billed amount above $10,000 are classified as high-value claims.
High-value claims require review by a human claims analyst before any payment determination.
Automated approval of high-value claims is not permitted.
