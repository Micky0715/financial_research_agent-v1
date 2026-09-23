"""Export a raw financial-text corpus for the independent `financial_sft` project (v4 stage L).

This script has exactly one job: turn real text this pipeline has already
fetched and cleaned (BrowserAgent's web/PDF fetch output, persisted per run in
outputs/sources/*.json) into a standalone JSONL corpus of
{"text": ..., "source_url": ...} records. It does not call an LLM, does not
label anything, does not know anything about `financial_sft`'s schema, and
does not import or write into that project - the two projects talk to each
other only through the JSONL file this script produces.

Data flow (see docs/technical_report_v4.md for the full pipeline writeup):
    ResearchAgent (search) -> BrowserAgent (fetch + clean, tools/web_reader.py
    / tools/pdf_reader.py) -> Source.content, persisted by
    orchestrator/workflow.py to outputs/sources/{run_id}_{topic}_sources.json
    -> (this script) -> exports/financial_sft_raw_corpus.jsonl

That `content` field is the raw cleaned web/PDF text BrowserAgent fetched -
not AnalyzeAgent's LLM summary and not ReportAgent's generated report. This
script reads it, splits it into sentence-safe chunks, filters obvious junk
(nav menus, cookie banners, anti-bot/WAF challenge pages), deduplicates exact
repeats, and writes it out with per-chunk source_url intact so a downstream
project can do URL-grouped train/val/test splitting without data leakage.

Usage:
    python scripts/export_sft_corpus.py --help
    python scripts/export_sft_corpus.py --dry_run
    python scripts/export_sft_corpus.py --output exports/financial_sft_raw_corpus.jsonl --max_samples 100
"""
import argparse
import json
import random
import re
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# This shell's stdout can default to a non-UTF-8 codepage (observed: cp936/GBK
# on Windows), which garbles printed Chinese text even though the underlying
# strings/files are perfectly valid UTF-8. Same fix as scripts/export_reports.py.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from config import config  # noqa: E402
from tools.domain_rules import get_domain  # noqa: E402

# ------------------------------------------------------------------ #
# Constants
# ------------------------------------------------------------------ #
DEFAULT_OUTPUT = PROJECT_ROOT / "exports" / "financial_sft_raw_corpus.jsonl"
DEFAULT_STATS_PATH = PROJECT_ROOT / "exports" / "financial_sft_corpus_stats.json"
DEFAULT_PREVIEW_PATH = PROJECT_ROOT / "exports" / "financial_sft_corpus_preview.jsonl"
PREVIEW_SAMPLE_SIZE = 50

# Chinese-punctuation-only sentence boundary. Deliberately NOT the general
# utils.text_utils.split_sentences(), which also splits on ASCII '.' and '?'
# / '!' - that breaks decimal financial numbers like "1708.99亿元" into
# "1708." / "99亿元" mid-token (the exact pitfall documented for DCF text
# extraction in eval/bad_cases.md Bad Case 14). Chunk boundaries must never
# land inside a number.
_ZH_SENTENCE_END_RE = re.compile(r"(?<=[。！？；])")

# Lines that are unambiguously boilerplate/UI chrome, not article content.
_JUNK_LINE_RES = [re.compile(p) for p in (
    r"^(登录|注册|扫码登录|立即登录|免费注册)$",
    r"[Cc]ookie",
    r"^(版权所有|Copyright|All Rights Reserved|©).*$",
    r"点击(查看|阅读)更多",
    r"^(首页|导航|菜单|返回顶部|返回首页|上一页|下一页)$",
    r"^(下载|扫码下载).{0,6}(APP|客户端)$",
    r"^分享到",
    r"^(function|var |const |let )\s*[\(=]",
    r"^[{}\[\];:,\"'()]+$",
    r"_waf_|__jsl_clearance|window\.__|document\.cookie",
)]
_URL_ONLY_RE = re.compile(r"^https?://\S+$")
_HTML_TAG_ONLY_RE = re.compile(r"^<[^>]+>$")
_HAS_CJK_RE = re.compile(r"[一-鿿]")
_HAS_NUMBER_RE = re.compile(r"\d")

# Whole-document junk: anti-bot/WAF challenge pages that BrowserAgent fetched
# successfully (HTTP 200) but which contain no real article text at all -
# observed on 100% of xueqiu.com URLs in this corpus (98/98 sampled). These
# must be dropped at the document level, before chunking, or they pollute
# every chunk derived from them.
_WAF_DOCUMENT_RE = re.compile(r'"_waf_|__jsl_clearance|antispider|verify.*slider')


def _is_junk_line(line: str) -> bool:
    if _URL_ONLY_RE.match(line) or _HTML_TAG_ONLY_RE.match(line):
        return True
    return any(p.search(line) for p in _JUNK_LINE_RES)


def _looks_like_repetitive_garbage(text: str) -> bool:
    """Low character-diversity strings (repeated boilerplate tokens, base64-ish
    blobs) that survive line filtering but are not real prose."""
    if len(text) < 30:
        return False
    unique_ratio = len(set(text)) / len(text)
    return unique_ratio < 0.15


# ------------------------------------------------------------------ #
# Step 1: locate and read real run data (outputs/sources + outputs/traces)
# ------------------------------------------------------------------ #
def _load_trace_for(run_id: str, trace_dir: Path) -> dict[str, Any]:
    """Best-effort: outputs/traces/{run_id}_*_trace.json carries the exact
    original topic string, the run's start time, and (for company_research
    runs) the entity_validator's resolved entity_name - all real values the
    pipeline already computed, not anything guessed by this script."""
    matches = list(trace_dir.glob(f"{run_id}_*_trace.json"))
    if not matches:
        return {}
    try:
        return json.loads(matches[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def iter_raw_documents(source_dir: Path, trace_dir: Path) -> tuple[list[dict[str, Any]], int]:
    """Read every outputs/sources/*.json, flatten to one dict per Source entry,
    then collapse to unique documents by url (the same article gets re-fetched
    and re-scored across multiple topic runs; re-chunking identical content N
    times would just produce N duplicate chunk sets that dedup would remove
    anyway - collapsing here is the efficient, equivalent place to do it).

    Returns (unique_documents, total_entries_seen).
    """
    seen_keys: set[str] = set()
    documents: list[dict[str, Any]] = []
    total_entries = 0
    trace_cache: dict[str, dict[str, Any]] = {}

    for sources_path in sorted(source_dir.glob("*_sources.json")):
        run_id = sources_path.stem.split("_", 1)[0]
        try:
            entries = json.loads(sources_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[warn] skipping unreadable {sources_path.name}: {exc}")
            continue
        if not isinstance(entries, list):
            continue

        if run_id not in trace_cache:
            trace_cache[run_id] = _load_trace_for(run_id, trace_dir)
        trace = trace_cache[run_id]
        topic = trace.get("topic") or sources_path.stem.replace("_sources", "").split("_", 1)[-1]
        retrieved_at = trace.get("started_at", "")
        entity_validation = trace.get("entity_validation") or {}
        # Only trust entity_name as a "company" label when the pipeline's own
        # entity_validator actually resolved this run's subject as a company
        # (entity_type == "company"); never guess it ourselves.
        company = (entity_validation.get("entity_name", "")
                   if entity_validation.get("entity_type") == "company" else "")

        for item in entries:
            total_entries += 1
            content = (item.get("content") or "").strip()
            if not content:
                continue
            url = (item.get("url") or "").strip()
            source_type = item.get("source_type", "web")
            if url:
                key = url
            else:
                # No URL (e.g. a local_file source, or a malformed entry) -
                # synthesize a stable per-document id from run_id+source_id so
                # every chunk from THIS document still shares one identifier,
                # without ever colliding across documents or runs.
                key = f"local://{run_id}_{item.get('source_id', 'unknown')}"
            if key in seen_keys:
                continue
            seen_keys.add(key)

            domain = item.get("domain") or (get_domain(url) if url else "")
            documents.append({
                "source_url": key,
                "url_missing": not bool(url),
                "content": content,
                "title": item.get("title", "") or "",
                "source_type": source_type,
                "authority_tier": item.get("authority_tier", "") or "",
                "source_domain": domain,
                "query": topic,
                "retrieved_at": retrieved_at,
                "company": company,
            })

    return documents, total_entries


# ------------------------------------------------------------------ #
# Step 2: chunking (sentence-safe, paragraph-aware, target min~max chars)
# ------------------------------------------------------------------ #
def chunk_document(content: str, min_chars: int, max_chars: int) -> list[str]:
    """Split one document's cleaned text into contiguous, sentence-safe chunks.

    The natural paragraph (one line of already-cleaned web/PDF text) is the
    base unit - real financial news paragraphs are usually one to a few
    sentences, e.g. "...营业总收入1708.99亿元，同比增长15.71%。归属于...
    862.28亿元..." (~90 chars), which is exactly the kind of self-contained,
    multi-entity unit worth keeping intact rather than either fragmenting or
    padding out. So: accumulate paragraphs into a chunk only until it crosses
    min_chars, then flush - short paragraphs get merged with their neighbor(s)
    just enough to become a viable sample, but a paragraph that's already
    long enough is emitted on its own instead of being force-packed toward
    max_chars. Tried the opposite (always greedy-fill to max_chars) first and
    it pushed contains_number_ratio from ~0.85 to ~0.97: once merges default
    to ~800 chars, nearly every chunk sweeps in at least one stray digit
    somewhere, which erases most of the number-free negative samples the
    downstream project explicitly needs (see spec section 11).
    """
    paragraphs: list[str] = []
    for raw_line in content.split("\n"):
        line = raw_line.strip()
        if not line or _is_junk_line(line):
            continue
        paragraphs.append(line)
    if not paragraphs:
        return []

    tokens: list[str] = []  # sentences interleaved with a paragraph-break marker
    for para in paragraphs:
        parts = [s.strip() for s in _ZH_SENTENCE_END_RE.split(para) if s.strip()]
        tokens.extend(parts)
        tokens.append("\0PARA\0")

    chunks: list[str] = []
    buf = ""
    for tok in tokens:
        if tok == "\0PARA\0":
            if len(buf) >= min_chars:
                chunks.append(buf.strip())
                buf = ""
            continue
        candidate = buf + tok if buf else tok
        if len(candidate) > max_chars and buf:
            chunks.append(buf.strip())
            buf = tok
        else:
            buf = candidate
        # A single sentence longer than max_chars on its own: hard-cut at the
        # last comma before the limit rather than mid-word, as a last resort.
        while len(buf) > max_chars:
            cut = buf.rfind("，", 0, max_chars)
            if cut < min_chars:
                cut = max_chars
            chunks.append(buf[:cut].strip())
            buf = buf[cut:]
    if buf.strip():
        chunks.append(buf.strip())

    # Fold tiny trailing fragments (leftover after the final paragraph, below
    # half the target minimum) into the previous chunk instead of emitting an
    # orphan sliver that would just get dropped by the length filter anyway.
    merged: list[str] = []
    for c in chunks:
        if merged and len(c) < min_chars * 0.5 and len(merged[-1]) + len(c) <= max_chars * 1.2:
            merged[-1] = f"{merged[-1]}{c}"
        else:
            merged.append(c)
    return [c for c in merged if c.strip()]


# ------------------------------------------------------------------ #
# Step 3: chunk-level quality filter
# ------------------------------------------------------------------ #
def is_acceptable_chunk(text: str, min_chars: int, max_chars: int) -> bool:
    """Reject obvious junk. Deliberately does NOT require digits/financial
    keywords - the downstream SFT project needs negative samples (real
    financial prose with no extractable metric) as much as positive ones."""
    if not text or not (min_chars <= len(text) <= max_chars * 1.5):
        return False
    if not _HAS_CJK_RE.search(text):
        return False
    if _WAF_DOCUMENT_RE.search(text):
        return False
    if _looks_like_repetitive_garbage(text):
        return False
    return True


def normalize_for_dedup(text: str) -> str:
    text = text.strip()
    text = re.sub(r"[ \t　]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text


# ------------------------------------------------------------------ #
# Pipeline
# ------------------------------------------------------------------ #
def build_corpus(source_dir: Path, trace_dir: Path, min_chars: int, max_chars: int
                  ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    documents, total_entries = iter_raw_documents(source_dir, trace_dir)

    docs_dropped_as_waf = 0
    chunks_before_filter = 0
    chunks_filtered_out = 0
    records: list[dict[str, Any]] = []

    for doc in documents:
        if _WAF_DOCUMENT_RE.search(doc["content"][:2000]):
            docs_dropped_as_waf += 1
            continue
        for chunk_text in chunk_document(doc["content"], min_chars, max_chars):
            chunks_before_filter += 1
            if not is_acceptable_chunk(chunk_text, min_chars, max_chars):
                chunks_filtered_out += 1
                continue
            records.append({
                "text": chunk_text,
                "source_url": doc["source_url"],
                "title": doc["title"],
                "company": doc["company"],
                "query": doc["query"],
                "retrieved_at": doc["retrieved_at"],
                "source_domain": doc["source_domain"],
                "source_type": doc["source_type"],
                "authority_tier": doc["authority_tier"],
                "exported_from": "financial_research_agent-v1",
                "source_stage": "browser",
                "is_llm_generated": False,
            })

    # Exact-duplicate dedup on normalized text (keep first occurrence).
    seen_norm: set[str] = set()
    deduped: list[dict[str, Any]] = []
    duplicate_count = 0
    for rec in records:
        norm = normalize_for_dedup(rec["text"])
        if norm in seen_norm:
            duplicate_count += 1
            continue
        seen_norm.add(norm)
        deduped.append(rec)

    stats = {
        "raw_source_entries": total_entries,
        "raw_document_count": len(documents),
        "documents_dropped_as_waf_or_antibot": docs_dropped_as_waf,
        "chunk_count_before_filter": chunks_before_filter,
        "chunks_filtered_out": chunks_filtered_out,
        "chunk_count_after_filter": len(records),
        "duplicate_chunks_removed": duplicate_count,
        "deduplicated_count": len(records),
        "final_count": len(deduped),
        "missing_source_url_count": sum(1 for d in documents if d["url_missing"]),
    }
    return deduped, stats


def compute_full_stats(records: list[dict[str, Any]], base_stats: dict[str, Any]) -> dict[str, Any]:
    char_lens = [len(r["text"]) for r in records]
    unique_urls = {r["source_url"] for r in records}
    contains_number = sum(1 for r in records if _HAS_NUMBER_RE.search(r["text"]))
    domain_counter = Counter(r["source_domain"] for r in records if r["source_domain"])
    company_counter = Counter(r["company"] for r in records if r["company"])

    stats = dict(base_stats)
    stats.update({
        "unique_source_count": len(unique_urls),
        "avg_chars": round(statistics.mean(char_lens), 1) if char_lens else 0,
        "median_chars": statistics.median(char_lens) if char_lens else 0,
        "max_chars": max(char_lens) if char_lens else 0,
        "min_chars": min(char_lens) if char_lens else 0,
        "contains_number_ratio": round(contains_number / len(records), 4) if records else 0.0,
        "no_number_ratio": round(1 - contains_number / len(records), 4) if records else 0.0,
        "source_domain_top10": domain_counter.most_common(10),
        "company_top10": company_counter.most_common(10),
    })
    return stats


def print_stats_report(stats: dict[str, Any]) -> None:
    print("\n===== 导出统计 =====")
    print(f"原始 source 记录数（跨全部 run 文件）      : {stats['raw_source_entries']}")
    print(f"去重后原始文档/网页数（按 source_url）       : {stats['raw_document_count']}")
    print(f"整篇判定为反爬/WAF垃圾并丢弃的文档数         : {stats['documents_dropped_as_waf_or_antibot']}")
    print(f"切分出的原始 chunk 数                        : {stats['chunk_count_before_filter']}")
    print(f"被质量过滤器丢弃的 chunk 数                  : {stats['chunks_filtered_out']}")
    print(f"过滤后 chunk 数                              : {stats['chunk_count_after_filter']}")
    print(f"文本级去重移除的重复 chunk 数                : {stats['duplicate_chunks_removed']}")
    print(f"最终样本数                                    : {stats['final_count']}")
    print(f"unique source_url 数量                       : {stats.get('unique_source_count', 'N/A')}")
    print(f"缺失 source_url（使用了合成标识符）的文档数   : {stats['missing_source_url_count']}")
    if "avg_chars" in stats:
        print(f"text 平均字符数 / 中位数 / 最大 / 最小       : "
              f"{stats['avg_chars']} / {stats['median_chars']} / {stats['max_chars']} / {stats['min_chars']}")
        print(f"包含数字的比例 / 不包含数字的比例            : "
              f"{stats['contains_number_ratio']} / {stats['no_number_ratio']}")
        if stats.get("source_domain_top10"):
            print("来源 domain Top 10:")
            for domain, count in stats["source_domain_top10"]:
                print(f"  {domain}: {count}")
        if stats.get("company_top10"):
            print("company 字段 Top 10（来自 entity_validator 已解析结果，非猜测）:")
            for company, count in stats["company_top10"]:
                print(f"  {company}: {count}")
    print("=====================\n")


# ------------------------------------------------------------------ #
# CLI
# ------------------------------------------------------------------ #
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export a raw financial-text JSONL corpus from this project's "
                    "already-fetched BrowserAgent output, for the independent "
                    "financial_sft project to consume.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help=f"Output JSONL path (default: {DEFAULT_OUTPUT.relative_to(PROJECT_ROOT)})")
    parser.add_argument("--source", type=Path, default=config.SOURCES_DIR,
                        help=f"Directory containing *_sources.json run artifacts "
                             f"(default: {config.SOURCES_DIR.relative_to(PROJECT_ROOT)})")
    parser.add_argument("--max_samples", type=int, default=None,
                        help="Cap the number of chunks written (random subset, seeded by --seed). "
                             "Default: no cap, export everything that survives filtering+dedup.")
    parser.add_argument("--min_chars", type=int, default=150,
                        help="Minimum chunk length in characters (default: 150)")
    parser.add_argument("--max_chars", type=int, default=800,
                        help="Target maximum chunk length in characters (default: 800)")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for --max_samples subsampling and preview sampling (default: 42)")
    parser.add_argument("--dry_run", action="store_true",
                        help="Run the full pipeline and print stats/sample records, "
                             "but do not write any output files.")
    args = parser.parse_args()

    trace_dir = config.TRACES_DIR
    if not args.source.exists():
        print(f"[error] source directory not found: {args.source}")
        sys.exit(1)

    print(f"reading real BrowserAgent output from: {args.source}")
    print(f"cross-referencing run metadata from : {trace_dir}")

    records, base_stats = build_corpus(args.source, trace_dir, args.min_chars, args.max_chars)

    rng = random.Random(args.seed)
    if args.max_samples is not None and len(records) > args.max_samples:
        records = rng.sample(records, args.max_samples)
        print(f"[info] --max_samples={args.max_samples}: randomly subsampled from "
              f"{base_stats['final_count']} filtered+deduped chunks (seed={args.seed})")

    full_stats = compute_full_stats(records, base_stats)
    print_stats_report(full_stats)

    preview_n = min(PREVIEW_SAMPLE_SIZE, len(records))
    preview_records = rng.sample(records, preview_n) if records else []

    if args.dry_run:
        print(f"[dry_run] would write {len(records)} record(s) to {args.output} (not written)")
        print(f"[dry_run] would write stats to {DEFAULT_STATS_PATH.relative_to(PROJECT_ROOT)} (not written)")
        print(f"[dry_run] would write {preview_n} preview record(s) to "
              f"{DEFAULT_PREVIEW_PATH.relative_to(PROJECT_ROOT)} (not written)")
        print("\n--- sample records ---")
        for rec in records[: min(5, len(records))]:
            print(json.dumps(rec, ensure_ascii=False))
        return

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"wrote {len(records)} record(s) -> {args.output}")

    stats_path = args.output.parent / "financial_sft_corpus_stats.json"
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(full_stats, f, ensure_ascii=False, indent=2)
    print(f"wrote stats -> {stats_path}")

    preview_path = args.output.parent / "financial_sft_corpus_preview.jsonl"
    with open(preview_path, "w", encoding="utf-8") as f:
        for rec in preview_records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"wrote {preview_n} preview record(s) -> {preview_path}")


if __name__ == "__main__":
    main()
