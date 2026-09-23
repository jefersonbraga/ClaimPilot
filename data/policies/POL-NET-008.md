---
policy_id: POL-NET-008
version: "1.0"
title: HMO network restriction
effective_from: 2025-01-01
source: Synthetic Benefit Summary, SILVER_HMO network rules
applies_to:
  plans: [SILVER_HMO]
rule:
  type: network_restriction
---
Services from out-of-network providers are not covered under SILVER_HMO.
Out-of-network services are covered only when rendered as emergency services.
