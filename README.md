# nvb-digital-services

Extension services around the NVB demo core (Statements, Loan Applications, Batch Processing) for an
OpenShift modernization proof of concept. **Synthetic demo data only. No real disbursement, credit bureau,
messaging or production banking.** Conceptually inspired by modernization around a banking core; it does not
reproduce any specific bank's internal architecture.

> Local repository only — no remote has been created and nothing has been pushed or deployed.

## Principles

* The core stays authoritative for balances and financial transactions. Nothing here posts money;
  loan "approval" is an extension workflow state only.
* Services reach the core only through `services/core-adapter` → the core's authenticated `/internal/v1`
  API (disabled by default, see `contracts/core-integration-v1.md`). No core DB access or credentials.
* Money is integer minor units end to end.
* Every API and worker is separately runnable and deployable; images run as arbitrary non-root UIDs.

## Layout

```
services/core-adapter      ✅ M1  stable contract over the core API
services/statements-api    ⏳ M2
services/statements-worker ⏳ M2
services/loans-api         ☐ M4
services/loans-worker      ☐ M5
portal/ gateway/ batch/    ⏳ M2–M3
contracts/                 core integration + adapter contracts
migrations/{statements,loans}
deploy/{local,openshift}   manifests are prepared for review, never applied automatically
tests/{contract,integration,e2e,load}
docs/                      findings, plan, testing
scripts/safe_core_tests.py guarded runner for the core's destructive test fixture
```

See `docs/M1_FINDINGS_AND_DECISIONS.md`, `docs/IMPLEMENTATION_PLAN.md`, `docs/TESTING.md`.
