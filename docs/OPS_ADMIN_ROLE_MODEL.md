# Operations Admin role model (Loans + Statements)

> PoC role model for a synthetic demo bank. It is **not** a claim of production banking
> compliance. Nothing in this document has been executed against any live environment.

## Summary

* The separate **Reviewer** workspace and accounts are retired. The **Operations Admin** reviews
  loan applications and takes the final decision.
* "Approved" remains a **workflow decision only**: no money is disbursed, no Core loan is
  created, no balance changes (unchanged code path; tested).
* Identity still comes only from the Core session via the Core Adapter (introspection on every
  request, CSRF for unsafe methods). No role is ever taken from the browser.
* No schema change and no migration: `operations_admin` is a new **value** in the existing
  `role_assignments` table (no CHECK constraint on `role`).

## Who can do what

`operations_admin` is effective **only** when both hold: the trusted Core role is `admin`
(introspection) **and** an explicit, audited `operations_admin` assignment exists in that
service's database. A Core admin without the assignment gets nothing; a customer who somehow
holds the row gets nothing.

| Capability | Customer | Operations Admin | batch_operator (kept) | extension_admin (kept) | reviewer / approver (retired) |
| --- | --- | --- | --- | --- | --- |
| Own loan drafts, submit, view own | ✅ | — | — | — | — |
| Own statements: request, cancel, download own PDF | ✅ | — | — | — | — |
| Loan review queue, details, history, stats | — | ✅ all non-draft | — | queue view only (as before) | ❌ (403) |
| Start review (`start-review`) | — | ✅ not own | — | — | ❌ |
| Record review recommendation | — | ✅ on a review they started, not own | — | — | ❌ (403) |
| Final decision (approve / reject, reason ≥ 10 chars) | — | ✅ after a recommendation, not own | — | — | ❌ (403) |
| Assign reviewer / list reviewers | — | — | — | 410 `reviewer_workflow_retired` | ❌ |
| Statement operations view (`/ops/summary`, `/ops/requests`) | — | ✅ read-only, no PDFs | — | — | — |
| Statement / loan-summary batches (create, retry) | — | only if also granted `batch_operator` | ✅ | — | — |
| Extension role administration | — | — | — | ✅ | — |
| Read another customer's statement PDF | ❌ | ❌ | ❌ | ❌ | ❌ |

The rows for `reviewer` / `approver` apply while `LOANS_LEGACY_REVIEWER_WORKFLOW_ENABLED=false`
(the default). With `true`, they behave exactly as before this change (rollback switch).

## Separation of duties (PoC simplification)

The owner explicitly asked for one Operations Admin role to review **and** approve. Therefore
an Operations Admin may decide an application whose review they recorded. This removes the
four-eyes check of the old workflow and is acceptable only for this PoC.

Still enforced:
* nobody can start a review, recommend or decide on **their own** application (`409
  conflict_of_interest` / `separation_of_duties`);
* a recommendation is required before a decision (`recommendation_required`);
* every transition is a version-checked conditional update: stale or concurrent actions get
  `409 version_conflict`, a second decision on a decided application gets `409 invalid_state`;
* the legacy approver (rollback mode) keeps the original reviewer ≠ decider rule.

## Existing data and reviewer identities

* Nothing is deleted or rewritten: applications, recommendations, `reviewer_subject`,
  `loan_events` and `role_events` remain readable. The detail view shows the event `details`
  (e.g. the earlier reviewer) and an `assignment.held_by_retired_reviewer` flag.
* Applications held by a retired reviewer **without** a recommendation are not stuck: the
  Operations Admin takes them over with **Start review** (event `review_started`, details
  `previous_reviewer`).
* Applications with a recommendation from the old workflow can be decided directly.
* Former reviewer/approver identities keep their rows but they are not effective. They do
  **not** become admins (the admin role needs Core role `admin` + an explicit assignment). Their
  customer accounts and banking data are untouched.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `LOANS_LEGACY_REVIEWER_WORKFLOW_ENABLED` | `false` | `true` restores the retired reviewer/approver workflow exactly (rollback) |

No Statements configuration change.

## Transition — NOT EXECUTED (to be run by infra, in order, after review)

1. Bootstrap the Operations Admin(s) in **both** services (placeholders only; the subject is
   the Core identity shown by `/x/loans/v1/me` for the signed-in admin):

   ```bash
   python -m nvb_loans.admin grant --subject nvb-user:<id>:<created> --role operations_admin \
       --actor "ops:<ticket>" --reason "Operations Admin loan desk"
   python -m nvb_statements.admin grant --subject nvb-user:<id>:<created> --role operations_admin \
       --actor "ops:<ticket>" --reason "Operations Admin statement operations"
   ```

   The subject must belong to a Core user whose role is `admin`; otherwise the grant is inert.
2. Deploy the new images (the flag defaults to `false`, so the reviewer workflow is retired at
   that moment). Open loan work is still visible and actionable by the Operations Admin.
3. Report what the retirement affects (read-only):

   ```bash
   python -m nvb_loans.admin reviewer-transition            # --dry-run is the default
   ```

   Lists active reviewer/approver assignments and open applications held by a non-admin reviewer.
4. Optionally revoke the retired roles (audited; one `role_events` row per revocation; never
   touches applications or customer data):

   ```bash
   python -m nvb_loans.admin reviewer-transition --apply --actor "ops:<ticket>" --reason "reviewer workflow retired"
   ```

## Rollback

* Set `LOANS_LEGACY_REVIEWER_WORKFLOW_ENABLED=true` and restart loans-api: the reviewer/approver
  workflow works again with the existing rows. If step 4 was applied, re-grant the listed
  assignments with `python -m nvb_loans.admin grant ...` (the dry-run output is the record).
* Revoke `operations_admin` with `python -m nvb_loans.admin revoke ...` /
  `python -m nvb_statements.admin revoke ...`.
* Applications decided by an Operations Admin stay decided (they are valid workflow states).
  No schema change exists, so redeploying the previous images is compatible with the data
  (they ignore the extra role value and the extra event type).

## Legacy `/digital/` portal

The standalone portal's reviewer/approver screens are superseded by the unified banking UI's
Loan management page. The backend enforces the new model regardless of UI: those screens now
receive `403` (retired roles) or `410 reviewer_workflow_retired` (assignment).
