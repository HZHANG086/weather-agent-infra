import unittest

from capi_lifecycle import BLOCK_CREATE, summarize


def topology(count):
    return {"spec": {"topology": {"workers": {"machineDeployments": [{"name": "md-0", "replicas": count}]}}}}


def machine(name, *, node=None, deleting=False, conditions=()):
    metadata = {"name": name, "ownerReferences": [{"uid": "set-1", "controller": True}]}
    if deleting:
        metadata["deletionTimestamp"] = "2026-11-12T00:00:00Z"
    return {"metadata": metadata, "status": {"nodeRef": {"name": node} if node else None,
                                            "conditions": conditions}}


def node(name, *, ready="True", cordoned=False):
    return {"metadata": {"name": name}, "spec": {"unschedulable": cordoned},
            "status": {"conditions": [{"type": "Ready", "status": ready}]}}


class LifecycleTest(unittest.TestCase):
    def test_can_distinguish_topology_and_provider_delay(self):
        first = machine("first", node="worker-1")
        nodes = [node("worker-1")]
        propagation = summarize(topology(2), [{"spec": {"replicas": 1}}], [], [first], nodes)
        self.assertIn("WaitingForMachineDeployment", [f["reason"] for f in propagation["findings"]])

        paused_set = {"metadata": {"name": "md-0", "uid": "set-1", "annotations": {BLOCK_CREATE: "true"}},
                      "spec": {"replicas": 2}}
        blocked = summarize(topology(2), [{"spec": {"replicas": 2}}], [paused_set], [first], nodes)
        self.assertIn("MachineCreationDisabled", [f["reason"] for f in blocked["findings"]])
        self.assertEqual(blocked["workers"]["machines"], 1)

        second = machine("second", conditions=[{"type": "InfrastructureReady", "status": "False",
                                                "reason": "Provisioning", "message": "waiting for a container"}])
        pending = summarize(topology(2), [{"spec": {"replicas": 2}}], [], [first, second], nodes)
        self.assertEqual(pending["workers"]["machines"], 2)
        self.assertIn({"resource": "machine/second", "reason": "WaitingForInfrastructure",
                       "message": "waiting for a container"}, pending["findings"])

    def test_readiness_and_scale_down_require_live_nodes(self):
        machines = [machine("first", node="worker-1"), machine("second", node="worker-2")]
        two = [{"spec": {"replicas": 2}}]
        cordoned = summarize(topology(2), two, [], machines, [node("worker-1"), node("worker-2", cordoned=True)])
        self.assertIn("NodeCordoned", [f["reason"] for f in cordoned["findings"]])
        ready = summarize(topology(2), two, [], machines, [node("worker-1"), node("worker-2")])
        self.assertEqual(ready["status"], "converged")

        one = [{"spec": {"replicas": 1}}]
        deleting = [machines[0], machine("second", node="worker-2", deleting=True)]
        shrinking = summarize(topology(1), one, [], deleting, [node("worker-1"), node("worker-2")])
        self.assertEqual(shrinking["status"], "reconciling")
        self.assertIn("Deleting", [f["reason"] for f in shrinking["findings"]])
        self.assertEqual(summarize(topology(1), one, [], machines[:1], [node("worker-1")])["status"],
                         "converged")
