# Failure and recovery

Run `python scripts/demo.py exercise --service payments` after bootstrap.

1. Verify a healthy deployment and a successful Prometheus scrape.
2. Build a distinct `v2-failure` image from the generated service.
3. Commit its image reference locally, then push to local Git.
4. Verify HTTP 500 with healthy process probes and a firing error-rate alert.
5. Revert the failure commit, push the recovery, and verify HTTP 200 and alert resolution.

The local commit SHA is recorded before push. A push failure therefore still enters recovery. If recovery also fails, the report preserves both errors. A failed run must not be described as successful just because its cleanup completed.

Reports are under `work/keel/reports/`. On failure, inspect the matching diagnostics JSON and:

```bash
export KUBECONFIG="$PWD/work/keel/kubeconfig"
kubectl --context kind-keel-local -n argocd get application payments -o yaml
git -C work/keel/gitops log -5 --oneline
```

Do not use imperative rollout undo with automated reconciliation. Recover desired state in Git. Dirty working copies are rejected so the CLI cannot silently include manual edits in its next deployment.

If a bootstrap stage fails, fix the reported cause and re-run `up`. When there are uncommitted generated changes, inspect them first; the CLI preserves them rather than discarding them. Generated source is never overwritten by repeated bootstrap.

Cluster ownership is tied to the workspace and the kind control-plane container ID. A missing or mismatched ownership record blocks reuse and deletion. Older records without a container ID also fail closed; do not bypass this check by creating a marker file. Inspect the cluster before any manual cleanup.
