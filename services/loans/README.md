# services/loans

Package `nvb_loans`, one image (`services/loans/Containerfile`, build context = repository root):

| Component | Command | Kind |
| --- | --- | --- |
| loans-api | `uvicorn --factory nvb_loans.api:app_factory` (image default) | Deployment + Service |
| loans-relay | `python -m nvb_loans.relay` | Deployment |
| loans-worker | `python -m nvb_loans.worker --drain` | Job / KEDA ScaledJob |
| batch trigger | `python -m nvb_loans.relay batch-create --trigger schedule` | CronJob |
| role administration | `python -m nvb_loans.admin grant|revoke|list` | one-off Job |
| migration | `alembic -c /opt/app/migrations/loans/alembic.ini upgrade head` | one-off Job |

Workflow `draft → submitted → under_review → approved | rejected`. Approval is a workflow decision
only: this package contains no call that posts money or writes to the core. The background
summary is an integer-only, clearly labelled synthetic indicator; humans decide.
