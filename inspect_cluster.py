"""Read Kubernetes objects through kubectl and report actionable problems."""

import argparse
import json
import subprocess
import sys

PRESSURES = {"MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable"}
BAD_WAIT = {
    "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull",
    "CreateContainerConfigError", "CreateContainerError", "RunContainerError",
}


def kubectl_json(context: str, resource: str, namespace: str | None = None,
                 name: str | None = None) -> dict:
    command = ["kubectl", "--context", context, "--request-timeout=10s", "get", resource]
    if name is not None:
        command.append(name)
    if namespace is not None:
        command.extend(["--namespace", namespace])
    command.extend(["-o", "json"])
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict) or (name is None and not isinstance(payload.get("items"), list)):
        raise ValueError(f"unexpected response for {resource}")
    return payload


def capi_expected_workers(cluster: dict) -> int:
    workers = cluster.get("spec", {}).get("topology", {}).get("workers", {})
    deployments = workers.get("machineDeployments", [])
    if not deployments or workers.get("machinePools"):
        raise ValueError("expected a Cluster API managed topology using MachineDeployments only")
    counts = [deployment.get("replicas") for deployment in deployments]
    if any(type(count) is not int or count < 0 for count in counts):
        raise ValueError("set explicit nonnegative topology replicas; autoscaled workers are unsupported")
    return sum(counts)


def inspect(nodes: list[dict], pods: list[dict], deployments: list[dict], expected_workers=None) -> dict:
    findings = []
    worker_count = ready_workers = 0

    def finding(resource: str, reason: str, message: str) -> None:
        findings.append({"resource": resource, "reason": reason, "message": message})

    for node in nodes:
        meta = node["metadata"]
        name = "node/" + meta["name"]
        conditions = {c["type"]: c for c in node.get("status", {}).get("conditions", [])}
        ready = conditions.get("Ready", {})
        is_ready = ready.get("status") == "True"
        labels = meta.get("labels", {})
        is_worker = not {"node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master"} & labels.keys()
        if is_worker:
            worker_count += 1
            ready_workers += int(is_ready)
        if not is_ready:
            finding(name, "NodeNotReady", ready.get("reason") or "Ready condition absent")
        if node.get("spec", {}).get("unschedulable"):
            finding(name, "NodeCordoned", "Node is marked unschedulable")
        for kind in sorted(PRESSURES & conditions.keys()):
            if conditions[kind].get("status") == "True":
                finding(name, kind, conditions[kind].get("reason", kind))

    if expected_workers is not None and (worker_count != expected_workers or ready_workers != expected_workers):
        finding("cluster/workers", "WorkerCountMismatch",
                f"expected={expected_workers}, registered={worker_count}, ready={ready_workers}")

    for pod in pods:
        meta = pod["metadata"]
        status = pod.get("status", {})
        if meta.get("deletionTimestamp") or status.get("phase") == "Succeeded":
            continue
        name = f"pod/{meta.get('namespace', 'default')}/{meta['name']}"
        for condition in status.get("conditions", []):
            if condition.get("type") == "PodScheduled" and condition.get("status") == "False":
                finding(name, condition.get("reason") or "PodUnscheduled",
                        condition.get("message") or "Pod is not scheduled")
        for container in status.get("initContainerStatuses", []) + status.get("containerStatuses", []):
            waiting = container.get("state", {}).get("waiting", {})
            if waiting.get("reason") in BAD_WAIT:
                finding(name, waiting["reason"], container["name"])
        if status.get("phase") == "Failed":
            finding(name, status.get("reason") or "PodFailed", status.get("message") or "Pod failed")

    for deployment in deployments:
        meta = deployment["metadata"]
        name = f"deployment/{meta.get('namespace', 'default')}/{meta['name']}"
        desired = deployment.get("spec", {}).get("replicas", 1)
        status = deployment.get("status", {})
        available = status.get("availableReplicas", 0)
        if available < desired:
            finding(name, "ReplicaShortfall", f"desired={desired}, available={available}")
        if status.get("observedGeneration", 0) < meta.get("generation", 0):
            finding(name, "GenerationPending", "Controller has not observed the current spec")
        for condition in status.get("conditions", []):
            if condition.get("type") == "Progressing" and condition.get("status") == "False":
                finding(name, condition.get("reason") or "RolloutStalled",
                        condition.get("message") or "Rollout stalled")

    return {"status": "degraded" if findings else "healthy",
            "workers": {"expected": expected_workers, "registered": worker_count, "ready": ready_workers},
            "findings": findings}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True, help="explicit kubectl context")
    parser.add_argument("--namespace", default="weather")
    parser.add_argument("--expected-workers", type=int)
    parser.add_argument("--management-context", help="Cluster API management cluster context")
    parser.add_argument("--capi-cluster", help="Cluster API Cluster object name")
    parser.add_argument("--capi-namespace", default="default")
    args = parser.parse_args(argv)
    if args.expected_workers is not None and args.expected_workers < 0:
        parser.error("--expected-workers must be nonnegative")
    if bool(args.management_context) != bool(args.capi_cluster):
        parser.error("--management-context and --capi-cluster must be supplied together")
    if args.capi_cluster and args.expected_workers is not None:
        parser.error("choose the Cluster API topology or --expected-workers")
    expected = args.expected_workers
    try:
        if args.capi_cluster:
            expected = capi_expected_workers(kubectl_json(
                args.management_context, "clusters.cluster.x-k8s.io", args.capi_namespace, args.capi_cluster,
            ))
        report = inspect(
            kubectl_json(args.context, "nodes")["items"],
            kubectl_json(args.context, "pods", args.namespace)["items"],
            kubectl_json(args.context, "deployments", args.namespace)["items"],
            expected,
        )
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "error", "error": getattr(exc, "stderr", None) or str(exc)}),
              file=sys.stderr)
        return 2
    print(json.dumps({"context": args.context, "namespace": args.namespace, **report}, indent=2))
    return 1 if report["findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
