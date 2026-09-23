---
policy_id: POL-MRI-001
version: "2.0"
title: Knee MRI prior authorization (GOLD_PPO)
effective_from: 2026-01-01
source: Synthetic Utilization Management Manual, section 4.2 (2026 edition)
applies_to:
  plans: [GOLD_PPO]
  procedures: [MRI_KNEE]
rule:
  type: prior_auth_threshold
  amount: 1500
---
MRI knee procedures under GOLD_PPO require prior authorization when the billed amount exceeds $1,500.
Claims at or below $1,500 do not require prior authorization for knee MRI.
Exceptions may apply for emergency cases as defined by the emergency services policy.
