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

## Cluster API lifecycle lab

This is a separate exercise for changing actual worker-node count. Use the [Cluster API Docker-provider Quick Start](https://cluster-api.sigs.k8s.io/user/quick-start), including workload networking/CNI and Docker Desktop kubeconfig instructions. The Docker provider is for development. The provided management-cluster config mounts the host Docker socket; reserve it for a disposable local lab.

1. Create the management cluster: `kind create cluster --name weather-management --config k8s/capi-management.yaml`. In a shell with that cluster selected, set `CLUSTER_TOPOLOGY=true` and run `clusterctl init --infrastructure docker` following the official guide. Choose compatible Kubernetes and Cluster API versions from the guide rather than assuming the locally installed versions match.
2. Generate a ClusterClass-based Docker development manifest named `weather-workload` with one control-plane machine and one worker. Apply it to the management cluster, complete the Quick Start's CNI step, and register its kubeconfig as a separate context. You can point the CLI at the workload context immediately for Node checks; if deploying the demo there, ensure the image is loaded on every schedulable worker or publish it to a registry you control and adjust the manifest's image and pull policy.
3. Change the worker replica count in the desired Cluster topology from 1 to 2, then reapply the manifest. Watch Cluster API reconcile. Inspect the expected count and actual Nodes while provisioning and after the second Node becomes Ready. If a minor-version upgrade is in scope, follow the [Cluster API upgrade guide](https://cluster-api.sigs.k8s.io/tasks/upgrading-clusters) and verify Node versions and readiness separately.

```bash
# Replace <workload-context> with the context you registered.
python3 inspect_cluster.py --context '<workload-context>' \
  --management-context kind-weather-management --capi-cluster weather-workload
```

The CLI reads the declared replica count from the managed topology's MachineDeployments. Explicit replica counts are required; autoscaler-managed counts and MachinePools are outside this example. For Node-only inspection without the demo, Pod and Deployment reads in the default `weather` namespace can return empty lists; if your API or RBAC rejects the namespace, pass `--namespace default`.

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
