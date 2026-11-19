"""Record the resolved dependency closure without exporting pip credentials."""
import json
from importlib.metadata import distribution
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


ROOT = Path(__file__).resolve().parent.parent


def main():
    pending = [Requirement(line) for line in (ROOT / "requirements.txt").read_text().splitlines()
               if line.strip() and not line.startswith("#")]
    reports = {}
    report_path = ROOT / "docs" / "install-result.json"
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        for item in report["install"]:
            reports[canonicalize_name(item["metadata"]["name"])] = item["download_info"]
    components = {}
    visited = set()
    while pending:
        requirement = pending.pop()
        name = canonicalize_name(requirement.name)
        key = (name, tuple(sorted(requirement.extras)))
        if key in visited:
            continue
        visited.add(key)
        package = distribution(name)
        metadata = package.metadata
        components[name] = {
            "name": metadata["Name"], "version": package.version,
            "license": metadata.get("License-Expression") or metadata.get("License"),
            "license_classifiers": [value for value in metadata.get_all("Classifier", [])
                                    if value.startswith("License ::")],
            "project_urls": metadata.get_all("Project-URL", []),
            "package_url": f"pkg:pypi/{name}@{package.version}",
            "new_install_artifact": reports.get(name),
        }
        for text in package.requires or []:
            child = Requirement(text)
            if child.marker is None or any(child.marker.evaluate({"extra": extra})
                                           for extra in {"", *requirement.extras}):
                pending.append(child)
    ordered = sorted(components.values(), key=lambda component: component["name"].lower())
    docs = ROOT / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "dependencies.json").write_text(json.dumps({
        "format": "project dependency inventory", "recorded_on": "2026-11-19",
        "scope": "requirements.txt dependency closure in confirmed EduRAG Python 3.10",
        "components": ordered,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (ROOT / "requirements.lock").write_text(
        "# Resolved for Windows / Python 3.10, including local embedding dependencies.\n"
        + "\n".join(component["name"] + "==" + component["version"] for component in ordered)
        + "\n", encoding="utf-8")
    print(f"Recorded {len(ordered)} dependency components")


if __name__ == "__main__":
    main()
