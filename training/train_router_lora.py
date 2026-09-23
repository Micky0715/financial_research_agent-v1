"""LoRA SFT for the tool-router head. Gated, and does not run by default.

    python training/train_router_lora.py --config training/qwen3_1_7b_router_lora.yaml --check
    python training/train_router_lora.py --config training/qwen3_1_7b_router_lora.yaml --train

`--check` runs every precondition and prints what is still missing. It needs no
GPU and no extra packages, so it is the useful command until a machine with a
GPU is available.

`--train` refuses unless all preconditions hold:

  1. the exported datasets exist and pass the integrity checks
     (no reasoning fields, no group leakage between train and eval);
  2. `transformers`, `peft`, `datasets` and `torch` are importable;
  3. CUDA is available - this will not train on CPU, because a silent 40-hour
     CPU run is worse than a refusal;
  4. `--force-unreviewed` is passed if the dataset has no human-verified
     records, so training on purely grader-derived labels is a deliberate,
     recorded choice rather than an accident.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN_KEYS = {"reasoning", "reasoning_content", "thinking", "thought", "thoughts",
                  "chain_of_thought", "cot", "scratchpad"}


def load_config(path: Path) -> dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8")
    try:
        import yaml

        return yaml.safe_load(text)
    except ImportError:
        raise SystemExit("PyYAML is required to read the training config: pip install pyyaml")


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _scan_reasoning(obj: Any, path: str = "$") -> list[str]:
    hits: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                hits.append(f"{path}.{key}")
            hits.extend(_scan_reasoning(value, f"{path}.{key}"))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            hits.extend(_scan_reasoning(item, f"{path}[{i}]"))
    return hits


class Precondition:
    def __init__(self, name: str, ok: bool, detail: str, blocking: bool = True) -> None:
        self.name, self.ok, self.detail, self.blocking = name, ok, detail, blocking

    def render(self) -> str:
        mark = "PASS" if self.ok else ("BLOCK" if self.blocking else "WARN")
        return f"[{mark}] {self.name}: {self.detail}"


def check_preconditions(config: dict[str, Any], *, force_unreviewed: bool = False) -> list[Precondition]:
    checks: list[Precondition] = []
    data_root = REPO_ROOT / config["data"]["root"]

    # 1. datasets present
    missing, present, total_records = [], [], 0
    for spec in config["data"]["datasets"]:
        path = data_root / spec["file"]
        rows = _load_jsonl(path)
        if not rows:
            missing.append(spec["file"])
        else:
            present.append(spec["file"])
            total_records += len(rows)
    checks.append(Precondition(
        "datasets_present", not missing and bool(present),
        f"{len(present)} 个数据集共 {total_records} 条记录"
        + (f"；缺失/为空：{missing}" if missing else "")
        + ("；先运行 scripts/export_agent_trajectories.py" if missing else "")))

    # 2. no reasoning fields
    reasoning_hits: list[str] = []
    for spec in config["data"]["datasets"]:
        for row in _load_jsonl(data_root / spec["file"])[:2000]:
            reasoning_hits.extend(_scan_reasoning(row))
    checks.append(Precondition(
        "no_reasoning_fields", not reasoning_hits,
        "未发现隐藏推理字段" if not reasoning_hits else f"发现 {len(reasoning_hits)} 处：{reasoning_hits[:3]}"))

    # 3. no group leakage
    leaks: dict[str, list[str]] = {}
    for spec in config["data"]["datasets"]:
        groups: dict[str, set[str]] = defaultdict(set)
        for row in _load_jsonl(data_root / spec["file"]):
            groups[row.get("split", "?")].add(row.get("group", ""))
        overlap = sorted(groups.get("train", set()) & groups.get("eval", set()))
        if overlap:
            leaks[spec["file"]] = overlap[:5]
    checks.append(Precondition(
        "no_group_leakage", not leaks,
        "train/eval 组无重叠" if not leaks else f"存在泄漏：{leaks}"))

    # 4. both splits non-empty
    split_counts: dict[str, int] = defaultdict(int)
    for spec in config["data"]["datasets"]:
        for row in _load_jsonl(data_root / spec["file"]):
            split_counts[row.get("split", "?")] += 1
    checks.append(Precondition(
        "both_splits_present", split_counts.get("train", 0) > 0 and split_counts.get("eval", 0) > 0,
        f"train={split_counts.get('train', 0)} eval={split_counts.get('eval', 0)}"))

    # 5. human review status
    verified = sum(1 for spec in config["data"]["datasets"]
                   for row in _load_jsonl(data_root / spec["file"]) if row.get("verified"))
    checks.append(Precondition(
        "human_reviewed_labels", verified > 0 or force_unreviewed,
        f"人工复核记录 {verified} 条"
        + ("" if verified else
           "；全部标签由确定性 grader 推导。训练出的路由器只会复现当前系统的行为，"
           "包括它的错误。要在明知这一点的情况下继续，请显式传 --force-unreviewed"),
        blocking=verified == 0 and not force_unreviewed))

    # 6. libraries
    missing_libs = []
    for module in ("torch", "transformers", "peft", "datasets"):
        try:
            __import__(module)
        except ImportError:
            missing_libs.append(module)
    checks.append(Precondition(
        "libraries", not missing_libs,
        "训练依赖齐全" if not missing_libs else
        f"缺少 {missing_libs}；pip install {' '.join(missing_libs)}"))

    # 7. GPU
    cuda = False
    detail = "torch 未安装，无法检测 GPU"
    if "torch" not in missing_libs:
        import torch

        cuda = torch.cuda.is_available()
        detail = (f"检测到 {torch.cuda.device_count()} 张 GPU："
                  f"{torch.cuda.get_device_name(0)}" if cuda else
                  "未检测到 CUDA 设备；本脚本拒绝在 CPU 上训练"
                  "（静默跑 40 小时比直接拒绝更糟）")
    checks.append(Precondition("gpu_available", cuda, detail))

    return checks


def build_dataset(config: dict[str, Any], split: str) -> list[dict[str, str]]:
    """Flatten every task into instruction/input/output text triples."""
    data_root = REPO_ROOT / config["data"]["root"]
    examples: list[dict[str, str]] = []
    for spec in config["data"]["datasets"]:
        weight = float(spec.get("weight", 1.0))
        repeats = max(1, int(round(weight)))
        for row in _load_jsonl(data_root / spec["file"]):
            if row.get("split") != split:
                continue
            example = {
                "instruction": spec["instruction"],
                "input": json.dumps(row["input"], ensure_ascii=False),
                "output": json.dumps(row["output"], ensure_ascii=False),
                "task": spec["name"],
            }
            examples.extend([example] * (repeats if split == "train" else 1))
    return examples


def train(config: dict[str, Any]) -> int:  # pragma: no cover - requires a GPU
    import torch
    from datasets import Dataset
    from peft import LoraConfig, get_peft_model
    from transformers import (
        AutoModelForCausalLM,
        AutoTokenizer,
        DataCollatorForLanguageModeling,
        Trainer,
        TrainingArguments,
    )

    model_config, lora_config, train_config = config["model"], config["lora"], config["training"]
    tokenizer = AutoTokenizer.from_pretrained(model_config["name"],
                                              trust_remote_code=model_config.get("trust_remote_code", True))
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    def to_text(example: dict[str, str]) -> str:
        return (f"<|im_start|>system\n{example['instruction']}<|im_end|>\n"
                f"<|im_start|>user\n{example['input']}<|im_end|>\n"
                f"<|im_start|>assistant\n{example['output']}<|im_end|>")

    def tokenize(batch: dict[str, list[str]]) -> dict[str, Any]:
        texts = [to_text({"instruction": i, "input": x, "output": o})
                 for i, x, o in zip(batch["instruction"], batch["input"], batch["output"])]
        return tokenizer(texts, truncation=True, max_length=model_config["max_seq_length"])

    train_ds = Dataset.from_list(build_dataset(config, "train")).map(
        tokenize, batched=True, remove_columns=["instruction", "input", "output", "task"])
    eval_ds = Dataset.from_list(build_dataset(config, "eval")).map(
        tokenize, batched=True, remove_columns=["instruction", "input", "output", "task"])

    model = AutoModelForCausalLM.from_pretrained(
        model_config["name"], torch_dtype=getattr(torch, model_config["torch_dtype"]),
        trust_remote_code=model_config.get("trust_remote_code", True))
    model = get_peft_model(model, LoraConfig(
        r=lora_config["r"], lora_alpha=lora_config["alpha"],
        lora_dropout=lora_config["dropout"], bias=lora_config["bias"],
        task_type=lora_config["task_type"], target_modules=lora_config["target_modules"]))
    model.print_trainable_parameters()

    trainer = Trainer(
        model=model,
        args=TrainingArguments(**{k: v for k, v in train_config.items()}),
        train_dataset=train_ds, eval_dataset=eval_ds,
        data_collator=DataCollatorForLanguageModeling(tokenizer, mlm=False))
    trainer.train()
    trainer.save_model(train_config["output_dir"])
    tokenizer.save_pretrained(train_config["output_dir"])
    print(f"adapter saved to {train_config['output_dir']}")
    print("Next: python training/router_comparison.py --adapter "
          f"{train_config['output_dir']} --live")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="LoRA SFT for the tool-router head")
    parser.add_argument("--config", type=Path,
                        default=REPO_ROOT / "training" / "qwen3_1_7b_router_lora.yaml")
    parser.add_argument("--check", action="store_true", help="check preconditions and exit")
    parser.add_argument("--train", action="store_true", help="actually train (needs a GPU)")
    parser.add_argument("--force-unreviewed", action="store_true",
                        help="proceed even though no label has been reviewed by a human")
    args = parser.parse_args()

    config = load_config(args.config)
    checks = check_preconditions(config, force_unreviewed=args.force_unreviewed)
    for check in checks:
        print(check.render())

    blocking = [c for c in checks if c.blocking and not c.ok]
    train_count = len(build_dataset(config, "train"))
    eval_count = len(build_dataset(config, "eval"))
    print(f"\nweighted examples: train={train_count} eval={eval_count}")

    if args.check or not args.train:
        print("\n（仅检查，未训练。加 --train 且全部前置条件通过后才会真正开始训练。）"
              if not blocking else
              f"\n{len(blocking)} 项前置条件未满足，当前无法训练。")
        return 0 if not blocking else 1

    if blocking:
        print(f"\n拒绝训练：{[c.name for c in blocking]}")
        return 1
    return train(config)


if __name__ == "__main__":
    sys.exit(main())
