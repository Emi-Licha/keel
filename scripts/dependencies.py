"""Download verified upstream manifests/charts into the ignored workspace cache."""
import hashlib
import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    locks = json.loads((ROOT / "dependencies.lock.json").read_text())
    directory = ROOT / "work/vendor"
    directory.mkdir(parents=True, exist_ok=True)
    for prefix, filename in [("argocd", "argocd.yaml"), ("cilium_chart", "cilium-1.20.2.tgz")]:
        target = directory / filename
        data = target.read_bytes() if target.exists() else None
        if data is None or hashlib.sha256(data).hexdigest() != locks[prefix + "_sha256"]:
            with urllib.request.urlopen(locks[prefix + "_url"], timeout=90) as response:
                data = response.read()
            if hashlib.sha256(data).hexdigest() != locks[prefix + "_sha256"]:
                raise RuntimeError(f"Checksum mismatch for {filename}")
            temp = target.with_suffix(".download")
            temp.write_bytes(data)
            temp.replace(target)
        print(f"Verified {filename}")


if __name__ == "__main__":
    main()
