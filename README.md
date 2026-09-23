# Keel / Kubernetes Delivery

**Ship a service. Break a release. Recover through Git.**

Keel demonstrates a complete delivery and recovery loop on a local Kubernetes cluster. Generate a service, deploy it with Argo CD, introduce an application failure, and verify recovery against both HTTP responses and Prometheus alerts.

[Run it](#run-locally) · [Design decisions](docs/architecture.md) · [Recovery walkthrough](docs/recovery.md)

## A release is only half the story

A healthy pod does not guarantee a working service. Keel deliberately ships an image that returns HTTP 500 while its process probes stay healthy. The exercise checks whether monitoring detects the failure and whether reverting the desired state actually restores service.

| Deploy | Detect | Recover |
| --- | --- | --- |
| Build a versioned image and reconcile through Argo CD | Observe HTTP 500 and a firing Prometheus alert | Revert in Git; verify HTTP 200 and alert resolution |

Kubernetes manifests stay in YAML and Kustomize. Python coordinates established tools and verifies outcomes; it does not introduce another controller or configuration framework.

## What CI verifies

The [verification workflow](.github/workflows/verify.yml) builds a fresh cluster and checks:

- Lint and unit tests, followed by repeated bootstrap and two-service delivery.
- HTTP failure detection, a firing alert, Git recovery and alert resolution.
- Network boundaries, anonymous Git push rejection and Git persistence after pod replacement.
- Source dependencies, secrets and the built application and Git images, failing on HIGH/CRITICAL findings.

Each run uploads operation reports and available scan results as the `local-verification` artifact. Failed platform commands also collect cluster diagnostics. Logs and artifacts belong to the execution that produced them; they are not a permanent security guarantee. Upstream Argo CD, Cilium and Prometheus images are outside the custom-image scan scope.

## Run locally

Prerequisites: Docker running, kind 0.33, kubectl 1.37, Helm 4, Git, and Python 3.12 or 3.14 on macOS/Linux. Allow 6 GB of Docker memory and internet access for the first build.

Ruff 0.16.6 is optional locally for linting; CI installs the pinned version.

```bash
python3 -m venv work/venv
source work/venv/bin/activate
pip install --require-hashes -r requirements.txt
python scripts/dependencies.py

python scripts/scaffold.py payments --owner payments-team --output work/services/payments
python scripts/demo.py up --service-dir work/services/payments
python scripts/demo.py verify --service payments
python scripts/demo.py exercise --service payments
```

Run the scaffold command once: it refuses to overwrite existing work. After registration, `up --service payments` reuses the recorded source directory; use `--service-dir` to select another source explicitly. The shorter `up --service orders` generates a default-owned service when none is registered and deploys it through the same path.

Each execution writes a unique JSON report under `work/keel/reports/`. Failed commands retain their failed stages and cluster diagnostics. See the [recovery walkthrough](docs/recovery.md) for the checks and troubleshooting commands.

## Inspect and test

```bash
python -m unittest discover -s tests -v
ruff check scripts service tests
python tests/integration.py
python scripts/demo.py status
```

The integration tests require `payments` to be deployed. They check allowed/denied network access, rejection of anonymous Git pushes, and Git persistence after pod replacement.

The cluster is `keel-local`; its kubeconfig stays in `work/keel/kubeconfig`. Your default Kubernetes context is not changed. Deployment Git pushes use the Kubernetes API against this local cluster; the delivery flow does not require a GitHub remote.

## Cleanup

```bash
python scripts/demo.py down --confirm-cluster keel-local
```

This removes the dedicated cluster and its in-cluster volumes. Generated services, local Git history and reports remain on disk. Re-run `up` for each service to restore it. Other clusters are not removed.

## Scope, deliberately small

One node, a demonstration HTTP workload and a short-window alert. No HA claim, cloud IAM integration, external paging or dashboard. The focus is repeatable delivery, explicit boundaries and recovery evidence—not the number of tools in the stack.

Licensed under [MIT](LICENSE). Third-party dependencies retain their own licenses.
