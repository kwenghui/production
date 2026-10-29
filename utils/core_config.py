"""The analytical core's shared settings: OpenAI client, model names, logger.

Imported by utils/core_secure.py and the modules split out of it, so that they
all use ONE client (with token accounting and budgets, see utils/token_usage.py)
and the same model choices.

The OpenAI key normally arrives from the page via utils.credentials (kept in
memory, never in os.environ). When the core runs on its own (command line),
the same helper falls back to OPENAI_API_KEY (or the legacy name KEY) from the
environment or the Sage folder's .env. Importing this module without a key
raises RuntimeError: nothing can run without one.

Used by: utils/core_secure.py, utils/rag_engine.py.
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from utils.credentials import get_openai_api_key
from utils.model_client import SageModelClient
from utils.token_usage import TrackedOpenAI

# Only the Sage folder's own .env. Without a path, load_dotenv() uses the first .env
# found walking UP from this file's folder (utils\, then the Sage folder, then the
# drive root), so a stray file elsewhere could silently supply an old key.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

logger = logging.getLogger("sage")
APP_DIR = Path(__file__).resolve().parent.parent  # the Sage folder

API_KEY = get_openai_api_key()
if not API_KEY:
    raise RuntimeError(
        "Missing OpenAI key. Add OPENAI_API_KEY=your_openai_api_key to the .env file."
    )

#: The one model client: every call is labelled, counted and budget-checked.
client = SageModelClient(TrackedOpenAI(OpenAI(api_key=API_KEY, timeout=45.0, max_retries=2)))

CHAT_MODEL = os.getenv("SAGE_CHAT_MODEL", "gpt-5.6-luna")
EMBEDDING_MODEL = "text-embedding-3-small"


def _debug_print(*parts, **_ignored) -> None:
    """Developer diagnostics, sent to the DEBUG logger (never to stdout).

    They may paraphrase visitors' questions, so they follow the operator's
    logging configuration (pseudonymisation, retention) like other logs.
    """
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(" ".join(str(part) for part in parts))
