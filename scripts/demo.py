"""Keel local delivery CLI. Never publishes to an external Git host."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import yaml
from string import Template

from scaffold import generate, validate_identifier

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work" / "keel"
REPO = WORK / "gitops"
CONTEXT = "kind-keel-local"
CLUSTER = "keel-local"
KUBECONFIG = WORK / "kubeconfig"
REPORT = None


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2) + "\n")
    temp.replace(path)


class Report:
    def __init__(self, action):
        self.path = WORK / "reports" / f"{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}.json"
        self.data = {"action": action, "status": "running", "stages": [],
                     "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        save(self.path, self.data)

    @contextlib.contextmanager
    def stage(self, name):
        entry = {"name": name, "status": "running"}
        self.data["stages"].append(entry)
        save(self.path, self.data)
        print(f"[stage] {name}", flush=True)
        started = time.monotonic()
        try:
            yield
        except BaseException as error:
            entry.update(status="failed", error=str(error))
            raise
        else:
            entry["status"] = "passed"
        finally:
            entry["seconds"] = round(time.monotonic() - started, 3)
            save(self.path, self.data)

    def finish(self, error=None):
        self.data.update(status="failed" if error else "passed",
                         finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        if error:
            self.data["error"] = str(error)
        save(self.path, self.data)


def stage(name):
    return REPORT.stage(name) if REPORT else contextlib.nullcontext()


def run(*args, cwd=ROOT, data=None, timeout=120):
    env = dict(os.environ, KUBECONFIG=str(KUBECONFIG),
               HELM_CACHE_HOME=str(WORK / "helm-cache"),
               HELM_CONFIG_HOME=str(WORK / "helm-config"),
               HELM_DATA_HOME=str(WORK / "helm-data"))
    try:
        result = subprocess.run(args, cwd=cwd, input=data, text=True, env=env,
                                timeout=timeout, capture_output=True, check=True)
        return result.stdout.strip()
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"Command timed out after {timeout}s: {args[0]}") from error
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"{args[0]} exited {error.returncode}: {error.stderr[-6000:]}") from error


def kube(*args, data=None, timeout=120):
    return run("kubectl", "--context", CONTEXT, *args, data=data, timeout=timeout)


def apply_template(path, **values):
    kube("apply", "-f", "-", data=Template(path.read_text()).substitute(values))


def git(*args):
    return run("git", "-c", "protocol.ext.allow=always", "-C", str(REPO), *args)


def commit(message):
    git("add", ".")
    if git("status", "--porcelain"):
        git("-c", "user.name=Keel Local", "-c", "user.email=keel@localhost", "commit", "-m", message)
    return git("rev-parse", "HEAD")


def read_service_registry():
    path = WORK / "state.json"
    return json.loads(path.read_text()) if path.exists() else {"services": {}}


def cluster_identity():
    return {"cluster": CLUSTER, "workspace": str(ROOT),
            "node_id": run("docker", "inspect", "--format", "{{.Id}}", CLUSTER + "-control-plane")}


def verify_cluster_ownership():
    marker = WORK / "managed.json"
    require(marker.exists() and KUBECONFIG.exists(), "No local cluster ownership record.")
    require(json.loads(marker.read_text()) == cluster_identity(), "Cluster ownership mismatch; refusing operation.")


def read_service_metadata(directory):
    directory = directory.resolve()
    meta = json.loads((directory / "keel.json").read_text())
    require(meta.get("schema_version") == 1, "Unsupported service contract version.")
    for key in ("name", "owner"):
        validate_identifier(meta[key])
    require((directory / "service" / "Dockerfile").is_file(), "Service Dockerfile is missing.")
    require((directory / "deploy/deployment.yaml").is_file(), "Generate a service with the current YAML template.")
    return meta


def build_service_image(source, name, version, broken=False):
    digest = hashlib.sha256(version.encode())
    for path in sorted((source / "service").rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            digest.update(str(path.relative_to(source)).encode())
            digest.update(path.read_bytes())
    tag = f"keel/{name}:{version}-{digest.hexdigest()[:16]}"
    run("docker", "build", "-t", tag, "--build-arg", f"APP_VERSION={version}",
        "--build-arg", f"FAIL_REQUESTS={str(broken).lower()}", str(source / "service"), timeout=600)
    run("kind", "load", "docker-image", tag, "--name", CLUSTER, timeout=180)
    return tag


def wait_sync(name, revision):
    kube("-n", "argocd", "annotate", "application", name,
         "argocd.argoproj.io/refresh=hard", "--overwrite")
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        status = json.loads(kube("-n", "argocd", "get", "application", name, "-o", "json")).get("status", {})
        if (status.get("sync", {}).get("revision") == revision
                and status.get("sync", {}).get("status") == "Synced"
                and status.get("health", {}).get("status") == "Healthy"):
            return
        time.sleep(2)
    raise RuntimeError(f"{name} did not become Synced/Healthy at {revision}")


@contextlib.contextmanager
def port_forward(namespace, service, remote):
    import select
    env = dict(os.environ, KUBECONFIG=str(KUBECONFIG))
    proc = subprocess.Popen(["kubectl", "--context", CONTEXT, "-n", namespace,
                             "port-forward", f"svc/{service}", f":{remote}", "--address=127.0.0.1"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    try:
        deadline = time.monotonic() + 15
        output = []
        port = None
        while time.monotonic() < deadline:
            if select.select([proc.stdout], [], [], 0.5)[0]:
                line = proc.stdout.readline()
                output.append(line)
                match = re.search(r"127.0.0.1:(\d+) ->", line)
                if match:
                    port = int(match.group(1))
                    break
            if proc.poll() is not None:
                break
        require(port is not None, "Port-forward failed: " + "".join(output))
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        proc.stdout.close()


def request(base, path="/"):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(base + path, timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def query_prometheus(base, expression):
    code, body = request(base, "/api/v1/query?" + urllib.parse.urlencode({"query": expression}))
    require(code == 200, "Prometheus query failed.")
    result = json.loads(body)
    require(result.get("status") == "success", f"Invalid Prometheus response: {body}")
    return result["data"]["result"]


def refresh_monitoring(names):
    directory = WORK / "monitoring"
    shutil.copytree(ROOT / "cluster/monitoring", directory, dirs_exist_ok=True)
    save(directory / "targets.json", [
        {"targets": [f"{name}.keel-apps.svc.cluster.local:8080"], "labels": {"job": name}}
        for name in names])
    kube("apply", "-k", str(directory))
    kube("-n", "keel-system", "rollout", "status", "deployment/prometheus", "--timeout=180s", timeout=200)


def bootstrap_platform(service_dir):
    with stage("preflight"):
        for binary in ("docker", "kind", "kubectl", "git", "helm"):
            require(shutil.which(binary), f"Missing dependency: {binary}")
        run("docker", "info")
        meta = read_service_metadata(service_dir)
        name = meta["name"]
        clusters = run("kind", "get", "clusters").splitlines()
        if CLUSTER in clusters:
            verify_cluster_ownership()
    with stage("cluster and policy enforcement"):
        if CLUSTER not in clusters:
            run("kind", "create", "cluster", "--name", CLUSTER,
                "--config", str(ROOT / "cluster/kind.yaml"), "--kubeconfig", str(KUBECONFIG), timeout=240)
            save(WORK / "managed.json", cluster_identity())
        # Kept in the workspace; reproducible chart checksum is checked before installation.
        chart = ROOT / "work/vendor/cilium-1.20.2.tgz"
        require(chart.exists(), "Run python3 scripts/dependencies.py first.")
        locks = json.loads((ROOT / "dependencies.lock.json").read_text())
        require(hashlib.sha256(chart.read_bytes()).hexdigest() == locks["cilium_chart_sha256"],
                "Cilium chart checksum mismatch.")
        run("helm", "upgrade", "--install", "cilium", str(chart), "--kube-context", CONTEXT,
            "-n", "kube-system", "--set", "ipam.mode=kubernetes", "--set", "operator.replicas=1",
            "--set", "envoy.enabled=false", "--wait", "--timeout", "240s", timeout=270)
        kube("wait", "--for=condition=Ready", "node", "--all", "--timeout=180s", timeout=200)
        kube("apply", "-f", str(ROOT / "cluster/namespaces.yaml"))
    with stage("persistent read-only Git service"):
        dockerfile = ROOT / "cluster/git/Dockerfile"
        tag = "keel/git:" + hashlib.sha256(dockerfile.read_bytes()).hexdigest()[:16]
        run("docker", "build", "-t", tag, str(dockerfile.parent), timeout=600)
        run("kind", "load", "docker-image", tag, "--name", CLUSTER, timeout=180)
        apply_template(ROOT / "cluster/git.yaml", image=tag)
        kube("-n", "keel-system", "rollout", "status", "deployment/keel-git", "--timeout=180s", timeout=200)
        REPO.mkdir(parents=True, exist_ok=True)
        remote = f"ext::kubectl --context {CONTEXT} -n keel-system exec -i deployment/keel-git -- %S /repos/platform.git"
        if not (REPO / ".git").exists():
            git("init", "-b", "main")
            git("remote", "add", "origin", remote)
        require(git("remote", "get-url", "origin") == remote, "Unexpected Git remote; refusing to push.")
        require(not git("status", "--porcelain"), "GitOps worktree is dirty; preserve or commit your changes first.")
    with stage("build and register " + name):
        current = read_service_registry()
        tag = build_service_image(service_dir, name, "v1")
        target = REPO / name
        git("rm", "-r", "--ignore-unmatch", "--", name)
        shutil.copytree(service_dir / "deploy", target)
        set_deployment_image(target / "deployment.yaml", tag)
        revision = commit("Deploy " + name)
        git("push", "origin", "main")
        current["services"][name] = {"source": str(service_dir.resolve())}
        save(WORK / "state.json", current)
    with stage("Argo CD reconciliation"):
        manifest = ROOT / "work/vendor/argocd.yaml"
        locks = json.loads((ROOT / "dependencies.lock.json").read_text())
        require(hashlib.sha256(manifest.read_bytes()).hexdigest() == locks["argocd_sha256"],
                "Argo CD manifest checksum mismatch.")
        kube("apply", "--server-side", "-n", "argocd", "-f", str(manifest), timeout=180)
        kube("-n", "argocd", "rollout", "status", "deployment/argocd-repo-server", "--timeout=240s", timeout=260)
        apply_template(ROOT / "cluster/application.yaml", service_name=name)
        wait_sync(name, revision)
    with stage("metrics and network boundaries"):
        refresh_monitoring(sorted(current["services"]))
        kube("apply", "-f", str(ROOT / "cluster/network-policies.yaml"))
    verify_service(name)


def verify_service(name):
    with stage("verify " + name):
        wait_sync(name, git("rev-parse", "HEAD"))
        with port_forward("keel-apps", name, 8080) as base:
            for _ in range(10):
                code, _ = request(base)
                require(code == 200, f"Expected HTTP 200, received {code}")
            code, metrics = request(base, "/metrics")
            require(code == 200 and 'status="200"' in metrics, "Success metrics missing.")
            require("http_request_duration_seconds_bucket" in metrics, "Latency histogram missing.")
        with port_forward("keel-system", "prometheus", 9090) as base:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                values = query_prometheus(base, f'up{{job="{name}"}}')
                if values and values[0]["value"][1] == "1":
                    break
                time.sleep(2)
            else:
                raise RuntimeError("Service was not scraped by Prometheus.")


def drive_alert(name, should_fire, expected_status):
    with port_forward("keel-apps", name, 8080) as app, port_forward("keel-system", "prometheus", 9090) as prom:
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            for _ in range(5):
                code, _ = request(app)
                require(code == expected_status, f"Expected HTTP {expected_status}, received {code}")
            require(request(app, "/healthz")[0] == 200, "Process health unexpectedly failed.")
            values = query_prometheus(prom, f'ALERTS{{alertname="KeelHighErrorRate",job="{name}",alertstate="firing"}}')
            if bool(values) == should_fire:
                return
            time.sleep(2)
        raise RuntimeError(f"Availability alert did not reach firing={should_fire}")


def set_deployment_image(path, image):
    manifest = yaml.safe_load(path.read_text())
    manifest["spec"]["template"]["spec"]["containers"][0]["image"] = image
    path.write_text(yaml.safe_dump(manifest, sort_keys=False))


def exercise_recovery(name):
    verify_service(name)
    require(not git("status", "--porcelain"), "GitOps worktree must be clean.")
    source = Path(read_service_registry()["services"][name]["source"])
    baseline = git("rev-parse", "HEAD")
    failure = None
    original_error = None
    with stage("build distinct failure image"):
        tag = build_service_image(source, name, "v2-failure", broken=True)
    try:
        with stage("promote failure image"):
            target = REPO / name / "deployment.yaml"
            set_deployment_image(target, tag)
            failure = commit("Exercise " + name + " failure image")
            REPORT.data.update(baseline=baseline, failure=failure)
            git("push", "origin", "main")
            wait_sync(name, failure)
        with stage("observe firing availability alert"):
            drive_alert(name, True, 500)
            REPORT.data["failure_observed"] = True
    except BaseException as error:
        original_error = error
        raise
    finally:
        if failure:
            try:
                with stage("recover baseline through Git"):
                    git("-c", "user.name=Keel Local", "-c", "user.email=keel@localhost",
                        "revert", "--no-edit", failure)
                    git("push", "origin", "main")
                    recovery = git("rev-parse", "HEAD")
                    REPORT.data["recovery"] = recovery
                    wait_sync(name, recovery)
                    verify_service(name)
                with stage("observe resolved availability alert"):
                    drive_alert(name, False, 200)
                    REPORT.data["recovered"] = True
            except BaseException as recovery_error:
                REPORT.data.update(recovered=False, recovery_error=str(recovery_error))
                if original_error:
                    REPORT.data["original_error"] = str(original_error)
                raise


def diagnostics():
    collected = {}
    for key, args in {"pods": ["get", "pods", "-A", "-o", "wide"],
                      "events": ["get", "events", "-A", "--sort-by=.lastTimestamp"],
                      "applications": ["get", "applications", "-n", "argocd", "-o", "yaml"]}.items():
        try:
            collected[key] = kube(*args, timeout=15)
        except Exception as error:
            collected[key] = str(error)
    save(REPORT.path.with_suffix(".diagnostics.json"), collected)


def main():
    global REPORT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["up", "verify", "exercise", "status", "down"])
    parser.add_argument("--service", default="payments", type=validate_identifier)
    parser.add_argument("--service-dir", type=Path)
    parser.add_argument("--confirm-cluster", help="Required exact cluster name for down.")
    args = parser.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    with (WORK / "operation.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SystemExit("Another Keel operation is running.") from error
        REPORT = Report(args.action)
        try:
            if args.action == "up":
                registered = read_service_registry()["services"].get(args.service)
                directory = args.service_dir or (Path(registered["source"]) if registered
                                                 else ROOT / "work/services" / args.service)
                if args.service_dir is None and not registered and not directory.exists():
                    generate(args.service, "platform-team", directory)
                bootstrap_platform(directory)
            elif args.action == "verify":
                verify_service(args.service)
            elif args.action == "exercise":
                exercise_recovery(args.service)
            elif args.action == "status":
                print(kube("get", "pods", "-A"))
            elif args.action == "down":
                require(args.confirm_cluster == CLUSTER, "Use --confirm-cluster keel-local.")
                verify_cluster_ownership()
                run("kind", "delete", "cluster", "--name", CLUSTER, timeout=120)
                (WORK / "managed.json").unlink()
                print("Removed the dedicated cluster. Sources, Git history and reports are retained.")
        except BaseException as error:
            REPORT.finish(error)
            diagnostics()
            print(f"FAILED: {error}\nReport: {REPORT.path}", flush=True)
            raise SystemExit(1) from error
        else:
            REPORT.finish()
            print(f"PASS: {args.action}\nReport: {REPORT.path}", flush=True)


if __name__ == "__main__":
    main()
