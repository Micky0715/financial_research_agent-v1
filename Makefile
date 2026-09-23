# Financial Research Agent - one-command entry points.
#
# Windows without make: every target below is a single command; run the
# PYTHON=... line directly, or use `python scripts/dev.py <target>` which
# mirrors these targets exactly.

PYTHON ?= python

.PHONY: help test test-legacy test-harness eval-smoke eval eval-test eval-regression \
        eval-report eval-ablation memory-ab datasets review-queue demo \
        mine propose gate-status trajectories router-compare train-check \
        api docker-build clean-eval

help:
	@echo "make test             - full offline test suite (legacy + harness)"
	@echo "make eval-smoke       - 5-task offline smoke eval (~1 min, no network, no cost)"
	@echo "make eval             - dev split, 3 trials, offline fixtures"
	@echo "make eval-test        - held-out test split (run sparingly)"
	@echo "make eval-regression  - bad-case regression set"
	@echo "make eval-report      - build the Markdown/JSON eval report"
	@echo "make eval-ablation    - run every ablation arm and compare"
	@echo "make memory-ab        - multi-session memory A/B evaluation"
	@echo "make datasets         - rebuild eval datasets from repo assets"
	@echo "make review-queue     - export tasks needing human review"
	@echo "make mine             - classify failures from the latest dev results"
	@echo "make propose          - generate improvement candidates (proposals only)"
	@echo "make trajectories     - export agent trajectory training data + Data Card"
	@echo "make router-compare   - compare tool-router strategies"
	@echo "make train-check      - check LoRA training preconditions (no GPU needed)"
	@echo "make demo             - single offline end-to-end run with a full trace"
	@echo "make api              - start the FastAPI service"

# ---------------------------------------------------------------- tests
test:
	$(PYTHON) -m pytest tests -q

test-legacy:
	$(PYTHON) -m pytest tests -q --ignore=tests/harness

test-harness:
	$(PYTHON) -m pytest tests/harness -q

# ---------------------------------------------------------------- evaluation
eval-smoke:
	$(PYTHON) -m evals.runners.run_eval --smoke

eval:
	$(PYTHON) -m evals.runners.run_eval --split dev --arm harness --trials 3

eval-test:
	$(PYTHON) -m evals.runners.run_eval --split test --arm harness --trials 3

eval-regression:
	$(PYTHON) -m evals.runners.run_eval --split regression --arm harness --trials 1 \
		--results evals/results/regression_harness.jsonl

eval-report:
	$(PYTHON) -m evals.reports.build_report \
		--results evals/results/dev_harness.jsonl \
		--compare evals/results/dev_baseline_v4.jsonl

eval-ablation:
	$(PYTHON) -m evals.runners.run_eval --split dev --arm baseline_v4 --trials 1 \
		--results evals/results/abl_baseline_v4.jsonl
	$(PYTHON) -m evals.runners.run_eval --split dev --arm no_fallback --trials 1 \
		--results evals/results/abl_no_fallback.jsonl
	$(PYTHON) -m evals.runners.run_eval --split dev --arm no_compression --trials 1 \
		--results evals/results/abl_no_compression.jsonl
	$(PYTHON) -m evals.runners.run_eval --split dev --arm max_rounds_0 --trials 1 \
		--results evals/results/abl_max_rounds_0.jsonl
	$(PYTHON) -m evals.runners.run_eval --split dev --arm low_concurrency --trials 1 \
		--results evals/results/abl_low_concurrency.jsonl
	$(PYTHON) scripts/build_ablation_matrix.py

memory-ab:
	$(PYTHON) -m evals.runners.memory_ab --sessions 4 --subjects 6

# ---------------------------------------------------------------- datasets
datasets:
	$(PYTHON) -m evals.datasets.build_datasets --write

review-queue:
	$(PYTHON) -m evals.datasets.review_queue export --limit 40
	$(PYTHON) -m evals.datasets.review_queue status

# ---------------------------------------------------------------- optimization
mine:
	$(PYTHON) -m src.optimization.experiment_runner mine \
		--results evals/results/dev_harness.jsonl

propose:
	$(PYTHON) -m src.optimization.experiment_runner propose \
		--results evals/results/dev_harness.jsonl

gate-status:
	$(PYTHON) -m src.optimization.experiment_runner status

# ---------------------------------------------------------------- post-training prep
trajectories:
	$(PYTHON) scripts/export_agent_trajectories.py \
		--results evals/results/dev_harness.jsonl

router-compare:
	$(PYTHON) training/router_comparison.py --data exports/agent_sft --split eval

train-check:
	$(PYTHON) training/train_router_lora.py \
		--config training/qwen3_1_7b_router_lora.yaml --check

# ---------------------------------------------------------------- demo / service
demo:
	$(PYTHON) scripts/demo_harness_run.py

api:
	$(PYTHON) -m uvicorn api.main:app --host 0.0.0.0 --port 8000

docker-build:
	docker build -t financial-research-agent .

clean-eval:
	rm -rf evals/results/runs outputs/harness/traces
