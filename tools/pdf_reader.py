"""PDF parsing tool built on PyMuPDF, supporting local paths and remote URLs."""
import tempfile
from pathlib import Path

import requests

from config import config
from utils.logger import logger


def _download_pdf(url: str) -> str:
    """Download a remote PDF to a temp file and return its local path."""
    headers = {"User-Agent": config.USER_AGENT}
    resp = requests.get(url, headers=headers, timeout=config.REQUEST_TIMEOUT)
    resp.raise_for_status()
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp.write(resp.content)
    tmp.close()
    return tmp.name


def read_pdf(path_or_url: str) -> dict:
    """Extract text from a local PDF path or a remote PDF URL.

    Returns a dict: {success, page_count, full_text, pages, error}, where
    `pages` is a list of {page_number, text}. Never raises - failures are
    reported via success=False/error so callers can skip bad PDFs.
    """
    result = {
        "success": False,
        "page_count": 0,
        "full_text": "",
        "pages": [],
        "error": None,
    }

    is_remote = path_or_url.lower().startswith(("http://", "https://"))
    local_path = None
    downloaded = False

    try:
        if is_remote:
            local_path = _download_pdf(path_or_url)
            downloaded = True
        else:
            local_path = path_or_url
            if not Path(local_path).exists():
                result["error"] = "file_not_found"
                return result

        import fitz  # PyMuPDF

        doc = fitz.open(local_path)
        pages = []
        full_text_parts = []
        for i, page in enumerate(doc):
            text = page.get_text().strip()
            pages.append({"page_number": i + 1, "text": text})
            if text:
                full_text_parts.append(text)
        doc.close()

        result["success"] = True
        result["page_count"] = len(pages)
        result["pages"] = pages
        result["full_text"] = "\n".join(full_text_parts)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"read_pdf failed for {path_or_url}: {exc}")
        result["error"] = str(exc)
        return result
    finally:
        if downloaded and local_path:
            try:
                Path(local_path).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
