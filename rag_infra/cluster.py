"""Run a real Indexed Job demo in a disposable kind cluster."""

import argparse
import json
import secrets
import subprocess
import time
from pathlib import Path

from rag_infra.core import read_suite, release_gate, summarize

SHARDS = 4
PARALLELISM = 2
SUITE_PATH = "/app/fixtures/rag-suite.json"


class Kubectl:
    def __init__(self, context, namespace):
        self.context = context
        self.namespace = namespace

    def run(self, *args, payload=None, scoped=True):
        command = ["kubectl", "--context", self.context]
        if scoped:
            command += ["--namespace", self.namespace]
        completed = subprocess.run(
            command + list(args), input=json.dumps(payload) if payload else None, text=True,
            capture_output=True, timeout=135,
        )
        if completed.returncode:
            raise RuntimeError(f"kubectl {' '.join(args)}: {completed.stderr.strip()}")
        return completed.stdout

    def create(self, value, scoped=True):
        return self.run("create", "-f", "-", payload=value, scoped=scoped)

    def get(self, *args):
        return json.loads(self.run("get", *args, "-o", "json"))


def container(image, name, command=None, args=None, fault=False):
    cpu = "1000" if fault else "50m"
    spec = {
        "name": name, "image": image, "imagePullPolicy": "Never",
        "resources": {"requests": {"cpu": cpu, "memory": "32Mi"},
                      "limits": {"cpu": "1000" if fault else "250m", "memory": "128Mi"}},
        "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
    }
    if command:
        spec["command"] = command
    if args:
        spec["args"] = args
    return spec


def pod_spec(spec, restart="Always"):
    return {"restartPolicy": restart, "automountServiceAccountToken": False,
            "securityContext": {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [spec]}


def deploy(k, name, image, profile, weather):
    selector = {"app": name}
    command = ["python", "weather_agent.py"] if weather else ["python", "-m", "rag_infra.serve"]
    spec = container(image, "rag", command, args=["--host", "0.0.0.0", "--rag-profile", profile])
    spec["readinessProbe"] = {"httpGet": {"path": "/readyz", "port": 8080}, "periodSeconds": 2}
    k.create({
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name},
        "spec": {"replicas": 1, "selector": {"matchLabels": selector},
                 "template": {"metadata": {"labels": selector}, "spec": pod_spec(spec)}},
    })
    k.create({"apiVersion": "v1", "kind": "Service", "metadata": {"name": name},
              "spec": {"selector": selector, "ports": [{"port": 8080, "targetPort": 8080}]}})


def job_manifest(name, image, endpoint, model, prompt, fault=False):
    spec = container(image, "evaluate", command=["python", "-m", "rag_infra.core"], args=[
        "shard", "--suite", SUITE_PATH, "--shards", str(SHARDS), "--endpoint", endpoint,
        "--model", model, "--prompt", prompt,
    ], fault=fault)
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name}, "spec": {
        "completionMode": "Indexed", "completions": SHARDS, "parallelism": PARALLELISM,
        "backoffLimitPerIndex": 1, "activeDeadlineSeconds": 150,
        "template": {"spec": pod_spec(spec, "Never")},
    }}


def scheduling_failures(pods):
    issues = []
    for pod in pods:
        for condition in pod.get("status", {}).get("conditions", []):
            if (condition.get("type"), condition.get("status"), condition.get("reason")) == (
                "PodScheduled", "False", "Unschedulable"
            ):
                issues.append({"type": "scheduling_failed", "pod": pod["metadata"]["name"],
                               "reason": condition["reason"], "detail": condition.get("message", "")})
    return issues


def run_job(k, suite, name, image, service, model, prompt, out, fault=False):
    endpoint = f"http://{service}:8080/rag/answer"
    k.create(job_manifest(name, image, endpoint, model, prompt, fault))
    deadline = time.monotonic() + (50 if fault else 160)
    reports, findings = [], []
    while True:
        job = k.get("job", name)
        pods = k.get("pods", "-l", f"batch.kubernetes.io/job-name={name}")["items"]
        if fault and (findings := scheduling_failures(pods)):
            break
        conditions = {c["type"]: c for c in job.get("status", {}).get("conditions", [])
                      if c.get("status") == "True"}
        if "Complete" in conditions or "Failed" in conditions or time.monotonic() >= deadline:
            if "Complete" not in conditions:
                findings = scheduling_failures(pods)
                if not findings:
                    findings.append({"type": "execution_failed", "job": name,
                                     "reason": conditions.get("Failed", {}).get("reason", "deadline")})
            break
        time.sleep(2)
    for pod in pods:
        if pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"}:
            continue
        pod_name = pod["metadata"]["name"]
        try:
            row = json.loads(k.run("logs", pod_name, "-c", "evaluate"))
            if pod["status"]["phase"] == "Succeeded":
                reports.append(row)
            elif any(item.get("status") == "execution_error" for item in row.get("results", [])):
                # Failed-attempt details are diagnostic only; they never substitute for a successful shard.
                findings_for_pod = [item for item in row["results"] if item["status"] == "execution_error"]
                (out / f"{pod_name}-execution-error.json").write_text(json.dumps(findings_for_pod, indent=2))
        except (ValueError, RuntimeError):
            if pod["status"]["phase"] == "Succeeded":
                findings.append({"type": "execution_failed", "pod": pod_name, "reason": "unreadable_report"})
    if job.get("spec", {}).get("completionMode") != "Indexed":
        findings.append({"type": "configuration_error", "reason": "expected_indexed_job"})
    result = summarize(suite, reports, SHARDS, model, prompt, "fixture", findings)
    result["kubernetes"] = {"namespace": k.namespace, "job": name, "parallelism": PARALLELISM,
                            "completion_mode": "Indexed",
                            "pods": [{"name": pod["metadata"]["name"],
                                      "phase": pod.get("status", {}).get("phase", "Unknown")} for pod in pods]}
    (out / f"{name}.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"{name}: {result['status']}, completion={result['metrics']['completion_rate']:.0%}", flush=True)
    return result


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--suite", default="fixtures/rag-suite.json")
    parser.add_argument("--output", default="artifacts/rag-eval")
    parser.add_argument("--serve-weather", action="store_true", help="use the Weather Agent HTTP server")
    args = parser.parse_args()
    if not args.context.startswith("kind-"):
        parser.error("demo only runs against an explicit disposable kind context")
    suite = read_suite(args.suite)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    namespace = "rag-eval-" + secrets.token_hex(3)
    k = Kubectl(args.context, namespace)
    k.create({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}}, scoped=False)
    print(f"Inspect or clean up the disposable namespace after the run: {namespace}", flush=True)
    deploy(k, "rag-baseline", args.image, "guarded", args.serve_weather)
    deploy(k, "rag-candidate", args.image, "regressed", args.serve_weather)
    for service in ("rag-baseline", "rag-candidate"):
        k.run("rollout", "status", f"deployment/{service}", "--timeout=90s")
    baseline = run_job(k, suite, "baseline", args.image, "rag-baseline", "fixture-v1", "guarded-v1", out)
    candidate = run_job(k, suite, "injection", args.image, "rag-candidate", "fixture-v1", "regressed-v1", out)
    scheduling = run_job(k, suite, "unschedulable", args.image, "rag-candidate",
                         "fixture-v1", "regressed-v1", out, fault=True)
    gate = release_gate(baseline, candidate)
    (out / "gate.json").write_text(json.dumps({"release_gate": gate, "scheduling_run": scheduling["status"]}, indent=2))
    require(baseline["status"] == "passed" and baseline["shards"] == list(range(SHARDS)),
            "all four baseline shards must pass")
    require(gate == "regression" and candidate["metrics"]["completion_rate"] == 1
            and candidate["metrics"]["attack_success"]["count"] == 1,
            "injection must block release after all shards complete")
    require(scheduling["status"] == "incomplete" and scheduling["metrics"]["completion_rate"] == 0
            and any(isinstance(f, dict) and f["type"] == "scheduling_failed"
                    for f in scheduling["infrastructure_findings"]),
            "unschedulable Pods must yield an incomplete run with scheduler evidence")
    print("Baseline, model regression and Kubernetes scheduling failure verified.", flush=True)


if __name__ == "__main__":
    main()
