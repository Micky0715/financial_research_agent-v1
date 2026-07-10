"""Local embedding-based semantic similarity scoring for search candidates.

v2 模块2：在 BrowserAgent 的候选排序环节（tools/source_filter.py）加一层基于
本地 embedding 模型的语义相似度打分，与已有的关键词规则打分做加权融合——
不替换规则打分（规则分保留在结果里，保证可解释性）。

模型：BAAI/bge-small-zh-v1.5（约 95MB，本地 CPU 推理，不依赖付费 API）。
国内网络从 ModelScope 下载（huggingface.co 直连通常失败，见 eval/bad_cases.md）。

失败策略：模型不可用（未下载/加载失败）时 `semantic_scores()` 返回 None，
调用方退回纯规则排序并打一次 warning——语义层是增强，不是硬依赖。
"""
import threading
from pathlib import Path
from typing import Optional

from config import config
from utils.logger import logger

_MODEL_NAME = "BAAI/bge-small-zh-v1.5"

_lock = threading.Lock()
_model = None
_model_failed = False


def _find_modelscope_cache() -> Optional[str]:
    """Locate the ModelScope snapshot dir for the embedding model, if downloaded.

    Checked before calling snapshot_download so a cached model loads without
    any network round-trip (and without modelscope installed at all, if the
    directory was copied over).
    """
    candidates = [
        Path.home() / ".cache" / "modelscope" / "models" / "BAAI--bge-small-zh-v1.5" / "snapshots" / "master",
        Path.home() / ".cache" / "modelscope" / "hub" / "models" / "BAAI" / "bge-small-zh-v1.5",
        Path.home() / ".cache" / "modelscope" / "hub" / "BAAI" / "bge-small-zh-v1.5",
    ]
    for path in candidates:
        if path.exists() and (path / "config.json").exists():
            return str(path)
    # Fall back to asking modelscope (downloads on first use; instant if cached)
    try:
        from modelscope import snapshot_download

        return snapshot_download(_MODEL_NAME)
    except Exception:  # noqa: BLE001 - caller degrades to name-based / no semantic layer
        return None


def _load_model():
    """Lazy-load the sentence-transformers model once per process. Never raises."""
    global _model, _model_failed
    if _model is not None or _model_failed:
        return _model
    with _lock:
        if _model is not None or _model_failed:
            return _model
        try:
            from sentence_transformers import SentenceTransformer

            local_path = _find_modelscope_cache()
            source = local_path or _MODEL_NAME
            _model = SentenceTransformer(source, device="cpu")
            logger.info(f"semantic scorer: embedding model loaded from {source}")
        except Exception as exc:  # noqa: BLE001 - semantic layer is optional, never break ranking
            logger.warning(f"semantic scorer unavailable, ranking falls back to rules only: {exc}")
            _model_failed = True
    return _model


def semantic_scores(query_text: str, candidate_texts: list[str]) -> Optional[list[float]]:
    """Cosine similarity of each candidate against the query, in [0, 1].

    Returns None when the model is unavailable (caller falls back to pure
    rule ranking). bge models expect the query side to carry the instruction
    prefix; passage side is encoded as-is.
    """
    if not candidate_texts:
        return []
    model = _load_model()
    if model is None:
        return None
    try:
        query_emb = model.encode(
            [f"为这个句子生成表示以用于检索相关文章：{query_text}"], normalize_embeddings=True
        )
        cand_embs = model.encode(candidate_texts, normalize_embeddings=True, batch_size=32)
        sims = (cand_embs @ query_emb[0]).tolist()
        # normalized embeddings -> cosine in [-1, 1]; clamp into [0, 1]
        return [max(0.0, min(1.0, (s + 1.0) / 2.0)) for s in sims]
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"semantic scoring failed for this batch, falling back to rules: {exc}")
        return None
