---
policy_id: POL-ELIG-003
version: "1.0"
title: Member eligibility on date of service
effective_from: 2025-01-01
source: Synthetic Enrollment and Eligibility Standard
applies_to: {}
rule:
  type: active_coverage
---
A claim is payable only if the member has active coverage on the date of service.
The plan on the claim must match the plan on the member enrollment record.
Claims for members without active coverage on the date of service must be denied.
