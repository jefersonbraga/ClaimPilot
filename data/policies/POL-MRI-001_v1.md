---
policy_id: POL-MRI-001
version: "1.0"
title: Knee MRI prior authorization (GOLD_PPO)
effective_from: 2025-01-01
effective_to: 2025-12-31
source: Synthetic Utilization Management Manual, section 4.2 (2025 edition)
applies_to:
  plans: [GOLD_PPO]
  procedures: [MRI_KNEE]
rule:
  type: prior_auth_threshold
  amount: 2000
---
MRI knee procedures under GOLD_PPO require prior authorization when the billed amount exceeds $2,000.
Claims at or below $2,000 do not require prior authorization for knee MRI.
Exceptions may apply for emergency cases as defined by the emergency services policy.
