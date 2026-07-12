"""Lightweight local vector store (v3 stage I).

numpy 余弦检索 + bge-small-zh embedding（复用 tools/semantic_scorer 的本地模型）。
不是 Milvus/FAISS 级别的生产向量库，也不追求：本项目的记忆体量（几十条报告
摘要/坏案例）用暴力余弦就是最合理的实现。

降级：embedding 模型不可用时自动退回关键词重叠打分（jaccard-ish），检索照常
工作只是变糙——依赖缺失不报错。

持久化：memory/index/records.jsonl（文本+metadata）+ embeddings.npy（向量矩阵，
仅在向量模式下存在）。
"""
import json
from pathlib import Path
from typing import Any, Optional

import numpy as np

from utils.logger import logger

_INDEX_DIR = Path(__file__).resolve().parent / "index"
_RECORDS_PATH = _INDEX_DIR / "records.jsonl"
_EMBEDDINGS_PATH = _INDEX_DIR / "embeddings.npy"


def _embed(texts: list[str]) -> Optional[np.ndarray]:
    """bge embedding；模型不可用返回 None（调用方退回关键词模式）。"""
    try:
        from tools.semantic_scorer import _load_model

        model = _load_model()
        if model is None:
            return None
        return np.asarray(model.encode(texts, normalize_embeddings=True, batch_size=32))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"memory embedding unavailable, keyword fallback: {exc}")
        return None


def _keyword_score(query: str, text: str) -> float:
    """无模型时的降级打分：查询字符 bigram 与文本的重叠率。"""
    grams = {query[i:i + 2] for i in range(len(query) - 1)} if len(query) > 1 else {query}
    if not grams:
        return 0.0
    hits = sum(1 for g in grams if g in text)
    return hits / len(grams)


class LightVectorStore:
    """append-only 本地向量库。add() 后需 save()；search() 惰性加载。"""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.embeddings: Optional[np.ndarray] = None
        self._loaded = False

    # ------------------------------------------------------------ #
    def load(self) -> "LightVectorStore":
        if self._loaded:
            return self
        self._loaded = True
        try:
            if _RECORDS_PATH.exists():
                self.records = [
                    json.loads(line) for line in _RECORDS_PATH.read_text(encoding="utf-8").splitlines() if line.strip()
                ]
            if _EMBEDDINGS_PATH.exists():
                self.embeddings = np.load(_EMBEDDINGS_PATH)
                if len(self.records) != len(self.embeddings):
                    logger.warning("memory index records/embeddings length mismatch - rebuilding recommended")
                    self.embeddings = None
        except Exception as exc:  # noqa: BLE001 - corrupt index -> start empty
            logger.warning(f"memory index load failed, starting empty: {exc}")
            self.records, self.embeddings = [], None
        return self

    def rebuild(self, texts: list[str], metadatas: list[dict[str, Any]]) -> dict[str, Any]:
        """全量重建索引（本项目记忆体量小，重建比增量简单可靠）。"""
        self.records = [{"text": t, **m} for t, m in zip(texts, metadatas)]
        emb = _embed(texts) if texts else None
        self.embeddings = emb
        _INDEX_DIR.mkdir(parents=True, exist_ok=True)
        with open(_RECORDS_PATH, "w", encoding="utf-8") as f:
            for r in self.records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        if emb is not None:
            np.save(_EMBEDDINGS_PATH, emb)
        elif _EMBEDDINGS_PATH.exists():
            _EMBEDDINGS_PATH.unlink()
        self._loaded = True
        return {"records": len(self.records), "vector_mode": emb is not None}

    def search(self, query: str, top_k: int = 5, kind: Optional[str] = None) -> list[dict[str, Any]]:
        """相似检索。返回 [{score, text, ...metadata}]，kind 可过滤记忆类型。"""
        self.load()
        candidates = [
            (i, r) for i, r in enumerate(self.records) if kind is None or r.get("kind") == kind
        ]
        if not candidates:
            return []

        if self.embeddings is not None:
            q_emb = _embed([query])
            if q_emb is not None:
                sims = self.embeddings @ q_emb[0]
                scored = [(float(sims[i]), r) for i, r in candidates]
            else:
                scored = [(_keyword_score(query, r["text"]), r) for _, r in candidates]
        else:
            scored = [(_keyword_score(query, r["text"]), r) for _, r in candidates]

        scored.sort(key=lambda x: x[0], reverse=True)
        return [{"score": round(s, 4), **r} for s, r in scored[:top_k]]
