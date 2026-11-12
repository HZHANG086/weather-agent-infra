"""Assert lifecycle transitions on the disposable Cluster API workload cluster."""

import argparse
import json
import subprocess
import time
from pathlib import Path

from capi_lifecycle import CLUSTER, CLUSTER_LABEL, MACHINE, MACHINESET, PAUSED, snapshot
from inspect_cluster import kubectl_json

MANAGEMENT = "kind-weather-management"
CLUSTER_NAME = "weather-workload"
REPORT = Path("artifacts/capi-lifecycle/report.json")
IMAGE = "weather-agent:demo"


def kubectl(context, *args, document=None, timeout=130):
    return subprocess.run(
        ["kubectl", "--context", context, *args],
        input=json.dumps(document) if document is not None else None,
        text=True, check=True, stdout=subprocess.PIPE, timeout=timeout,
    ).stdout


def wait_for(context, description, predicate, limit=270):
    end = time.monotonic() + limit
    previous = state = None
    while time.monotonic() < end:
        state = snapshot(MANAGEMENT, context, CLUSTER_NAME)
        key = (state["workers"], [(r["machine"], r["stage"]) for r in state["machines"]],
               [f["reason"] for f in state["findings"]])
        if key != previous:
            print(f"{description}: {key}", flush=True)
            previous = key
        if predicate(state):
            return state
        time.sleep(3)
    raise RuntimeError(f"timed out: {description}; last state: {state}")


def converged(state, count):
    return state["status"] == "converged" and state["workers"]["desired"] == count


def change_replicas(old, new):
    path = "/spec/topology/workers/machineDeployments/0/replicas"
    patch = [{"op": "test", "path": path, "value": old}, {"op": "replace", "path": path, "value": new}]
    kubectl(MANAGEMENT, "-n", "default", "patch", CLUSTER, CLUSTER_NAME,
            "--type=json", "-p", json.dumps(patch))


def load_image(node, archive):
    # Verify the target container belongs to the CAPD lab before importing anything.
    labels = json.loads(subprocess.check_output(
        ["docker", "inspect", "--format", "{{json .Config.Labels}}", node], text=True, timeout=15))
    if (labels or {}).get("io.x-k8s.kind.cluster") != CLUSTER_NAME:
        raise RuntimeError(f"refusing to import an image into an unrelated Docker node: {node}")
    with archive.open("rb") as stream:
        subprocess.run(["docker", "exec", "-i", node, "ctr", "-n", "k8s.io", "images", "import", "-"],
                       stdin=stream, check=True, timeout=120)


def verify_weather(context, name, node):
    # Use a normal scheduler decision with a selector rather than pinning spec.nodeName.
    test = (
        "import json, urllib.request; "
        "r=json.load(urllib.request.urlopen('http://weather-agent:8080/ask?city=hangzhou&intent=trend', timeout=10)); "
        "assert r['trend']=='up' and len(r['evidence'])==2, r; "
        "print(json.dumps({'trend':r['trend'],'evidence_count':len(r['evidence'])}))"
    )
    hostname = kubectl_json(context, "nodes", name=node)["metadata"]["labels"]["kubernetes.io/hostname"]
    pod = {"restartPolicy": "Never", "automountServiceAccountToken": False,
           "nodeSelector": {"kubernetes.io/hostname": hostname},
           "securityContext": {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}},
           "containers": [{"name": "probe", "image": IMAGE, "imagePullPolicy": "Never",
                           "command": ["python", "-c", test],
                           "securityContext": {"allowPrivilegeEscalation": False,
                                               "capabilities": {"drop": ["ALL"]}}}]}
    job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "namespace": "weather"},
           "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 90, "template": {"spec": pod}}}
    kubectl(context, "create", "-f", "-", document=job)
    try:
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            live = kubectl_json(context, "jobs", "weather", name)
            terminal = {c["type"] for c in live.get("status", {}).get("conditions", [])
                        if c["status"] == "True"}
            if "Failed" in terminal:
                raise RuntimeError(f"weather probe Job {name} failed")
            if "Complete" in terminal:
                break
            time.sleep(2)
        else:
            raise RuntimeError(f"weather probe Job {name} timed out")
        (completed,) = kubectl_json(context, "pods", "weather", selector=f"batch.kubernetes.io/job-name={name}")["items"]
        if completed["spec"]["nodeName"] != node:
            raise RuntimeError("weather probe ran on the wrong worker")
        response = json.loads(kubectl(context, "-n", "weather", "logs", f"job/{name}"))
        return {"node": node, "response": response}
    except (RuntimeError, subprocess.SubprocessError, ValueError):
        for action in ("describe", "logs"):
            try:
                print(kubectl(context, "-n", "weather", action, f"job/{name}", timeout=15), flush=True)
            except subprocess.SubprocessError:
                pass
        raise
    finally:
        kubectl(context, "-n", "weather", "delete", "job", name, "--ignore-not-found", "--timeout=45s")


def run(context, archive, report):
    initial = wait_for(context, "initial worker", lambda s: converged(s, 1))
    (old,) = initial["machines"]
    report["initial"] = initial
    load_image(old["node"], archive)
    kubectl(context, "apply", "-f", "k8s/weather.yaml")
    kubectl(context, "-n", "weather", "scale", "deployment/weather-agent", "--replicas=1")
    kubectl(context, "-n", "weather", "rollout", "status", "deployment/weather-agent", "--timeout=100s")
    report["initial_probe"] = verify_weather(context, "weather-before-scale", old["node"])

    (machine_set,) = kubectl_json(MANAGEMENT, MACHINESET, "default",
                                  selector=f"{CLUSTER_LABEL}={CLUSTER_NAME}")["items"]
    ms = machine_set["metadata"]["name"]
    kubectl(MANAGEMENT, "-n", "default", "annotate", MACHINESET, ms, f"{PAUSED}=true")
    try:
        change_replicas(1, 2)
        blocked = wait_for(context, "scale-up blocked before provisioning",
                           lambda s: s["workers"] == {
                               "desired": 2, "deployment_replicas": 2, "machines": 1,
                               "registered": 1, "ready_and_uncordoned": 1,
                           } and any(f["reason"] == "MachineSetPaused" for f in s["findings"]))
        report["blocked_scale_up"] = blocked
        report["blocked_probe"] = verify_weather(context, "weather-during-block", old["node"])
    finally:
        kubectl(MANAGEMENT, "-n", "default", "annotate", MACHINESET, ms, f"{PAUSED}-")

    expanded = wait_for(context, "two workers available", lambda s: converged(s, 2))
    (new,) = [machine for machine in expanded["machines"] if machine["node"] != old["node"]]
    report["scaled_up"] = expanded
    load_image(new["node"], archive)
    report["new_worker_probe"] = verify_weather(context, "weather-on-new-worker", new["node"])

    # In this lab there is one MachineSet. Mark the added worker as the scale-down candidate.
    kubectl(MANAGEMENT, "-n", "default", "annotate", MACHINE, new["machine"],
            "cluster.x-k8s.io/delete-machine=1")
    change_replicas(2, 1)
    shrunk = wait_for(context, "original worker remains after scale-down",
                      lambda s: converged(s, 1) and s["machines"] == [old])
    report["scaled_down"] = shrunk
    report["final_probe"] = verify_weather(context, "weather-after-scale", old["node"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workload-context", required=True)
    parser.add_argument("--image-archive", type=Path, required=True)
    args = parser.parse_args()
    if args.workload_context not in {f"{CLUSTER_NAME}-admin@{CLUSTER_NAME}", f"{CLUSTER_NAME}-admin"}:
        parser.error("use the kubeconfig from the disposable weather-workload CAPI Cluster")
    report = {"status": "incomplete", "scope": "disposable CPU cluster and synthetic weather observations"}
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    try:
        run(args.workload_context, args.image_archive, report)
        report["status"] = "passed"
    finally:
        REPORT.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
