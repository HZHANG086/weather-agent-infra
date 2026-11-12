"""Roll a worker-only MachineDeployment and measure its Weather Service impact."""

import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess
import time

from capi_lifecycle import DEPLOYMENT
from inspect_cluster import kubectl_json
from scripts.capi_demo import CLUSTER_NAME, IMAGE, MANAGEMENT, kubectl, load_image
from scripts.weather_probe import INTERVAL, TIMEOUT

FROM = "v1.34.0"
TO = "v1.34.3"
IMAGES = {
    FROM: "kindest/node:v1.34.0@sha256:7416a61b42b1662ca6ca89f02028ac133a309a2a30ba309614e8ec94d976dc5a",
    TO: "kindest/node:v1.34.3@sha256:08497ee19eace7b4b5348db5c6a1591d7752b164530a36f855cb0f2bdcbadd48",
}
NAME = "weather-upgrade-workers"
POOL_KEY = "weather.example/worker-pool"
POOL_LABELS = {POOL_KEY: "upgrade"}
PROBE = "weather-upgrade-probe"
OUTPUT = Path("artifacts/capi-upgrade")
POST_WINDOW = 10


def object_(api, kind, name, spec, namespace="default"):
    return {"apiVersion": api, "kind": kind, "metadata": {"name": name, "namespace": namespace},
            "spec": spec}


def infrastructure(version):
    return object_("infrastructure.cluster.x-k8s.io/v1beta2", "DockerMachineTemplate", f"{NAME}-{version}",
                   {"template": {"spec": {"customImage": IMAGES[version], "preLoadImages": [IMAGE]}}})


def worker_manifest():
    bootstrap = object_("bootstrap.cluster.x-k8s.io/v1beta2", "KubeadmConfigTemplate", NAME,
                        {"template": {"spec": {"joinConfiguration": {"nodeRegistration": {
                            "kubeletExtraArgs": [{"name": "node-labels", "value": f"{POOL_KEY}=upgrade"}],
                        }}}}})
    workers = object_("cluster.x-k8s.io/v1beta2", "MachineDeployment", NAME, {
        "clusterName": CLUSTER_NAME, "replicas": 2,
        "rollout": {"strategy": {"type": "RollingUpdate",
                                 "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}}},
        "selector": {"matchLabels": POOL_LABELS},
        "template": {"metadata": {"labels": POOL_LABELS}, "spec": {
            "clusterName": CLUSTER_NAME, "version": FROM,
            "bootstrap": {"configRef": {"apiGroup": "bootstrap.cluster.x-k8s.io",
                                        "kind": "KubeadmConfigTemplate", "name": NAME}},
            "infrastructureRef": {"apiGroup": "infrastructure.cluster.x-k8s.io",
                                  "kind": "DockerMachineTemplate", "name": f"{NAME}-{FROM}"},
        }},
    })
    return {"apiVersion": "v1", "kind": "List", "items": [bootstrap, infrastructure(FROM), infrastructure(TO), workers]}


def ready(obj):
    return not obj["metadata"].get("deletionTimestamp") and any(
        c["type"] == "Ready" and c["status"] == "True" for c in obj.get("status", {}).get("conditions", []))


def nodes(context):
    return kubectl_json(context, "nodes")["items"]


def snapshot(context):
    raw = nodes(context)
    groups = {"workers": [], "control_plane": [], "ready_replicas": []}
    for node in raw:
        metadata = node["metadata"]
        record = {"name": metadata["name"], "uid": metadata["uid"], "ready": ready(node),
                  "version": node.get("status", {}).get("nodeInfo", {}).get("kubeletVersion")}
        if "node-role.kubernetes.io/control-plane" in metadata.get("labels", {}):
            groups["control_plane"].append(record)
        elif metadata.get("labels", {}).get(POOL_KEY) == "upgrade":
            groups["workers"].append(record)
    for pod in kubectl_json(context, "pods", "weather", selector="app=weather-agent")["items"]:
        if ready(pod):
            groups["ready_replicas"].append({"name": pod["metadata"]["name"], "node": pod["spec"]["nodeName"]})
    return {k: sorted(v, key=lambda item: item["name"]) for k, v in groups.items()}


def converged(state, version, *, replicas):
    workers = state["workers"]
    if len(workers) != 2 or not all(node["ready"] and node["version"] == version for node in workers):
        return False
    pods = state["ready_replicas"]
    return not replicas or (len(pods) == 2 and {p["node"] for p in pods} == {n["name"] for n in workers})


def wait_state(context, report, predicate, description, timeout=420):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = snapshot(context)
        if not report["transitions"] or report["transitions"][-1]["state"] != state:
            report["transitions"].append({"at": time.time(), "state": state})
            print(f"{description}: {state}", flush=True)
        if predicate(state):
            return state
        time.sleep(2)
    raise RuntimeError(f"timed out waiting for {description}")


def start_probe(context, node):
    source = Path("scripts/weather_probe.py").read_text()
    config = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": PROBE, "namespace": "weather"},
              "data": {"weather_probe.py": source}}
    kubectl(context, "create", "-f", "-", document=config)
    pod = object_("v1", "Pod", PROBE, {
        "restartPolicy": "Never", "automountServiceAccountToken": False,
        "nodeSelector": {"kubernetes.io/hostname": node},
        "tolerations": [{"key": "node-role.kubernetes.io/control-plane", "operator": "Exists"}],
        "securityContext": {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [{"name": "probe", "image": IMAGE, "imagePullPolicy": "Never",
                        "command": ["python", "-u", "/probe/weather_probe.py"],
                        "volumeMounts": [{"name": "script", "mountPath": "/probe", "readOnly": True}]}],
        "volumes": [{"name": "script", "configMap": {"name": PROBE}}],
    }, "weather")
    kubectl(context, "create", "-f", "-", document=pod)
    kubectl(context, "-n", "weather", "wait", "--for=condition=Ready", f"pod/{PROBE}", "--timeout=90s")


def logs(context):
    raw = kubectl(context, "-n", "weather", "logs", f"pod/{PROBE}", timeout=25)
    (OUTPUT / "samples.jsonl").write_text(raw)
    return sorted((json.loads(line) for line in raw.splitlines() if line), key=lambda row: row["seq"])


def baseline(context):
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        samples = logs(context)
        if len(samples) >= 5 and all(row["ok"] for row in samples[-5:]):
            return
        time.sleep(2)
    raise RuntimeError("probe did not observe five consecutive successes before the upgrade")


def measure(samples, start, end):
    """Measure scheduled attempts in the rollout, using the post-window to close outages."""
    relevant = sorted((s for s in samples if start - INTERVAL <= s["scheduled"] <= end + POST_WINDOW),
                      key=lambda s: s["seq"])
    if (not relevant or relevant[0]["scheduled"] > start or relevant[-1]["scheduled"] < end + POST_WINDOW - INTERVAL
            or any(b["seq"] != a["seq"] + 1 or abs(b["scheduled"] - a["scheduled"] - INTERVAL) > .05
                   for a, b in zip(relevant, relevant[1:]))
            or any(s["reason"] == "probe_overrun" or abs(s["started"] - s["scheduled"]) > INTERVAL
                   for s in relevant)):
        raise ValueError("incomplete or unreliable probe coverage across the rollout")
    rollout = [s for s in relevant if start <= s["scheduled"] < end]
    tail = [s for s in relevant if s["scheduled"] >= end]
    if not rollout or len(tail) < 5 or not all(s["ok"] for s in tail[-5:]):
        raise ValueError("no rollout samples or no healthy post-upgrade tail")
    errors = Counter(s["reason"] for s in rollout if not s["ok"])
    intervals = []
    first = None
    for s in rollout + tail:
        if not s["ok"]:
            if first is None and s["scheduled"] < end:
                first = s
        elif first is not None:
            intervals.append({"from_seq": first["seq"], "recovered_at_seq": s["seq"],
                              "duration_seconds": round(s["ended"] - first["started"], 3)})
            first = None
    return {"requests": len(rollout), "failed": sum(errors.values()),
            "failure_rate_percent": round(100 * sum(errors.values()) / len(rollout), 3),
            "failure_reasons": dict(errors), "longest_observed_interruption_seconds":
            max((i["duration_seconds"] for i in intervals), default=0),
            "last_request_recovery_seconds": intervals[-1]["duration_seconds"] if intervals else None,
            "two_upgraded_replicas_ready_seconds": round(end - start, 3),
            "outages": intervals, "post_upgrade_samples": len(tail)}


def run(context, archive, report):
    kubectl(context, "create", "namespace", "weather")
    kubectl(MANAGEMENT, "create", "-f", "-", document=worker_manifest())
    wait_state(context, report, lambda s: converged(s, FROM, replicas=False), "initial workers")
    # Create the namespace and Service, then apply the scheduling policy before
    # starting the two measured replicas.
    kubectl(context, "apply", "-f", "k8s/weather.yaml")
    kubectl(context, "-n", "weather", "patch", "deployment/weather-agent",
            "--type=merge", "--patch-file=k8s/upgrade-placement.json")
    kubectl(context, "apply", "-f", "k8s/upgrade-pdb.yaml")
    initial = wait_state(context, report, lambda s: converged(s, FROM, replicas=True), "two split replicas")
    report["initial"] = initial
    (control,) = initial["control_plane"]
    control_object = kubectl_json(context, "nodes", name=control["name"])
    load_image(control["name"], archive)
    start_probe(context, control_object["metadata"]["labels"]["kubernetes.io/hostname"])
    baseline(context)
    version_path = "/spec/template/spec/version"
    patch = [{"op": "test", "path": version_path, "value": FROM},
             {"op": "replace", "path": version_path, "value": TO},
             {"op": "replace", "path": "/spec/template/spec/infrastructureRef/name", "value": f"{NAME}-{TO}"}]
    start = report["upgrade_requested_at"] = time.time()
    kubectl(MANAGEMENT, "-n", "default", "patch", DEPLOYMENT, NAME, "--type=json", "-p", json.dumps(patch))
    old_uids = {n["uid"] for n in initial["workers"]}

    def finished(state):
        if state["control_plane"] != initial["control_plane"]:
            raise RuntimeError("control plane changed during the worker-only experiment")
        return converged(state, TO, replicas=True) and not old_uids.intersection(n["uid"] for n in state["workers"])

    report["final"] = wait_state(context, report, finished, "replacement workers and split replicas")
    end = report["upgraded_replicas_ready_at"] = time.time()
    time.sleep(POST_WINDOW + TIMEOUT + INTERVAL)
    report["metrics"] = measure(logs(context), start, end)
    print(json.dumps(report["metrics"], indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workload-context", required=True)
    parser.add_argument("--image-archive", type=Path, required=True)
    args = parser.parse_args()
    if args.workload_context != f"{CLUSTER_NAME}-admin@{CLUSTER_NAME}":
        parser.error("use the dedicated disposable CAPI workload")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    report = {"status": "incomplete", "scope": "CPU lab, synthetic Hangzhou weather response",
              "worker_version_from": FROM, "worker_version_to": TO,
              "request_interval_seconds": INTERVAL, "socket_timeout_seconds": TIMEOUT, "transitions": []}
    try:
        run(args.workload_context, args.image_archive, report)
        report["status"] = "passed"
    except Exception as exc:
        report["error"] = str(exc)
        try:
            logs(args.workload_context)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        raise
    finally:
        (OUTPUT / "report.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
