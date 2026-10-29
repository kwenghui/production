"""Per-request OpenAI model selection; avoids shared-global model changes.

Used by: app.py, ui/sidebar.py, utils/core_config.py.
Tested in: tests/test_small_modules.py.
"""

import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from utils.token_usage import api_operation

MODEL_OPTIONS = (
    "gpt-5.6-luna",
    "gpt-5.1",
    "gpt-5",
    "gpt-5-mini",
    "gpt-5-nano",
    "gpt-4.1",
    "gpt-4.1-mini",
)
_ACTIVE_MODEL = ContextVar("sage_selected_request_model", default=None)
REASONING_EFFORTS = ("minimal", "low", "medium", "high")


def reasoning_effort(env=None):
    """SAGE_REASONING_EFFORT: optional speed/quality trade-off for GPT-5-family models.

    Unset (the default) leaves the model's own default. "low" answers noticeably sooner;
    "minimal" is fastest but weakest at choosing tables and tools. Unknown values are
    ignored.
    """
    env = os.environ if env is None else env
    value = str(env.get("SAGE_REASONING_EFFORT") or "").strip().lower()
    return value if value in REASONING_EFFORTS else None


@contextmanager
def selected_model_scope(model):
    """Keep concurrent Streamlit sessions isolated even during API failures."""
    if model not in MODEL_OPTIONS:
        raise ValueError("Unrecognised model")
    token = _ACTIVE_MODEL.set(model)
    try:
        yield
    finally:
        _ACTIVE_MODEL.reset(token)


class _ResponseAdapter:
    """Wraps ``client.responses`` so the model chosen for this request is used."""

    def __init__(self, wrapped) -> None:
        """Wrap the tracked ``responses`` endpoint."""
        self._wrapped = wrapped

    def create(self, **kwargs):
        """Override model only within the current request's selected scope."""
        stage = kwargs.pop("sage_stage", None)
        purpose = kwargs.pop("sage_purpose", None)
        model = _ACTIVE_MODEL.get()
        if model is not None:
            kwargs["model"] = model
        effort = reasoning_effort()
        if (
            effort
            and "reasoning" not in kwargs
            and str(kwargs.get("model", "")).startswith("gpt-5")
        ):
            kwargs["reasoning"] = {"effort": effort}

        def call(arguments):
            """Make the API call, labelled with its task when a stage and purpose are given."""
            if stage is not None and purpose is not None:
                with api_operation(stage, purpose):
                    return self._wrapped.create(**arguments)
            return self._wrapped.create(**arguments)

        try:
            return call(kwargs)
        except Exception as error:  # noqa: BLE001 - only a rejected effort value is retried
            if not (
                effort
                and "reasoning" in kwargs
                and getattr(error, "status_code", None) == 400
                and "reasoning" in str(error).lower()
            ):
                raise
            # A setting must never break answers: drop it, retry once, say so in the log.
            logging.getLogger("sage").warning(
                "SAGE_REASONING_EFFORT=%r was rejected for model %s; retrying with the default.",
                effort,
                kwargs.get("model"),
            )
            return call({k: v for k, v in kwargs.items() if k != "reasoning"})


class SageModelClient:
    """Wrap the tracked OpenAI client, preserving usage tracking and embeddings."""

    def __init__(self, tracked_client) -> None:
        """Wrap the token-tracking client; ``responses`` gets Sage's labelling adapter."""
        self._client = tracked_client
        self.responses = _ResponseAdapter(tracked_client.responses)

    def with_options(self, **options):
        """Derived clients keep per-request model scoping and usage tracking."""
        return SageModelClient(self._client.with_options(**options))

    def __getattr__(self, key):
        """Every other attribute comes from the tracked client unchanged."""
        return getattr(self._client, key)
