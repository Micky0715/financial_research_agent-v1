"""Web page fetch + text extraction tool."""
import requests
from bs4 import BeautifulSoup

from config import config
from utils.logger import logger
from utils.text_utils import clean_text

_STRIP_TAGS = ["script", "style", "nav", "footer", "header", "aside", "noscript", "form"]
_MIN_LINE_LEN = 8


def read_webpage(url: str, max_chars: int = 8000) -> dict:
    """Fetch a URL and extract cleaned body text.

    Returns a dict: {success, url, title, content, error}. Never raises -
    any network/parse failure is captured in `error` with success=False so a
    single bad page cannot take down the pipeline.
    """
    result = {"success": False, "url": url, "title": "", "content": "", "error": None}

    if not url or not url.lower().startswith(("http://", "https://")):
        result["error"] = "invalid_url"
        return result

    headers = {"User-Agent": config.USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, timeout=config.REQUEST_TIMEOUT)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"read_webpage failed for {url}: {exc}")
        result["error"] = str(exc)
        return result

    try:
        resp.encoding = resp.apparent_encoding or resp.encoding
        soup = BeautifulSoup(resp.text, "html.parser")

        for tag_name in _STRIP_TAGS:
            for tag in soup.find_all(tag_name):
                tag.decompose()

        title = soup.title.get_text(strip=True) if soup.title else ""

        lines = []
        for line in soup.get_text("\n").splitlines():
            line = line.strip()
            if len(line) >= _MIN_LINE_LEN:
                lines.append(line)

        content = clean_text("\n".join(lines))[:max_chars]

        result["success"] = bool(content)
        result["title"] = title
        result["content"] = content
        if not content:
            result["error"] = "empty_content_after_cleaning"
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"read_webpage parse failed for {url}: {exc}")
        result["error"] = str(exc)
        return result
