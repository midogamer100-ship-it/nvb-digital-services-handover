# Demo script (about 15 minutes)

Works identically on the laptop stack (`scripts/local_stack.py start` + `scripts/local_run.py up`,
portal `http://127.0.0.1:18080`) and on the OpenShift test namespace (portal = the Route host;
replace the `local_run.py worker …` commands with the worker Jobs of the runbook, steps 10/14, or
let KEDA start them). All data is synthetic. Say so at the start: the red banner on every page and
the watermark on every PDF say it too.

Demo logins: `customer001`, `customer002` (customers), `opsadmin` (core admin). Passwords: the
generated file (`.local/run/demo-passwords.json` locally; `secrets/test/test/core-test-seed.env`
for the cluster).

## 0. Preparation (before the audience arrives)

Grant extension roles once (subjects are shown on each user's **My identity** page):

```
python scripts/local_run.py grant statements extension_admin <opsadmin subject>
python scripts/local_run.py grant loans extension_admin <opsadmin subject>
```
Then, as `opsadmin` on the **Roles** page: statements `batch_operator` → opsadmin; loans
`batch_operator` + `approver` → opsadmin; loans `reviewer` → customer002's subject (any second
person works; the point is that it is an explicit assignment, not a core role).

## 1. Login through the new origin
1. Open the portal. Sign in as `customer001` with the **existing core credentials**.
2. Point out: the address bar shows the new portal host; the login call went to the core
   (`/api/auth/login`) through the gateway; the session cookie is HttpOnly and host-only.
3. Open `<portal>/api/accounts` in a new tab → JSON 404: only the three session calls of the core
   are exposed here. The existing core application keeps working on its own address, unchanged.

## 2. Statement request and download
1. **Statements** → choose an account and last month → **Request statement** → status `queued`.
2. Start one bounded worker run: `python scripts/local_run.py worker statements` (cluster: worker
   Job / KEDA). The row turns `completed` by itself (the page polls).
3. **Download PDF**: watermark, period in Cairo time and UTC, opening/closing balances, dataset id.
4. Sign in as `customer002` in a private window and paste the first customer's document URL →
   "not found". Sign out as customer001 and reload the document URL → 401.

## 3. Batch statement progress
1. Sign in as `opsadmin` → **Batches** → month = last month → **Create statement batch**.
2. Status goes `freezing → releasing → running`; "queued N".
3. Run `python scripts/local_run.py worker statements -- --max-messages 2`: progress shows 2
   completed, the rest still queued — one bounded Job handles a bounded group.
4. Run the worker again without the limit; the batch ends `completed`. **Failures** lists reasons
   when there are any; **Retry failed** re-queues only those (bounded rounds).
5. Back as `customer001`: the batch-generated statement appears in *My requests*.

## 4. Worker scale-up and return to idle (cluster, with KEDA enabled)
1. `oc -n $NS get jobs -w` in a terminal; create a batch.
2. Jobs appear (at most 2 for statements), each exits after ≤ 50 messages or when idle.
3. When the queue is empty no Job is running: `oc -n $NS get pods -l nvb.io/component=statements-worker`
   shows only Completed pods (removed after the TTL). Without KEDA show the same with two manual Jobs.

## 5. Loan submission, review, decision
1. `customer001` → **Loans** → amount `120000.00`, 24 months, income `15000.00` → **Save draft**
   → **Submit**.
2. `opsadmin` → **Loan review** → open it → **Assign reviewer** (customer002's subject).
3. Run `python scripts/local_run.py worker loans` → the *Background summary* appears, labelled
   "SYNTHETIC DEMO SCORE – not a credit decision".
4. Sign in as the reviewer → **Loan review** shows only the assigned application → record a
   recommendation with a reason. Note: the reviewer has no Approve button.
5. `opsadmin` (approver, a different person) → **Approve** with a reason. History shows who did
   what and when.
6. `customer001` sees `approved` and the notice that no money is disbursed. Open the core
   application: the account balance is unchanged.

## 6. Core remains operational under bounded batch work
1. Start `python tests/load/statement_load.py --base-url <portal> --passwords-file <file>
   --users customer001,customer002 --requests 40 --concurrency 4` and a worker run in parallel.
2. Meanwhile use the core application normally (dashboard, a transfer).
3. Show the script's output: latency of a core call before vs. during, all requests finished.
   Explain the bounds: one adapter replica × 4 concurrent core calls, 20 requests/s, ≤ 2 worker
   Jobs; the core additionally limits export queries per process and answers 429 beyond that.
   Numbers from this run are observations of this environment, not capacity guarantees.
