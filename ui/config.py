"""Settings shared by the Sage user interface: paths, limits, locks, versions.

One place for values that several ui/ modules and app.py need. Paths are
relative to the Sage folder. Locks and shared objects come from
utils/process_state.py, so they survive Streamlit's re-runs of app.py and are
shared by every browser session in the same Python process.

Used by: app.py, ui/answer_pipeline.py, ui/answer_view.py, ui/auto_test.py,
ui/core_loader.py, ui/debug_capture.py, ui/question_bank.py,
ui/question_form.py, ui/question_log.py, ui/session.py, ui/sidebar.py,
ui/startup.py.
"""

import logging
import os
import secrets
from pathlib import Path

from utils import process_state as _process_state
from utils.settings import env_opt_in

LOGGER = logging.getLogger("sage_ui")

# --- Versions -----------------------------------------------------------------
SAGE_UI_RELEASE = "V70-FEEDBACK-DOWNLOAD-2026-10-25"  # bump on every release; logged at startup
# The analytical core must declare this version (SAGE_CORE_RELEASE)
# or the page stops: the UI and core are released together.
SAGE_REQUIRED_CORE_RELEASE = "V21-2026-09-28"

# --- Folders and files ----------------------------------------------------------
APP_DIR = Path(__file__).resolve().parent.parent  # the Sage folder (this file is in ui/)
CORE_FILE = APP_DIR / "utils" / "core_secure.py"
CORE_MODULE_NAME = "utils.core_secure"
QUESTIONS_DIR = APP_DIR / "questions"
QUESTION_BANK_FILE = QUESTIONS_DIR / "questions.txt"
GENERATED_AUDIT_FILE = QUESTIONS_DIR / "Generated_Question_Audit.csv"
# NOTE (review F1): this "management" audit does not exist in the package, so
# preferred_source_for_sample() and audited_chart_expected() are inactive. Pointing
# it at questions/Generated_Question_Audit.csv would make sample questions use the
# audited table as a discovery hint -- a behaviour change; decide and test before.
QUESTION_AUDIT_FILE = APP_DIR / "Management_Question_Audit.csv"
# Outcome logs (personal data under PDPA: see .gitignore and log retention).
FAILED_QUESTIONS_FILE = QUESTIONS_DIR / "failed.txt"
SUCCESS_QUESTIONS_FILE = QUESTIONS_DIR / "success.txt"
# Overridable so a sandboxed deployment can keep the code directory read-only.
DEBUG_LOG_FILE = Path(os.getenv("SAGE_DEBUG_LOG_FILE") or (APP_DIR / "debug.txt"))
# Operator self-test progress (ui/auto_test.py).
TEST_CHECKPOINT_FILE = APP_DIR / "test_progress.json"
TEST_RESET_MARKER_FILE = APP_DIR / "test_reset_once.json"

# --- Limits and switches --------------------------------------------------------
MAX_UI_INPUT_CHARS = 4000  # longest question accepted from the page
MAX_CORE_HISTORY_MESSAGES = 8  # earlier turns passed to the core with a question
# Full per-question diagnostics require explicit operator opt-in.
SAGE_FULL_DEBUG_LOG = env_opt_in("SAGE_FULL_DEBUG_LOG")

# --- Process-wide locks and shared state ----------------------------------------
FAILED_FILE_LOCK = _process_state.lock("failed_file")
SUCCESS_FILE_LOCK = _process_state.lock("success_file")
DEBUG_FILE_LOCK = _process_state.lock("debug_file", reentrant=True)
DEBUG_CONSOLE_LOCK = _process_state.lock("debug_console", reentrant=True)
CORE_IMPORT_LOCK = _process_state.lock("core_import", reentrant=True)
TEST_CHECKPOINT_LOCK = _process_state.lock("test_checkpoint")
TEST_RESET_LOCK = _process_state.lock("test_reset")
ACTIVE_DEBUG_QUERY_THREADS = _process_state.shared("active_debug_query_threads", set)
# Stable across browser sessions in ONE Python process; a new server process
# gets a new token (the self-test then starts again from Question 1).
TEST_PROCESS_TOKEN = os.environ.setdefault("_SAGE_PROCESS_RESET_TOKEN", secrets.token_hex(24))
