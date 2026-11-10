"""Offline validation of deploy/openshift (no cluster access; nothing is applied).

1. `kustomize build` of every kustomization of both overlays.
2. `kubeconform -strict` schema validation of the rendered built-in kinds (Route, ScaledJob and
   TriggerAuthentication are CRDs: they are parsed and policy-checked here, schema-skipped).
3. Policy checks on every pod template:
   * restricted pod security: runAsNonRoot, no privilege escalation, drop ALL, RuntimeDefault
     seccomp, no hostNetwork/hostPID/hostPath/privileged, no runAsUser 0;
   * CPU/memory requests and limits on every container; no ServiceAccount token automount;
   * every referenced ServiceAccount/ConfigMap exists in the rendered set or is a documented
     externally-created object; every referenced Secret is documented in secrets/README.md;
   * no `:latest` application image; no localhost / sqlite / embedded credentials in ConfigMaps;
   * settle/page-size agreement between core, adapter, statements and loans settings;
   * the adapter's fleet bound: replicas x ADAPTER_CORE_MAX_CONCURRENCY <= its annotation;
   * KEDA: per-Job message target equals *_WORKER_MAX_MESSAGES; Jobs have deadlines and TTLs.
4. `--strict`: additionally fail on any remaining REPLACE_* placeholder (use before a real apply).

    python scripts/validate_manifests.py [--strict] [--kustomize PATH] [--kubeconform PATH]
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
OPENSHIFT = ROOT / "deploy" / "openshift"
SUBS = ["", "migrations", "worker-statements", "worker-loans", "batch-cronjobs", "role-bootstrap", "autoscaling-keda"]
EXTERNAL_CONFIGMAPS = {"core-ca", "deps-ca"}          # created by infra (shared PoC), see overlays/poc
CRD_KINDS = {"Route", "ScaledJob", "TriggerAuthentication"}
APP_IMAGES = ("nvb-core-adapter", "nvb-statements", "nvb-loans", "nvb-gateway")


def tool(name: str, explicit: str | None) -> str:
    for cand in (explicit, shutil.which(name), str(ROOT / ".local" / name / f"{name}.exe"),
                 str(ROOT / ".local" / name / name)):
        if cand and Path(cand).exists():
            return cand
    raise SystemExit(f"{name} not found (pass --{name})")


def pod_specs(doc: dict):
    kind, spec = doc.get("kind"), doc.get("spec", {})
    if kind in ("Deployment", "StatefulSet", "Job"):
        yield spec["template"]["spec"]
    elif kind == "CronJob":
        yield spec["jobTemplate"]["spec"]["template"]["spec"]
    elif kind == "ScaledJob":
        yield spec["jobTargetRef"]["template"]["spec"]


def check_pods(where: str, docs: list[dict], names: dict, documented: set[str], errors: list[str]) -> None:
    def err(msg):
        errors.append(f"{where}: {msg}")

    for d in docs:
        name = f"{d['kind']}/{d['metadata']['name']}"
        for ps in pod_specs(d):
            if ps.get("hostNetwork") or ps.get("hostPID") or ps.get("hostIPC"):
                err(f"{name}: host namespaces are not allowed")
            if ps.get("automountServiceAccountToken") is not False:
                err(f"{name}: automountServiceAccountToken must be false")
            sa = ps.get("serviceAccountName")
            if sa and sa not in names["ServiceAccount"]:
                err(f"{name}: ServiceAccount {sa} is not defined")
            psc = ps.get("securityContext", {})
            for v in ps.get("volumes", []):
                if "hostPath" in v:
                    err(f"{name}: hostPath volume")
                if "configMap" in v and v["configMap"]["name"] not in names["ConfigMap"] | EXTERNAL_CONFIGMAPS:
                    err(f"{name}: ConfigMap {v['configMap']['name']} is not defined")
                if "secret" in v and v["secret"]["secretName"] not in documented \
                        and not v["secret"]["secretName"].endswith("-tls"):
                    err(f"{name}: Secret {v['secret']['secretName']} is not documented in secrets/README.md")
            for c in ps.get("containers", []) + ps.get("initContainers", []):
                sc = c.get("securityContext", {})
                cn = f"{name}/{c['name']}"
                if sc.get("privileged") or sc.get("runAsUser") == 0 or psc.get("runAsUser") == 0:
                    err(f"{cn}: privileged / root")
                if sc.get("allowPrivilegeEscalation") is not False:
                    err(f"{cn}: allowPrivilegeEscalation must be false")
                if "ALL" not in sc.get("capabilities", {}).get("drop", []):
                    err(f"{cn}: must drop ALL capabilities")
                if not (sc.get("runAsNonRoot") or psc.get("runAsNonRoot")):
                    err(f"{cn}: runAsNonRoot")
                if (sc.get("seccompProfile") or psc.get("seccompProfile") or {}).get("type") != "RuntimeDefault":
                    err(f"{cn}: seccompProfile RuntimeDefault")
                res = c.get("resources", {})
                for kind_ in ("requests", "limits"):
                    if not {"cpu", "memory"} <= set(res.get(kind_, {})):
                        err(f"{cn}: missing resources.{kind_}")
                image = c.get("image", "")
                last = image.split("/")[-1]
                if any(a in image for a in APP_IMAGES) and (image.endswith(":latest") or (":" not in last and "@" not in last)):
                    err(f"{cn}: application image must carry a version tag or digest ({image})")
                for ref in c.get("envFrom", []):
                    cm = ref.get("configMapRef", {}).get("name")
                    if cm and cm not in names["ConfigMap"]:
                        err(f"{cn}: envFrom ConfigMap {cm} is not defined")
                    sec = ref.get("secretRef", {}).get("name")
                    if sec and sec not in documented:
                        err(f"{cn}: envFrom Secret {sec} is not documented")
                for e in c.get("env", []):
                    sec = (e.get("valueFrom") or {}).get("secretKeyRef", {}).get("name")
                    if sec and sec not in documented:
                        err(f"{cn}: Secret {sec} is not documented")
        spec = d.get("spec", {})
        if d["kind"] == "Job" and (not spec.get("activeDeadlineSeconds") or spec.get("ttlSecondsAfterFinished") is None):
            err(f"{name}: Job needs activeDeadlineSeconds and ttlSecondsAfterFinished")
        if d["kind"] == "CronJob" and (spec.get("successfulJobsHistoryLimit") is None
                                        or spec.get("concurrencyPolicy") != "Forbid"):
            err(f"{name}: CronJob needs history limits and concurrencyPolicy Forbid")
        if d["kind"] == "ScaledJob":
            if spec.get("maxReplicaCount", 99) > 4:
                err(f"{name}: maxReplicaCount is not conservative")
            if any(t["metadata"]["value"] != "50" for t in spec["triggers"]):
                err(f"{name}: trigger value must equal *_WORKER_MAX_MESSAGES (50)")
        if d["kind"] == "Secret":
            err(f"{name}: Secrets must not be part of the rendered manifests")
        if d["kind"] == "ConfigMap":
            for k, v in (d.get("data") or {}).items():
                if not k.isupper():
                    continue
                if k.endswith(("_URL", "_UPSTREAM", "_HOST", "_ORIGIN")) and re.search(
                        r"localhost|127\.0\.0\.1|sqlite", str(v)):
                    err(f"{name}: {k} points at localhost/sqlite")
                if re.search(r"://[^/@\s]+:[^/@\s]+@", str(v)):
                    err(f"{name}: {k} embeds credentials in a URL")
                if k.endswith(("_PASSWORD", "_TOKEN", "_SECRET", "_SECRET_KEY")):
                    err(f"{name}: {k} looks like a secret in a ConfigMap")


def check_config(env: str, docs: list[dict], errors: list[str]) -> None:
    def err(msg):
        errors.append(f"{env}: {msg}")

    cms = {d["metadata"]["name"]: d.get("data") or {} for d in docs if d["kind"] == "ConfigMap"}
    st, ad, ln, gw = (cms[n] for n in ("statements-config", "core-adapter-config", "loans-config", "gateway-config"))
    core = cms.get("core-test-config", {})
    core_settle = int(core.get("NVB_INTEGRATION_SETTLE_SECONDS", "300"))
    core_page = int(core.get("NVB_INTEGRATION_MAX_PAGE_SIZE", "1000"))
    core_entries = int(core.get("NVB_INTEGRATION_MAX_STATEMENT_ENTRIES", "250000"))
    if int(st["STATEMENTS_SETTLE_SECONDS"]) < core_settle or int(ln["LOANS_SETTLE_SECONDS"]) < core_settle:
        err("settle seconds of statements/loans must be >= the core's NVB_INTEGRATION_SETTLE_SECONDS")
    if not int(st["STATEMENTS_ENTRIES_PAGE_SIZE"]) <= int(ad["ADAPTER_ENTRIES_MAX_PAGE_SIZE"]) <= core_page:
        err("page sizes must satisfy statements <= adapter <= core")
    if int(st["STATEMENTS_MAX_ENTRIES_PER_STATEMENT"]) > core_entries:
        err("STATEMENTS_MAX_ENTRIES_PER_STATEMENT exceeds the core's NVB_INTEGRATION_MAX_STATEMENT_ENTRIES")
    if st["STATEMENTS_STATEMENT_TIMEZONE"] != ln["LOANS_SUMMARY_TIMEZONE"]:
        err("statement and loan summary timezones differ")
    if st["STATEMENTS_WORKER_MAX_MESSAGES"] != "50" or ln["LOANS_WORKER_MAX_MESSAGES"] != "50":
        err("*_WORKER_MAX_MESSAGES must equal the KEDA trigger value (50)")
    for prefix, data in (("STATEMENTS", st), ("LOANS", ln)):
        if data.get(f"{prefix}_ALLOW_PLAINTEXT_DEPENDENCIES") == "true" and env != "test":
            err(f"{prefix}: plaintext dependencies are only allowed in the disposable test overlay")
        if not data[f"{prefix}_ADAPTER_BASE_URL"].startswith("https://"):
            err(f"{prefix}: the adapter URL must be https://")
        if "ALLOW_SQLITE" in " ".join(data) or "ALLOW_INSECURE_HTTP" in " ".join(data):
            err(f"{prefix}: development-only flags must not appear in cluster configuration")
    if not ad["ADAPTER_CORE_BASE_URL"].startswith("https://"):
        err("ADAPTER_CORE_BASE_URL must be https://")
    route = next(d for d in docs if d["kind"] == "Route")
    if gw["GATEWAY_PUBLIC_ORIGIN"] != f"https://{route['spec'].get('host')}":
        err("GATEWAY_PUBLIC_ORIGIN must equal https://<Route host>")
    if route["spec"]["tls"]["termination"] != "reencrypt":
        err("the portal Route must use re-encrypt TLS")
    for k in ("GATEWAY_CORE_UPSTREAM", "GATEWAY_STATEMENTS_UPSTREAM", "GATEWAY_LOANS_UPSTREAM"):
        if not gw[k].startswith("https://"):
            err(f"gateway-config: {k} must be https://")
    if "GATEWAY_ALLOW_INSECURE_UPSTREAMS" in gw:
        err("gateway-config: GATEWAY_ALLOW_INSECURE_UPSTREAMS must not be set in cluster configuration")
    adapter = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "core-adapter")
    budget = int(adapter["metadata"]["annotations"]["nvb.io/core-fleet-max-concurrency"])
    used = adapter["spec"]["replicas"] * int(ad["ADAPTER_CORE_MAX_CONCURRENCY"])
    if used > budget:
        err(f"core-adapter: replicas x ADAPTER_CORE_MAX_CONCURRENCY = {used} exceeds the fleet bound {budget}")
    if any(d["kind"] == "HorizontalPodAutoscaler" for d in docs):
        err("no HorizontalPodAutoscaler is allowed (the adapter bound and the ScaledJobs are the scaling model)")
    policies = {d["metadata"]["name"] for d in docs if d["kind"] == "NetworkPolicy"}
    for needed in ("default-deny", "allow-dns", "egress-to-core"):
        if needed not in policies:
            err(f"NetworkPolicy {needed} is missing")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--strict", action="store_true", help="fail on remaining REPLACE_* placeholders")
    p.add_argument("--kustomize")
    p.add_argument("--kubeconform")
    args = p.parse_args()
    kustomize, kubeconform = tool("kustomize", args.kustomize), tool("kubeconform", args.kubeconform)
    documented = set(re.findall(r"^\| `([a-z0-9-]+)` \|",
                                (OPENSHIFT / "secrets" / "README.md").read_text(encoding="utf-8"), re.M))
    errors: list[str] = []
    placeholders: dict[str, set[str]] = {}
    total = 0
    for env in ("test", "poc"):
        rendered: dict[str, list[dict]] = {}
        for sub in SUBS + (["core-test-jobs"] if env == "test" else []):
            label = f"{env}/{sub or 'main'}"
            out = subprocess.run([kustomize, "build", str(OPENSHIFT / "overlays" / env / sub)], capture_output=True,
                                 text=True)
            if out.returncode:
                errors.append(f"{label}: kustomize build failed: {out.stderr.strip()[:300]}")
                continue
            conf = subprocess.run([kubeconform, "-strict", "-summary", "-kubernetes-version", "1.29.0", "-skip",
                                   ",".join(sorted(CRD_KINDS))], input=out.stdout, capture_output=True, text=True)
            if conf.returncode:
                errors.append(f"{label}: kubeconform: {conf.stdout.strip()[:600]}")
            docs = [d for d in yaml.safe_load_all(out.stdout) if d]
            rendered[sub or "main"] = docs
            total += len(docs)
            placeholders.setdefault(env, set()).update(re.findall(r"REPLACE_[A-Z_]+", out.stdout))
        main_docs = rendered.get("main", [])
        names = {k: {d["metadata"]["name"] for d in main_docs if d["kind"] == k} for k in ("ServiceAccount", "ConfigMap")}
        for sub, docs in rendered.items():
            check_pods(f"{env}/{sub}", docs, names, documented, errors)
        if main_docs:
            check_config(env, main_docs, errors)
    errors = sorted(set(errors))
    print(f"rendered objects checked: {total}")
    for env, found in sorted(placeholders.items()):
        if found:
            print(f"placeholders still to set in overlays/{env}: {sorted(found)}")
            if args.strict:
                errors.append(f"{env}: unresolved placeholders (strict mode)")
    for e in errors:
        print("ERROR", e)
    print("RESULT:", "FAILED" if errors else "OK (schema + policy checks passed; nothing was applied to any cluster)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
