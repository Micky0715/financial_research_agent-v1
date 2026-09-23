"""Makefile-equivalent task runner, for machines without `make` (i.e. Windows).

    python scripts/dev.py                 # list targets
    python scripts/dev.py eval-smoke
    python scripts/dev.py test

Targets are kept in lockstep with the Makefile; `python scripts/dev.py --verify`
checks that neither has drifted from the other.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

TARGETS: dict[str, list[list[str]]] = {
    "test": [[PY, "-m", "pytest", "tests", "-q"]],
    "test-legacy": [[PY, "-m", "pytest", "tests", "-q", "--ignore=tests/harness"]],
    "test-harness": [[PY, "-m", "pytest", "tests/harness", "-q"]],

    "eval-smoke": [[PY, "-m", "evals.runners.run_eval", "--smoke"]],
    "eval": [[PY, "-m", "evals.runners.run_eval", "--split", "dev", "--arm", "harness",
              "--trials", "3"]],
    "eval-test": [[PY, "-m", "evals.runners.run_eval", "--split", "test", "--arm", "harness",
                   "--trials", "3"]],
    "eval-regression": [[PY, "-m", "evals.runners.run_eval", "--split", "regression",
                         "--arm", "harness", "--trials", "1",
                         "--results", "evals/results/regression_harness.jsonl"]],
    "eval-report": [[PY, "-m", "evals.reports.build_report",
                     "--results", "evals/results/dev_harness.jsonl",
                     "--compare", "evals/results/dev_baseline_v4.jsonl"]],
    "eval-ablation": [
        [PY, "-m", "evals.runners.run_eval", "--split", "dev", "--arm", "baseline_v4",
         "--trials", "1", "--results", "evals/results/abl_baseline_v4.jsonl"],
        [PY, "-m", "evals.runners.run_eval", "--split", "dev", "--arm", "no_fallback",
         "--trials", "1", "--results", "evals/results/abl_no_fallback.jsonl"],
        [PY, "-m", "evals.runners.run_eval", "--split", "dev", "--arm", "no_compression",
         "--trials", "1", "--results", "evals/results/abl_no_compression.jsonl"],
        [PY, "-m", "evals.runners.run_eval", "--split", "dev", "--arm", "max_rounds_0",
         "--trials", "1", "--results", "evals/results/abl_max_rounds_0.jsonl"],
        [PY, "-m", "evals.runners.run_eval", "--split", "dev", "--arm", "low_concurrency",
         "--trials", "1", "--results", "evals/results/abl_low_concurrency.jsonl"],
        [PY, "scripts/build_ablation_matrix.py"],
    ],
    "memory-ab": [[PY, "-m", "evals.runners.memory_ab", "--sessions", "4", "--subjects", "6"]],

    "datasets": [[PY, "-m", "evals.datasets.build_datasets", "--write"]],
    "review-queue": [[PY, "-m", "evals.datasets.review_queue", "export", "--limit", "40"],
                     [PY, "-m", "evals.datasets.review_queue", "status"]],

    "mine": [[PY, "-m", "src.optimization.experiment_runner", "mine",
              "--results", "evals/results/dev_harness.jsonl"]],
    "propose": [[PY, "-m", "src.optimization.experiment_runner", "propose",
                 "--results", "evals/results/dev_harness.jsonl"]],
    "gate-status": [[PY, "-m", "src.optimization.experiment_runner", "status"]],

    "trajectories": [[PY, "scripts/export_agent_trajectories.py",
                      "--results", "evals/results/dev_harness.jsonl"]],
    "router-compare": [[PY, "training/router_comparison.py",
                        "--data", "exports/agent_sft", "--split", "eval"]],
    "train-check": [[PY, "training/train_router_lora.py",
                     "--config", "training/qwen3_1_7b_router_lora.yaml", "--check"]],

    "demo": [[PY, "scripts/demo_harness_run.py"]],
    "api": [[PY, "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]],
    "docker-build": [["docker", "build", "-t", "financial-research-agent", "."]],
}


def makefile_targets() -> set[str]:
    makefile = REPO_ROOT / "Makefile"
    if not makefile.exists():
        return set()
    names = set()
    for line in makefile.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([a-zA-Z0-9_-]+):", line)
        if match and match.group(1) not in ("help", "clean-eval"):
            names.add(match.group(1))
    return names


def verify() -> int:
    """Fail if the Makefile and this script have drifted apart."""
    make_names = makefile_targets()
    only_make = sorted(make_names - set(TARGETS))
    only_here = sorted(set(TARGETS) - make_names)
    if only_make or only_here:
        print(f"drift detected: only in Makefile={only_make}, only in dev.py={only_here}")
        return 1
    print(f"in sync: {len(TARGETS)} targets")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Task runner (Makefile equivalent)")
    parser.add_argument("target", nargs="?", help="target to run")
    parser.add_argument("--verify", action="store_true", help="check parity with the Makefile")
    args = parser.parse_args()

    if args.verify:
        return verify()
    if not args.target:
        print("targets:")
        for name in sorted(TARGETS):
            print(f"  {name}")
        return 0
    if args.target not in TARGETS:
        print(f"unknown target {args.target!r}; available: {sorted(TARGETS)}")
        return 1

    for command in TARGETS[args.target]:
        print(f"$ {' '.join(command)}", flush=True)
        code = subprocess.call(command, cwd=REPO_ROOT)
        if code != 0:
            return code
    return 0


if __name__ == "__main__":
    sys.exit(main())
