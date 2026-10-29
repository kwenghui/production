"""Loading the analytical core (utils/core_secure.py) into the page.

The core is imported once per Python process (thread-safe) and handed to each
session as st.session_state.core. With SAGE_DEV_RELOAD=1 an edited core file
is reloaded on the next click. A UI/core version mismatch stops the page with
a clear message rather than running mismatched code.

Used by: app.py, ui/question_form.py.
"""

import importlib.util
import sys

import streamlit as st

from utils.errors import log_unexpected, SageError
from ui.config import (
    CORE_FILE,
    CORE_MODULE_NAME,
    LOGGER,
    MAX_CORE_HISTORY_MESSAGES,
    SAGE_REQUIRED_CORE_RELEASE,
    CORE_IMPORT_LOCK,
)
from utils.settings import env_opt_in


def load_core(api_key):
    """Load the Sage analytical core securely from the adjacent file."""
    if not api_key:
        return None

    if not CORE_FILE.exists():
        st.error("utils/core_secure.py is missing. Keep the utils folder alongside app.py.")
        return None

    try:
        # Production: load the core once per process. Hot-reloading on file change is a
        # development convenience only (SAGE_DEV_RELOAD=1); in production it costs a
        # stat() per click and leaves older sessions holding the previous module.
        existing_module = sys.modules.get(CORE_MODULE_NAME)
        # The marker is set only after the module finished executing: never hand out
        # a module another thread is still importing.
        if (
            existing_module is not None
            and not env_opt_in("SAGE_DEV_RELOAD")
            and getattr(existing_module, "__sage_source_mtime_ns__", None) is not None
        ):
            return existing_module
        with CORE_IMPORT_LOCK:
            source_mtime_ns = CORE_FILE.stat().st_mtime_ns
            existing_module = sys.modules.get(CORE_MODULE_NAME)
            loaded_mtime = getattr(existing_module, "__sage_source_mtime_ns__", None)
            if (
                existing_module is not None
                and loaded_mtime is not None
                and (not env_opt_in("SAGE_DEV_RELOAD") or loaded_mtime == source_mtime_ns)
            ):
                return existing_module

            # The core file changed while Streamlit was running. Remove the stale
            # dynamic module so the next import executes the updated source rather
            # than silently continuing with the previous in-memory implementation.
            if existing_module is not None:
                sys.modules.pop(CORE_MODULE_NAME, None)

            # Load the exact supporting file in utils rather than relying on general module
            # discovery. This reduces filename mismatches and module-shadowing risk.
            spec = importlib.util.spec_from_file_location(CORE_MODULE_NAME, CORE_FILE)
            if spec is None or spec.loader is None:
                raise ImportError(f"Unable to create module spec for {CORE_FILE.name}")

            module = importlib.util.module_from_spec(spec)
            sys.modules[CORE_MODULE_NAME] = module

            try:
                # The core reads the key from utils.credentials (process memory);
                # it is never published to os.environ.
                spec.loader.exec_module(module)
                module.__sage_source_mtime_ns__ = source_mtime_ns
            except BaseException:
                # Never leave a half-initialised module importable.
                sys.modules.pop(CORE_MODULE_NAME, None)
                raise

            return module

    except SageError as error:
        LOGGER.log(error.log_level, "Unable to load Sage core: %s", error)
        st.error(error.user_message)
        return None
    except Exception as error:  # noqa: BLE001 - UI boundary: an import can raise anything
        incident = log_unexpected(LOGGER, "Unable to load Sage core", error)
        st.error(f"Sage could not start its analysis engine (reference {incident}).")
        return None


def build_core_history() -> list:
    """Return recent dialogue context for final answer generation."""
    return [
        {
            "role": item["role"],
            "content": item["content"],
        }
        for item in st.session_state.messages[-MAX_CORE_HISTORY_MESSAGES:]
    ]


def ensure_core_loaded(api_key) -> None:
    """Load/reload the analytical engine or show setup guidance and stop the UI run."""
    if api_key:
        # load_core() is mtime-aware, so this is cheap when unchanged and also
        # guarantees that dropping in an updated core takes effect on rerun.
        st.session_state.core = load_core(api_key)

    if st.session_state.core is None:
        st.title("Sage")
        st.caption("Smart Assistant for Government Data Analysis & Explanation")
        st.info(
            "Configure OPENAI_API_KEY in Streamlit secrets or set it " "in your local environment."
        )
        st.stop()

    if getattr(st.session_state.core, "SAGE_CORE_RELEASE", None) != SAGE_REQUIRED_CORE_RELEASE:
        LOGGER.error(
            "Sage UI/core mismatch: expected core=%s actual core=%s",
            SAGE_REQUIRED_CORE_RELEASE,
            getattr(st.session_state.core, "SAGE_CORE_RELEASE", "not updated"),
        )
        # The version comes from the constant, so this text cannot go stale again.
        st.error(
            "The Sage interface and analytical core are different versions. "
            f"Install app.py and utils/core_secure.py from the same release "
            f"(core {SAGE_REQUIRED_CORE_RELEASE}) in the same folder, "
            "stop the old Streamlit process and restart. Your data are unchanged."
        )
        st.stop()
