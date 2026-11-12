#!/usr/bin/env bash
# Run only against the dedicated disposable cluster described in the README.
set -euo pipefail
cd "$(dirname "$0")/.."
cluster=weather-infra
context=kind-weather-infra
k=(kubectl --context "$context")
report=(python3 inspect_cluster.py --context "$context" --expected-workers 2)

docker build -t weather-agent:demo .
kind load docker-image --name "$cluster" weather-agent:demo
"${k[@]}" apply -f k8s/weather.yaml
"${k[@]}" -n weather rollout status deployment/weather-agent --timeout=120s
"${report[@]}"

if [ "${1:-}" = "--node-failure" ]; then
  worker=weather-infra-worker2
  docker pause "$worker"
  trap 'docker unpause "$worker"' EXIT
  detected=false
  for _ in $(seq 1 40); do
    status=0
    result=$("${report[@]}") || status=$?
    if [ "$status" -eq 2 ]; then exit 2; fi
    if [ "$status" -eq 1 ] &&
      printf '%s\n' "$result" | grep -A1 "\"resource\": \"node/$worker\"" | grep -q '"reason": "NodeNotReady"'; then
      printf '%s\n' "$result"
      detected=true
      break
    fi
    sleep 3
  done
  if [ "$detected" != true ]; then
    echo "Expected NotReady worker was not detected" >&2
    exit 1
  fi
  docker unpause "$worker"
  trap - EXIT
  "${k[@]}" wait --for=condition=Ready "node/$worker" --timeout=120s
  "${k[@]}" -n weather rollout status deployment/weather-agent --timeout=120s
  "${report[@]}"
fi
"${k[@]}" -n weather exec deployment/weather-agent -- python -c \
  'import json, urllib.request; data=json.load(urllib.request.urlopen("http://weather-agent:8080/ask?city=hangzhou&intent=trend", timeout=5)); assert data["trend"] == "up" and len(data["evidence"]) == 2, data'

# Change a pod resource request to an impossible value. Unlike an image pull,
# this reliably exercises the Kubernetes scheduler without accessing a registry.
"${k[@]}" -n weather set resources deployment/weather-agent -c api --requests=cpu=1000,memory=32Mi --limits=cpu=1000,memory=128Mi
rollback() {
  "${k[@]}" -n weather rollout undo deployment/weather-agent
}
trap rollback EXIT
detected=false
for _ in $(seq 1 20); do
  status=0
  result=$("${report[@]}") || status=$?
  if [ "$status" -eq 2 ]; then exit 2; fi
  if [ "$status" -eq 1 ] && printf '%s\n' "$result" | grep -q '"reason": "Unschedulable"'; then
    printf '%s\n' "$result"
    detected=true
    break
  fi
  sleep 2
done
if [ "$detected" != true ]; then
  echo "Expected scheduling failure was not detected" >&2
  exit 1
fi
rollback
trap - EXIT
"${k[@]}" -n weather rollout status deployment/weather-agent --timeout=120s
"${report[@]}"
