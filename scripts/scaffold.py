"""Create a service from the supported template without overwriting existing work."""
import argparse
import json
import re
import shutil
from pathlib import Path
from string import Template

ROOT = Path(__file__).resolve().parents[1]


def validate_identifier(value):
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,38}[a-z0-9]", value):
        raise ValueError("Use 3-40 lowercase letters/digits/hyphens, starting with a letter and ending with a letter/digit.")
    return value


def generate(name, owner, output):
    validate_identifier(name)
    validate_identifier(owner)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    shutil.copytree(ROOT / "service", output / "service", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (output / "keel.json").write_text(json.dumps({"name": name, "owner": owner, "schema_version": 1}, indent=2) + "\n")
    shutil.copytree(ROOT / "templates/service", output / "deploy")
    for path in (output / "deploy").glob("*.yaml"):
        path.write_text(Template(path.read_text()).substitute(service_name=name, owner=owner, image=f"keel/{name}:v1"))
    (output / "README.md").write_text(f"# {name}\n\nOwner: {owner}.\n\n"
        "Deploy from the Keel repository with:\n\n"
        f"`python scripts/demo.py up --service-dir {output}`\n")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", type=validate_identifier)
    parser.add_argument("--owner", required=True, type=validate_identifier)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(generate(args.name, args.owner, args.output))
