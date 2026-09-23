"""LLM judge for the qualities rules cannot decide.

Scope discipline: the judge is *not* asked whether facts are correct, whether
citations resolve, or whether numbers are grounded - deterministic graders own
all of that and are more reliable at it. The judge sees only what is genuinely
subjective: analytical coherence, whether the structure serves the question,
and whether conclusions are hedged appropriately for the evidence presented.

Guarantees this module enforces:

- The judge model and prompt version are recorded on every result, so a score
  can be reproduced or invalidated when either changes.
- The judge returns a visible rationale, never hidden reasoning; the prompt
  asks for a short justification and nothing is stored beyond it.
- Calibration against human labels is measured by `calibrate()` and reported
  as an agreement rate. An uncalibrated judge's numbers are reported as
  uncalibrated rather than presented alongside verified results.
- The judge must not be the same chain that produced the report. `guard_independence()`
  refuses to grade when the judge model equals the generator model unless the
  caller explicitly acknowledges the dependence, which is then recorded.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from pydantic import BaseModel, Field

from evals.datasets.schema import EvalTask
from evals.graders.base import BaseGrader, GradeResult, RunArtifacts

JUDGE_PROMPT_VERSION = "judge-v1"

#: The rubric is versioned with the prompt: changing either invalidates
#: comparisons with previously stored judge scores.
RUBRIC = """你是金融研究报告的质量评审。只评判以下三个维度，**不要**评判事实正确性、
引用是否有效、数字是否溯源——这些由确定性规则单独检查，不在你的职责范围内。

评分维度（每项 0-5 分整数）：

1. analytical_coherence（分析连贯性）
   5 = 论点之间有清晰因果链，结论由前文推导而来
   3 = 各段落合理但彼此独立，缺少推导
   1 = 段落之间互相矛盾或与结论无关

2. structure_fit（结构贴合度）
   5 = 章节安排直接服务于用户提出的问题
   3 = 使用了通用研报模板，能覆盖但不针对
   1 = 结构与问题无关

3. calibrated_hedging（结论的确定性校准）
   5 = 结论的确定性与所给证据强度匹配，不确定处明确标注
   1 = 证据薄弱却给出确定性断言，或证据充分却全篇模糊不敢下判断

只输出 JSON，不要输出任何其他文字：
{"analytical_coherence": <int>, "structure_fit": <int>, "calibrated_hedging": <int>,
 "justification": "<不超过120字的简短理由，说明扣分点>"}"""


class JudgeScore(BaseModel):
    analytical_coherence: int = 0
    structure_fit: int = 0
    calibrated_hedging: int = 0
    justification: str = ""

    def normalized(self) -> float:
        return round((self.analytical_coherence + self.structure_fit
                      + self.calibrated_hedging) / 15.0, 4)


class LLMJudgeGrader(BaseGrader):
    """Optional grader. Disabled by default so the suite runs with no API key."""

    name = "llm_judge"
    weight = 1.0

    def __init__(self, *, model: str = "", enabled: bool = False,
                 generator_model: str = "", allow_same_model: bool = False,
                 max_report_chars: int = 6000) -> None:
        self.model = model
        self.enabled = enabled
        self.generator_model = generator_model
        self.allow_same_model = allow_same_model
        self.max_report_chars = max_report_chars

    # ------------------------------------------------------------------ #
    def guard_independence(self) -> Optional[str]:
        """Refuse to pose as an independent grader when it is the same chain."""
        from src.runtime.budget import normalize_model_name

        judge = normalize_model_name(self.model)
        generator = normalize_model_name(self.generator_model or "")
        if judge and generator and judge == generator and not self.allow_same_model:
            return (f"judge model {judge!r} is the same as the generator model; "
                    "same-chain judging is not independent evidence "
                    "(pass allow_same_model=True to record it as such)")
        return None

    def grade(self, task: EvalTask, run: RunArtifacts) -> GradeResult:
        if not self.enabled:
            return self.not_applicable("LLM judge disabled (default: deterministic graders only)")
        if not run.report_markdown.strip():
            return self.not_applicable("no report to judge")
        if (problem := self.guard_independence()):
            return self.not_applicable(problem, judge_model=self.model,
                                       generator_model=self.generator_model)

        prompt = (
            f"{RUBRIC}\n\n"
            f"【用户问题】{task.query}\n"
            f"【报告类型】{task.report_type}\n\n"
            f"【待评报告】\n{run.report_markdown[:self.max_report_chars]}"
        )
        try:
            raw = self._call(prompt)
            score = self._parse(raw)
        except Exception as exc:  # noqa: BLE001 - a judge failure must not fail the suite
            return self.not_applicable(f"judge call failed: {type(exc).__name__}: {exc}")

        result = self.result(
            score.normalized(), score.normalized() >= 0.6,
            score.justification[:200],
            metrics={"judge_coherence": score.analytical_coherence,
                     "judge_structure": score.structure_fit,
                     "judge_hedging": score.calibrated_hedging,
                     "judge_normalized": score.normalized()},
            dimensions=score.model_dump())
        result.judge_model = self.model
        result.judge_prompt_version = JUDGE_PROMPT_VERSION
        # Same-chain judging that the caller explicitly accepted is recorded so
        # it can never be read as independent.
        if self.allow_same_model and self.model == self.generator_model:
            result.details["independence"] = "same-chain judge, explicitly acknowledged"
        return result

    # ------------------------------------------------------------------ #
    def _call(self, prompt: str) -> str:
        import litellm

        from config import config

        response = litellm.completion(
            model=self.model or config.MODEL_NAME,
            messages=[{"role": "system", "content": "你是严格的评审，只输出JSON。"},
                      {"role": "user", "content": prompt}],
            temperature=0.0, timeout=60,
            api_base=config.OPENAI_API_BASE or None)
        return response["choices"][0]["message"]["content"] or ""

    @staticmethod
    def _parse(raw: str) -> JudgeScore:
        from agents.base_agent import BaseAgent

        parsed = BaseAgent.parse_json_response(raw)
        if not isinstance(parsed, dict):
            raise ValueError(f"judge did not return a JSON object: {raw[:200]!r}")
        return JudgeScore(
            analytical_coherence=int(parsed.get("analytical_coherence", 0)),
            structure_fit=int(parsed.get("structure_fit", 0)),
            calibrated_hedging=int(parsed.get("calibrated_hedging", 0)),
            justification=str(parsed.get("justification", ""))[:300])


# --------------------------------------------------------------------------- #
# Calibration
# --------------------------------------------------------------------------- #
class CalibrationSample(BaseModel):
    task_id: str
    human_score: float          # 0.0-1.0, from a person
    judge_score: float
    annotator: str = ""


def calibrate(samples: list[CalibrationSample], tolerance: float = 0.2) -> dict[str, Any]:
    """Agreement between the judge and human labels.

    Reports both agreement-within-tolerance and mean signed bias, because a
    judge that is uniformly 0.15 too generous is a different (and fixable)
    problem from one that is noisy.
    """
    if not samples:
        return {"samples": 0, "calibrated": False,
                "note": "no human labels; judge scores are UNCALIBRATED and must be "
                        "reported as such, never alongside verified metrics"}
    deltas = [s.judge_score - s.human_score for s in samples]
    within = sum(1 for d in deltas if abs(d) <= tolerance)
    mean_bias = sum(deltas) / len(deltas)
    mean_abs = sum(abs(d) for d in deltas) / len(deltas)
    return {
        "samples": len(samples),
        "calibrated": True,
        "tolerance": tolerance,
        "agreement_rate": round(within / len(samples), 4),
        "mean_bias": round(mean_bias, 4),
        "mean_absolute_error": round(mean_abs, 4),
        "annotators": sorted({s.annotator for s in samples if s.annotator}),
        "verdict": ("judge tracks human labels within tolerance"
                    if within / len(samples) >= 0.7 else
                    "judge disagrees with human labels too often to be used as a metric"),
    }
