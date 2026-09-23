---
policy_id: POL-PT-007
version: "1.0"
title: Physical therapy annual visit limit (SILVER_HMO)
effective_from: 2025-01-01
source: Synthetic Benefit Summary, SILVER_HMO rehabilitation services
applies_to:
  plans: [SILVER_HMO]
  procedures: [PT_VISIT]
rule:
  type: annual_visit_limit
  limit: 20
---
Physical therapy visits under SILVER_HMO are limited to 20 visits per calendar year.
Physical therapy visits beyond the annual limit are not covered and must be denied.
