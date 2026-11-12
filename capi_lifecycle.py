"""Correlate Cluster API worker intent, Machines and live workload Nodes."""

import argparse
import json
import subprocess
import sys

from inspect_cluster import capi_expected_workers, kubectl_json

CLUSTER = "clusters.cluster.x-k8s.io"
DEPLOYMENT = "machinedeployments.cluster.x-k8s.io"
MACHINESET = "machinesets.cluster.x-k8s.io"
MACHINE = "machines.cluster.x-k8s.io"
CLUSTER_LABEL = "cluster.x-k8s.io/cluster-name"
CONTROL_LABELS = {"cluster.x-k8s.io/control-plane", "node-role.kubernetes.io/control-plane",
                  "node-role.kubernetes.io/master"}
BLOCK_CREATE = "cluster.x-k8s.io/disable-machine-create"


def condition(resource, key):
    return next((c for c in resource.get("status", {}).get("conditions", [])
                 if c["type"] == key), {})


def worker(resource):
    return not CONTROL_LABELS.intersection(resource["metadata"].get("labels", {}))


def summarize(cluster, deployments, sets, machines, nodes):
    expected = capi_expected_workers(cluster)
    by_node = {n["metadata"]["name"]: n for n in nodes if worker(n)}
    records = []
    findings = []
    active_machines = sum(worker(m) and not m["metadata"].get("deletionTimestamp") for m in machines)
    deployment_replicas = sum(d.get("spec", {}).get("replicas", 0) for d in deployments)

    def finding(resource, reason, message):
        findings.append({"resource": resource, "reason": reason, "message": message})

    if expected != deployment_replicas:
        finding("topology", "WaitingForMachineDeployment",
                f"Cluster topology requests {expected}; MachineDeployments request {deployment_replicas}")
    for item in sets:
        if item["metadata"].get("annotations", {}).get(BLOCK_CREATE) != "true":
            continue
        desired = item.get("spec", {}).get("replicas", 0)
        owned = sum(any(ref.get("uid") == item["metadata"]["uid"] and ref.get("controller")
                        for ref in m["metadata"].get("ownerReferences", []))
                    and not m["metadata"].get("deletionTimestamp") for m in machines)
        if owned < desired:
            finding("machineset/" + item["metadata"]["name"], "MachineCreationDisabled",
                    f"{BLOCK_CREATE}=true; {owned}/{desired} Machines present")
    if active_machines != expected:
        finding("cluster/machines", "MachineCountMismatch", f"expected={expected}, active={active_machines}")

    for m in machines:
        if not worker(m):
            continue
        meta = m["metadata"]
        status = m.get("status", {})
        node_name = (status.get("nodeRef") or {}).get("name")
        node = by_node.get(node_name)
        ready = condition(node or {}, "Ready")
        stage, message = "Ready", ""
        if meta.get("deletionTimestamp"):
            stage = "Deleting"
            message = condition(m, "Deleting").get("message", "")
        elif not node:
            for key, pending in (("BootstrapConfigReady", "WaitingForBootstrap"),
                                 ("InfrastructureReady", "WaitingForInfrastructure")):
                state = condition(m, key)
                if state and state.get("status") != "True":
                    stage, message = pending, state.get("message") or state.get("reason", "")
                    break
            else:
                stage, message = "WaitingForNode", "No corresponding worker Node is registered"
        elif ready.get("status") != "True":
            stage, message = "NodeNotReady", ready.get("message") or ready.get("reason", "Ready condition absent")
        elif node.get("spec", {}).get("unschedulable"):
            stage, message = "NodeCordoned", "Worker is Ready but cordoned"
        if stage != "Ready":
            finding("machine/" + meta["name"], stage, message)
        records.append({"machine": meta["name"], "node": node_name, "stage": stage})

    live = {r["node"] for r in records if r["node"] in by_node and r["stage"] != "Deleting"}
    if set(by_node) - live:
        finding("cluster/nodes", "UnmatchedNodes", "Unmatched or deleting workers: "
                + ", ".join(sorted(set(by_node) - live)))
    schedulable = sum(r["stage"] == "Ready" for r in records)
    if schedulable != expected or len(by_node) != expected:
        finding("cluster/nodes", "WorkerCountMismatch",
                f"expected={expected}, registered={len(by_node)}, ready_and_uncordoned={schedulable}")
    return {"status": "converged" if not findings else "reconciling",
            "workers": {"desired": expected, "deployment_replicas": deployment_replicas,
                        "machines": active_machines, "registered": len(by_node),
                        "ready_and_uncordoned": schedulable},
            "machines": sorted(records, key=lambda record: record["machine"]),
            "findings": findings}


def snapshot(management, workload, cluster_name, namespace="default"):
    label = f"{CLUSTER_LABEL}={cluster_name}"

    def get(resource):
        return kubectl_json(management, resource, namespace, selector=label)["items"]

    return summarize(kubectl_json(management, CLUSTER, namespace, cluster_name),
                     get(DEPLOYMENT), get(MACHINESET), get(MACHINE), kubectl_json(workload, "nodes")["items"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--management-context", required=True)
    parser.add_argument("--workload-context", required=True)
    parser.add_argument("--cluster", required=True)
    parser.add_argument("--namespace", default="default")
    args = parser.parse_args(argv)
    try:
        state = snapshot(args.management_context, args.workload_context, args.cluster, args.namespace)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "error", "error": getattr(exc, "stderr", None) or str(exc)}),
              file=sys.stderr)
        return 2
    print(json.dumps(state, indent=2))
    return 0 if state["status"] == "converged" else 1


if __name__ == "__main__":
    raise SystemExit(main())
