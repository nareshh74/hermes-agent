"""Every workflow reachable from ci.yaml runs on a free standard GitHub-hosted runner.

Larger runners (``*-32-core``, ``*-96-core``, ``*-arm-core``) are always billed, even in
public repositories (docs.github.com/en/billing/concepts/product-billing/github-actions).
The fork's required CI graph (ci.yaml plus every reusable workflow it reaches through
``uses: ./.github/workflows/...``) must not name one. Release/deploy graphs are out of scope.
"""
import re
from pathlib import Path

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"

# Standard labels from the GitHub-hosted runners reference (public-repo table).
STANDARD = {
    "ubuntu-latest", "ubuntu-24.04", "ubuntu-22.04", "ubuntu-26.04", "ubuntu-slim",
    "ubuntu-24.04-arm", "ubuntu-22.04-arm", "ubuntu-26.04-arm",
    "windows-latest", "windows-2025", "windows-2022", "windows-11-arm",
    "macos-latest", "macos-14", "macos-15", "macos-26", "macos-15-intel", "macos-26-intel",
}
LABEL = re.compile(r"'([^']+)'")


def _load(name):
    return YAML(typ="safe").load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _reachable(start="ci.yaml"):
    seen, todo = [], [start]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.append(name)
        for job in (_load(name).get("jobs") or {}).values():
            uses = job.get("uses", "")
            if uses.startswith("./.github/workflows/"):
                todo.append(uses.rsplit("/", 1)[1])
    return seen


def _labels(job):
    """Every runner label a job can resolve to: literals plus matrix `runner` values."""
    runs_on = job["runs-on"]
    found = set(LABEL.findall(runs_on)) if "${{" in str(runs_on) else {runs_on}
    if "matrix." in str(runs_on):
        include = job.get("strategy", {}).get("matrix", {}).get("include", [])
        found |= {cell["runner"] for cell in include if "runner" in cell}
    return found


def test_required_ci_graph_uses_only_free_standard_runners():
    graph = _reachable()
    assert {"tests.yml", "tests-os.yml", "js-tests.yml"} <= set(graph)  # the walk really descends
    bad = {}
    for name in graph:
        for job_id, job in (_load(name).get("jobs") or {}).items():
            if "runs-on" in job:
                extra = _labels(job) - STANDARD
                if extra:
                    bad[f"{name}:{job_id}"] = sorted(extra)
    assert not bad, f"non-free runner labels in fork CI graph: {bad}"


def test_windows_arm64_lane_survives():
    jobs = _load("tests-os.yml")["jobs"]["os-tests"]
    runners = {cell["runner"] for cell in jobs["strategy"]["matrix"]["include"]}
    assert "windows-11-arm" in runners and "windows-latest" in runners


def test_policy_catches_a_paid_label():
    job = {"runs-on": "${{ matrix.runner }}", "strategy": {"matrix": {"include": [
        {"runner": "windows-latest-32-arm-core"}]}}}
    assert _labels(job) - STANDARD
