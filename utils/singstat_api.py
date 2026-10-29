"""The knowledge-base builder's small client for the official SingStat API.

Used only by the operator's knowledge-base tools (python -m utils.kb and
utils/kb_structured_ingest.py), never while answering a user's question; that
path goes through utils/official_source.safe_http_get. Like that door, it
talks to ONE fixed host (tablebuilder.singstat.gov.sg/api/table) over HTTPS,
refuses redirects, caps responses at 30 MB and validates table IDs (safe_id).

Used by: utils/kb.py, utils/kb_structured_ingest.py,
utils/official_source.py, tools/diagnose_survey_ranking.py.
"""

from typing import Any
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


class _NoRedirect(HTTPRedirectHandler):
    """Refuse redirects, as utils/official_source._live_http_get does: a redirect
    could leave the approved official host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        """Refuse every HTTP redirect (requests may only go to the allowed host)."""
        return None


_OPENER = build_opener(_NoRedirect)


BASE = "https://tablebuilder.singstat.gov.sg/api/table"


ID_RE = re.compile(r"^(?:[A-Z][0-9]{6}|[0-9]{1,12})$")


UA = "Mozilla/5.0 (compatible; SageSingStatKnowledge/1.0)"


def safe_id(value: Any) -> str:
    """Return a validated, upper-case SingStat table ID, or raise ValueError."""
    ident = str(value or "").strip().upper()
    if not ID_RE.fullmatch(ident):
        raise ValueError(f"Invalid SingStat table identifier: {ident!r}")
    return ident


def request_json(
    path: str, params: dict[str, Any] | None = None, *, timeout: int = 25, tries: int = 3
) -> dict:
    """GET a JSON document from the official SingStat API (fixed host, retries)."""
    if not path.startswith('/') or '//' in path or '..' in path:
        raise ValueError('Invalid endpoint')
    url = BASE + path + ('?' + urlencode(params) if params else '')
    if not url.startswith(BASE + '/'):
        raise ValueError('Invalid URL')
    last: Exception | None = None
    for attempt in range(tries):
        try:
            req = Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
            with _OPENER.open(req, timeout=timeout) as response:
                if response.status != 200:
                    raise RuntimeError(f'HTTP {response.status}')
                raw = response.read(30_000_001)
            if len(raw) > 30_000_000:
                raise ValueError('API response too large')
            payload = json.loads(raw)
            if not isinstance(payload, dict) or str(payload.get('StatusCode', '200')) != '200':
                raise ValueError(f'API rejected request: {str(payload.get("Message", ""))[:200]}')
            return payload
        except (
            HTTPError,
            URLError,
            TimeoutError,
            ValueError,
            RuntimeError,
            json.JSONDecodeError,
        ) as exc:
            last = exc
            if attempt + 1 < tries:
                time.sleep(min(2**attempt, 4))
    raise RuntimeError(f'Official API failed at {path}: {last}')
