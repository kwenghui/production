"""Operator self-test: run every sample question in turn (``-- --test``).

Started with ``streamlit run app.py -- --test`` (requires
SAGE_ALLOW_ADMIN_TESTS=1). Progress is checkpointed to test_progress.json so
a restarted browser continues where it stopped; a NEW server process starts
again from the first question. Results go to failed.txt / success.txt.

Used by: app.py.
Tested in: tests/test_ui_helpers.py.
"""

from datetime import datetime, timezone
from pathlib import Path
import argparse
import json
import os
import time

import streamlit as st

from ui.config import (
    LOGGER,
    TEST_CHECKPOINT_FILE,
    TEST_RESET_MARKER_FILE,
    TEST_CHECKPOINT_LOCK,
    TEST_PROCESS_TOKEN,
    TEST_RESET_LOCK,
)
from ui.session import select_sample_question


def parse_test_options(argv):
    """Parse Streamlit script args (supplied after the CLI's bare -- token)."""
    args = list(argv)
    if args and args[0] == "--":
        args.pop(0)
    parser = argparse.ArgumentParser(
        prog="app.py", description="Sage automatic question-bank regression mode"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable diagnostics for submitted questions; start with a blank idle " "screen",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Offer a Start tests button for the saved question bank (never " "auto-start)",
    )
    parser.add_argument(
        "--test-interval",
        type=float,
        default=10.0,
        metavar="SECONDS",
        help="Pause after each finished query (default 10)",
    )
    parser.add_argument(
        "--test-reset",
        "--reset",
        dest="test_reset",
        action="store_true",
        help="Start a new test at question 1 (reset checkpoint only; keep SQLite "
        "and saved logs)",
    )
    options = parser.parse_args(args)
    if options.test_interval < 1 or not float(options.test_interval) < float("inf"):
        raise ValueError("--test-interval must be a finite number of at least 1 second")
    if options.test_reset and not (options.test or options.debug):
        raise ValueError("--test-reset requires --debug or --test")
    return options


def read_test_checkpoint(path, bank_hash, total):
    """Return next question index from a compatible, already-written checkpoint."""
    source = Path(path)
    if not source.exists():
        return 0
    try:
        record = json.loads(source.read_text(encoding="utf-8"))
        if record.get("bank_sha256") != bank_hash or record.get("question_count") != total:
            raise ValueError("The question bank changed since the last test")
        index = record.get("next_index")
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= total:
            raise ValueError("Saved test position is invalid")
        return index
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        raise ValueError("Cannot safely resume automatic testing: " + str(error)) from error


def write_test_checkpoint(next_index, bank_hash, total, *, path=None):
    """Atomically persist progress after a query is recorded to success/failed."""
    target = Path(path) if path is not None else TEST_CHECKPOINT_FILE
    record = {
        "version": 1,
        "bank_sha256": bank_hash,
        "question_count": total,
        "next_index": next_index,
        "completed": next_index == total,
        "updated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        with TEST_CHECKPOINT_LOCK:
            temporary = target.with_name(target.name + ".tmp")
            temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, target)
        return True
    except OSError:
        LOGGER.exception("Unable to save automatic test checkpoint to %s", target)
        return False


def reset_checkpoint_once_per_server(
    bank_hash, total, *, checkpoint=None, marker=None, process_token=None
):
    """An explicit --reset takes effect ONCE during this Python server run.

    If the browser reconnects or a second tab opens, reuse the checkpoint
    instead of silently restarting 180 questions. Restarting the Python process
    with --reset remains an explicit new reset; omit the flag to resume later.
    """
    checkpoint = Path(checkpoint) if checkpoint is not None else TEST_CHECKPOINT_FILE
    marker = Path(marker) if marker is not None else TEST_RESET_MARKER_FILE
    token = process_token if process_token is not None else TEST_PROCESS_TOKEN
    if not isinstance(token, str) or not token:
        raise ValueError('Invalid server reset token')
    with TEST_RESET_LOCK:
        try:
            info = json.loads(marker.read_text(encoding='utf-8')) if marker.exists() else {}
        except (OSError, UnicodeError, ValueError, TypeError):
            # A corrupt marker is not proof that --reset has already happened.
            # Do not silently overwrite the user's checkpoint in this case.
            raise ValueError(
                'Invalid reset marker. Start without --reset to ' 'preserve the saved checkpoint.'
            )
        if (
            info.get('server_token') == token
            and info.get('bank_sha256') == bank_hash
            and info.get('question_count') == total
        ):
            return read_test_checkpoint(checkpoint, bank_hash, total)
        # Transactional safety: if we cannot record that this process already
        # consumed --reset, the *previous* checkpoint must not be silently lost.
        # The backup is local and stores checkpoint metadata only (no API keys).
        backup = checkpoint.with_name(checkpoint.name + '.before_reset.bak')
        had_previous = checkpoint.exists()
        try:
            if had_previous:
                previous = checkpoint.read_bytes()
                backup.write_bytes(previous)
        except OSError as exc:
            raise ValueError(
                'Cannot back up previous test checkpoint; '
                'reset cancelled without changing progress.'
            ) from exc
        if not write_test_checkpoint(0, bank_hash, total, path=checkpoint):
            raise ValueError(
                'Could not create new checkpoint; previous '
                'checkpoint is retained in .before_reset.bak'
            )
        receipt = {
            'server_token': token,
            'bank_sha256': bank_hash,
            'question_count': total,
            'reset_utc': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        }
        temporary = marker.with_name(marker.name + '.tmp')
        try:
            temporary.write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
            os.replace(temporary, marker)
        except OSError as exc:
            try:
                with TEST_CHECKPOINT_LOCK:
                    if had_previous:
                        # atomic restore of the exact previous checkpoint
                        restored = checkpoint.with_name(checkpoint.name + '.restore.tmp')
                        restored.write_bytes(backup.read_bytes())
                        os.replace(restored, checkpoint)
                    else:
                        checkpoint.unlink(missing_ok=True)
            except OSError:
                raise ValueError(
                    'Reset marker failed AND checkpoint restoration '
                    'failed; restore progress from '
                    'test_progress.json.before_reset.bak manually.'
                ) from exc
            raise ValueError(
                'Reset marker failed: previous checkpoint restored. '
                'Start WITHOUT --reset to preserve the run.'
            ) from exc
        return 0


def mark_auto_test_question_complete(question) -> None:
    """Advance checkpoint once, including for failed answers, after logging."""
    state = st.session_state
    if not state.get("auto_test_bank_hash") or state.get("auto_test_running_question") != question:
        return
    if state.get("auto_test_log_failed"):
        # Do NOT checkpoint this question: it will be retried after the log
        # directory is writable again, preserving one recorded result/query.
        state.auto_test_running_question = None
        state.auto_test_running_index = None
        return
    completed = int(state.auto_test_running_index) + 1
    # V32: Never tell the UI that a question is complete unless its checkpoint
    # was durably saved. On failure preserve the original pending index so a
    # restarted process retries it rather than silently skipping an answer.
    if not write_test_checkpoint(completed, state.auto_test_bank_hash, state.auto_test_total):
        state.auto_test_error = (
            "Cannot save test_progress.json. Progress was not "
            "advanced; check disk permissions and resume "
            "without --reset."
        )
        state.auto_test_paused = True
        state.auto_test_running_question = None
        state.auto_test_running_index = None
        return
    state.auto_test_completed = completed
    state.auto_test_running_question = None
    state.auto_test_running_index = None
    state.auto_test_finished = completed >= int(state.auto_test_total)
    state.auto_test_next_due_at = (
        None if state.auto_test_finished else time.time() + float(state.auto_test_interval)
    )


def auto_test_tick(flat_questions) -> None:
    """Auto-select Next on deadline, only after the preceding query completed."""
    state = st.session_state
    if (
        not state.get("auto_test_bank_hash")
        or state.get("auto_test_finished")
        or state.get("auto_test_paused")
        or state.get("auto_test_error")
        or state.get("answer_phase") != "ready"
        or state.get("auto_test_running_question") is not None
    ):
        return
    due_at = state.get("auto_test_next_due_at")
    if due_at is None:
        return
    remaining = max(0, int(due_at - time.time() + 0.999))
    if remaining:
        st.caption(f"Next sample in {remaining}s")
        return
    next_index = int(state.auto_test_completed)
    if next_index >= len(flat_questions):
        state.auto_test_finished = True
        return
    # Matches the Next button: previous result cleared *before* analysis runs.
    select_sample_question(flat_questions[next_index], next_index)
    state.auto_test_running_index = next_index
    state.auto_test_running_question = flat_questions[next_index]["question"]
    state.auto_test_next_due_at = None
    return


def toggle_auto_test_pause() -> None:
    """A manual Pause/Resume control, without resetting saved progress."""
    state = st.session_state
    state.auto_test_paused = not state.auto_test_paused
    if not state.auto_test_paused and state.get("auto_test_next_due_at") is not None:
        state.auto_test_next_due_at = time.time() + float(state.auto_test_interval)


def render_auto_test_status(flat_questions) -> None:
    """Show completion and leave manual question navigation disabled in test mode."""
    state = st.session_state
    if state.get("auto_test_error"):
        st.error(state.auto_test_error)
        return
    if not state.get("auto_test_bank_hash"):
        return
    completed = int(state.auto_test_completed)
    total = len(flat_questions)
    if state.get("auto_test_finished"):
        st.success(
            f"Automatic test finished: {completed}/{total} questions processed. "
            "Review success.txt and failed.txt; progress is saved in test_progress.json."
        )
        return
    message = f"Automatic test: {completed}/{total} completed"
    if state.get("auto_test_paused"):
        message += " · paused"
    elif state.get("auto_test_next_due_at") is not None:
        remaining = max(0, int(state.auto_test_next_due_at - time.time() + 0.999))
        message += " · waiting for the next question"
    else:
        message += " · checking official data"
    col, control = st.columns([7, 1], vertical_alignment="center")
    with col:
        st.info(message)
    with control:
        st.button(
            "Resume" if state.get("auto_test_paused") else "Pause",
            on_click=toggle_auto_test_pause,
            disabled=bool(state.get("auto_test_error")),
        )
