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
trap 'rm -rf -- "$scratch"' EXIT

if ! "${mgmt[@]}" get node weather-management-control-plane >/dev/null; then
  echo "Create the dedicated weather-management kind cluster first." >&2
  exit 2
fi

export CLUSTER_TOPOLOGY=true EXP_MACHINE_POOL=true POD_SECURITY_STANDARD_ENABLED=false
export POD_CIDR='["192.168.0.0/16"]'
clusterctl init --kubeconfig-context "$management" --core "cluster-api:$capi_version" \
  --bootstrap "kubeadm:$capi_version" --control-plane "kubeadm:$capi_version" \
  --infrastructure "docker:$capi_version" --wait-providers --wait-provider-timeout 300

if [ -n "$("${mgmt[@]}" get clusters.cluster.x-k8s.io -n default -o name)" ]; then
  echo "Use a fresh dedicated management cluster without existing workload Clusters." >&2
  exit 2
fi
clusterctl generate cluster "$workload" --kubeconfig-context "$management" --infrastructure docker --flavor development \
  --kubernetes-version "$k8s_version" --control-plane-machine-count 1 --worker-machine-count 1 \
  > "$scratch/cluster.yaml"
"${mgmt[@]}" apply -f "$scratch/cluster.yaml"
"${mgmt[@]}" -n default wait --for=create "secret/$workload-kubeconfig" --timeout=300s
clusterctl get kubeconfig "$workload" --kubeconfig-context "$management" > "$scratch/workload.kubeconfig"
workload_context=$(kubectl --kubeconfig "$scratch/workload.kubeconfig" config current-context)
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}:$scratch/workload.kubeconfig"
wk=(kubectl --context "$workload_context")

echo "Waiting for the new workload API"
for _ in $(seq 1 75); do
  if "${wk[@]}" --request-timeout=4s get --raw=/readyz > /dev/null 2>&1; then break; fi
  sleep 4
done
"${wk[@]}" --request-timeout=10s get --raw=/readyz
curl -fsSL "https://raw.githubusercontent.com/kubernetes-sigs/cluster-api/$capi_version/test/e2e/data/cni/kindnet/kindnet.yaml" \
  -o "$scratch/kindnet-template.yaml"
sed "s|\${DOCKER_POD_CIDRS},\${DOCKER_POD_IPV6_CIDRS}|$cidr|" "$scratch/kindnet-template.yaml" > "$scratch/kindnet.yaml"
"${wk[@]}" apply -f "$scratch/kindnet.yaml"
docker build -t weather-agent:demo .
docker save -o "$scratch/weather-image.tar" weather-agent:demo

python3 -m scripts.capi_demo --workload-context "$workload_context" --image-archive "$scratch/weather-image.tar"
