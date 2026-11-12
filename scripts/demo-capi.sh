#!/usr/bin/env bash
# Linux Docker Engine only. Management cluster must be dedicated to this lab.
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077

capi_version=v1.12.11
k8s_version=v1.34.3
management=kind-weather-management
workload=weather-workload
cidr=192.168.0.0/16
mgmt=(kubectl --context "$management")
scratch=$(mktemp -d)
cleanup() {
  local code=$?
  if [ "$code" -ne 0 ] && [ -n "${workload_context:-}" ]; then
    kubectl --context "$workload_context" --request-timeout=10s get nodes,pods -A -o wide || true
    kubectl --context "$workload_context" --request-timeout=10s -n kube-system \
      describe daemonset/kindnet || true
    kubectl --context "$workload_context" --request-timeout=10s -n kube-system \
      logs -l app=kindnet --all-containers --prefix --tail=35 || true
    kubectl --context "$workload_context" --request-timeout=10s -n kube-system \
      logs -l k8s-app=kube-proxy --all-containers --prefix --tail=30 || true
    kubectl --context "$workload_context" --request-timeout=10s -n kube-system \
      logs -l k8s-app=kube-proxy --previous --prefix --tail=30 || true
    kubectl --context "$workload_context" --request-timeout=10s get events -A \
      --sort-by=.metadata.creationTimestamp | tail -55 || true
  fi
  rm -rf -- "$scratch"
}
trap cleanup EXIT

if ! "${mgmt[@]}" get node weather-management-control-plane >/dev/null; then
  echo "Create the dedicated weather-management kind cluster first." >&2
  exit 2
fi

export CLUSTER_TOPOLOGY=true EXP_MACHINE_POOL=true POD_SECURITY_STANDARD_ENABLED=false
export POD_CIDR='["192.168.0.0/16"]'
clusterctl init --kubeconfig-context "$management" --core "cluster-api:$capi_version" \
  --bootstrap "kubeadm:$capi_version" --control-plane "kubeadm:$capi_version" \
  --infrastructure "docker:$capi_version" --wait-providers --wait-provider-timeout 300
"${mgmt[@]}" -n capd-system wait --for=jsonpath='{.subsets[0].addresses[0].ip}' \
  endpoints/capd-webhook-service --timeout=90s

if [ -n "$("${mgmt[@]}" get clusters.cluster.x-k8s.io -n default -o name)" ]; then
  echo "Use a fresh dedicated management cluster without existing workload Clusters." >&2
  exit 2
fi
clusterctl generate cluster "$workload" --kubeconfig-context "$management" --infrastructure docker --flavor development \
  --kubernetes-version "$k8s_version" --control-plane-machine-count 1 --worker-machine-count 1 \
  > "$scratch/cluster.yaml"
for attempt in 1 2 3 4 5 6; do
  if output=$("${mgmt[@]}" apply -f "$scratch/cluster.yaml" 2>&1); then
    printf '%s\n' "$output"
    break
  fi
  printf '%s\n' "$output" >&2
  if [ "$attempt" = 6 ] || ! grep -Eq 'failed calling webhook.*(connection refused|no endpoints available)' <<< "$output"; then
    exit 1
  fi
  sleep 3
done
"${mgmt[@]}" -n default wait --for=create "secret/$workload-kubeconfig" --timeout=300s
clusterctl get kubeconfig "$workload" --kubeconfig-context "$management" > "$scratch/workload.kubeconfig"
workload_context=$(kubectl --kubeconfig "$scratch/workload.kubeconfig" config current-context)
workload_api=$(kubectl --kubeconfig "$scratch/workload.kubeconfig" config view --minify \
  -o jsonpath='{.clusters[0].cluster.server}')
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}:$scratch/workload.kubeconfig"
wk=(kubectl --context "$workload_context")

echo "Waiting for the new workload API"
for _ in $(seq 1 75); do
  if "${wk[@]}" --request-timeout=4s get --raw=/readyz > /dev/null 2>&1; then break; fi
  sleep 4
done
"${wk[@]}" --request-timeout=10s get --raw=/readyz
# Copy the kind CNI spec, rebind its bootstrap API endpoint and pod range to the
# workload cluster, and leave behind source metadata and service account tokens.
"${mgmt[@]}" -n kube-system get clusterrole/kindnet clusterrolebinding/kindnet \
  serviceaccount/kindnet daemonset/kindnet -o json > "$scratch/source-kindnet.json"
python3 - "$scratch/source-kindnet.json" "$cidr" "$workload_api" > "$scratch/kindnet.json" <<'PY'
import json
import sys
from urllib.parse import urlsplit

with open(sys.argv[1]) as stream:
    manifests = json.load(stream)
api = urlsplit(sys.argv[3])
if api.scheme != "https" or not api.hostname:
    raise ValueError("expected an HTTPS API endpoint from the workload kubeconfig")
# kindnet prefers its explicit endpoint over Kubernetes' service environment.
values = {"POD_SUBNET": sys.argv[2], "CONTROL_PLANE_ENDPOINT": api.netloc,
          "KUBERNETES_SERVICE_HOST": api.hostname, "KUBERNETES_SERVICE_PORT": str(api.port or 443)}
for item in manifests["items"]:
    item["metadata"] = {k: v for k, v in item["metadata"].items() if k in {"name", "namespace"}}
    item.pop("status", None)
    if item["kind"] == "ServiceAccount":
        item.pop("secrets", None)
        item.pop("imagePullSecrets", None)
    if item["kind"] == "DaemonSet":
        (kindnet,) = [c for c in item["spec"]["template"]["spec"]["containers"] if c["name"] == "kindnet-cni"]
        kindnet["env"] = [v for v in kindnet["env"] if v["name"] not in values]
        kindnet["env"].extend({"name": k, "value": v} for k, v in values.items())
print(json.dumps(manifests))
PY
"${wk[@]}" apply -f "$scratch/kindnet.json"
# Match kind's kubeadm configuration for container nodes: do not try to change
# the shared Linux conntrack limit from the workload kube-proxy.
"${wk[@]}" -n kube-system get configmap/kube-proxy -o jsonpath='{.data.config\.conf}' > "$scratch/kube-proxy-config.yaml"
python3 - "$scratch/kube-proxy-config.yaml" > "$scratch/kube-proxy-patch.json" <<'PY'
import json
import re
import sys

with open(sys.argv[1]) as stream:
    config, count = re.subn(r"(?m)^(\s+maxPerCore:) *[0-9]+$", r"\1 0", stream.read())
if count != 1:
    raise ValueError("expected exactly one kube-proxy conntrack maxPerCore setting")
print(json.dumps({"data": {"config.conf": config}}))
PY
"${wk[@]}" -n kube-system patch configmap/kube-proxy --type=merge --patch-file "$scratch/kube-proxy-patch.json"
"${wk[@]}" -n kube-system rollout restart daemonset/kube-proxy
"${wk[@]}" -n kube-system rollout status daemonset/kindnet --timeout=100s
"${wk[@]}" -n kube-system rollout status daemonset/kube-proxy --timeout=100s
"${wk[@]}" -n kube-system rollout status deployment/coredns --timeout=100s
docker build -t weather-agent:demo .
docker save -o "$scratch/weather-image.tar" weather-agent:demo

python3 -m scripts.capi_demo --workload-context "$workload_context" --image-archive "$scratch/weather-image.tar"
