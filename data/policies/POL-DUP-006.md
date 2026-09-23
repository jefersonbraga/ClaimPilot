---
policy_id: POL-DUP-006
version: "1.0"
title: Potential duplicate claims
effective_from: 2025-01-01
source: Synthetic Payment Integrity Guidelines, section 5
applies_to: {}
rule:
  type: duplicate_window
  days: 30
---
A claim for the same member and the same procedure within 30 days of a previously paid claim is a potential duplicate.
Potential duplicate claims must be reviewed by a human analyst before payment.
