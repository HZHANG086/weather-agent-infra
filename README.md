# Weather Agent Infra

A self-contained Kubernetes demo inspired by a knowledge-graph-enhanced weather agent. It combines an evidence-returning weather API, a Python cluster inspection CLI, and a failure-and-recovery lab. The weather observations are synthetic and the API uses deterministic rules; there is no live forecast, LLM dependency, lab code, or lab data in this repository.

The inspection CLI compares expected workers with registered and Ready workers, flags node pressure and cordons, and reports unschedulable Pods, common container failures, and Deployment readiness or rollout issues. It reads live objects via `kubectl`; it does not modify them.

## Run the API locally

Python 3.10+ is sufficient; there are no Python package dependencies.

```bash
python3 -m unittest discover -s tests -v
python3 weather_agent.py
# In a separate terminal:
curl -fsS 'http://127.0.0.1:8080/ask?city=hangzhou&intent=trend'
```

The response includes the IDs of two synthetic observations and a rainfall trend. Try `intent=alert` for a synthetic threshold check, or `city=unknown` for an evidence refusal. `/healthz` and `/readyz` serve the Kubernetes probes.

## Kubernetes deployment and recovery

Requires a running Docker engine, [kind](https://kind.sigs.k8s.io/docs/user/quick-start/) and `kubectl`.

```bash
kind create cluster --name weather-infra --config k8s/kind.yaml --wait 120s
bash scripts/demo-kind.sh
```

The script builds and loads the local image, deploys the service, and checks the weather API through the Kubernetes Service. It then changes the Deployment's CPU request to 1000 cores to make a new Pod unschedulable. The script checks that the CLI reports the scheduling failure, rolls back the Deployment, and requires a healthy report. Add `--node-failure` to also pause a disposable worker container until Kubernetes reports it NotReady, then resume it and verify recovery. Run the script only on the dedicated disposable `kind-weather-infra` context. Both failure scenarios are configured for GitHub Actions in `.github/workflows/ci.yaml`.

```bash
python3 inspect_cluster.py --context kind-weather-infra --expected-workers 2
kubectl --context kind-weather-infra -n weather port-forward service/weather-agent 8080:8080
```

The CLI exits with 0 for healthy, 1 for findings, and 2 when it cannot inspect. Worker counts assume the control-plane Nodes carry the standard control-plane or legacy master label. Deploying or scaling the service changes Pod replicas; it does not add worker Nodes.

## Weather RAG security regression

The Weather Agent also exposes a fixture `POST /rag/answer` endpoint. The existing `GET /ask` API is unchanged. The 12 synthetic evaluation cases are built around local rainfall observations, including the same 12 mm Hangzhou reading, injected instructions in retrieved evidence, and missing evidence.

After building and loading the image with the kind demo above, run:

```bash
python3 -m rag_infra.cluster --context kind-weather-infra --image weather-agent:demo --serve-weather
```

The command deploys baseline and regressed Weather Agent instances in an isolated namespace on the same cluster. It runs four Indexed Job shards with up to two in parallel. The baseline must pass; the regressed fixture must complete all 12 cases but block release on one injection case; a separate oversized CPU request must produce a scheduler failure and an incomplete report. The release gate also checks that the test dataset and backend type agree. Reports in `artifacts/rag-eval/` distinguish application regressions from incomplete execution and include the scheduler reason. GitHub Actions publishes the reports. These are workflow checks with deterministic fixtures, not real-model safety results.

## Cluster API lifecycle lab

The [lifecycle workflow](.github/workflows/capi-lifecycle.yaml) starts a management
cluster and uses Cluster API's Docker provider to create a separate workload
cluster. It changes actual worker Nodes through the ClusterClass topology:

1. Start with one worker, deploy the demo Weather Agent and check a synthetic rainfall query.
2. Disable Machine creation on the existing MachineSet and request two workers.
   Verify that the requested count reaches the MachineDeployment, the second
   Machine is blocked, and the original worker still serves the query.
3. Remove the block. Wait for two Ready workers, then schedule a probe on the new
   worker. It must resolve and query the existing Weather Agent Service.
4. Request one worker and verify the new Machine and its Node are removed.
   Repeat the service probe on the original worker.

The [read-only CLI](capi_lifecycle.py) follows topology → MachineDeployment →
Machine → Node. It distinguishes delayed propagation, disabled Machine creation,
bootstrap or infrastructure delays, missing Nodes, and unready or cordoned
Nodes. The workflow saves observations and probe responses as a JSON artifact.
The workload is a demo with synthetic observations. The probes check specific
points during the lifecycle; they are not an uptime benchmark.

To run the automation locally on Linux with Docker Engine, install `kind`,
`kubectl`, and `clusterctl` v1.12.11 and use a fresh dedicated cluster:

```bash
kind create cluster --name weather-management --config k8s/capi-management.yaml \
  --image kindest/node:v1.34.3 --wait 120s
bash scripts/demo-capi.sh
```

The management cluster mounts the Docker socket for the development-only
provider. Use a disposable host or runner. The hosted workflow increases the
runner's Linux inotify budget because nested nodes share the host limit; see
the [Cluster API Docker prerequisites](https://cluster-api.sigs.k8s.io/user/quick-start)
if a local kube-proxy logs `too many open files`. To clean up a local run, delete the
workload Cluster first so Cluster API can remove its Docker containers, then
delete the management cluster:

```bash
kubectl --context kind-weather-management delete clusters.cluster.x-k8s.io weather-workload --timeout=5m
kind delete cluster --name weather-management
```

For Docker Desktop, follow the [Cluster API Quick Start](https://cluster-api.sigs.k8s.io/user/quick-start)
for workload kubeconfig access and run the read-only CLI against both contexts:

```bash
# Replace <workload-context> with the context you registered.
python3 capi_lifecycle.py --workload-context '<workload-context>' \
  --management-context kind-weather-management --cluster weather-workload
```

The lifecycle CLI expects explicit worker replica counts on a ClusterClass
topology. MachinePools and autoscaler-managed worker counts are outside this lab.

## Publish to GitHub

Use this directory as a standalone repository. It intentionally contains no institutional source, original observations, credentials, or CV files. To review it privately first, authenticate with `gh auth login` and run the following from this directory. Omit `git init`, staging and commit if already prepared locally:

```bash
git init -b main
git add -- .github .gitignore .dockerignore Dockerfile README.md weather_agent.py inspect_cluster.py k8s scripts tests
git diff --cached --name-only
git commit -m "Add weather API and Kubernetes inspection demo"
gh repo create weather-agent-infra --private --source=. --remote=origin --push
```

Check the Actions run and review the files before making the repository public. Choose a license before presenting the code as open source. See [GitHub CLI documentation](https://cli.github.com/manual/gh_repo_create) for repository creation options.
