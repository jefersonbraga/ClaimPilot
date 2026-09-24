---
policy_id: POL-EMRG-002
version: "1.0"
title: Emergency services exemption
effective_from: 2025-01-01
source: Synthetic Member Benefits Handbook, chapter 7
applies_to: {}
rule:
  type: emergency_exemption
  waives: [prior_authorization, network_restriction]
  retro_authorization_hours: 72            # the provider must request retro-authorization within 72h ...
  retro_authorization_for: [prior_authorization]   # ... when the exemption waives prior authorization
---
Services rendered as part of a documented emergency are exempt from prior authorization requirements.
Emergency services are also exempt from network restrictions.
An emergency indicator must be documented on the claim for the exemption to apply.
The provider must request retrospective authorization within 72 hours of the emergency service.
