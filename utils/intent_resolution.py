"""Resolve Sage request intent using validated preview, fast classifier, then AI.

Callables are injected to keep the original classifiers and security policies
in the analytical core; this module does not independently approve requests.

Used by: utils/request_gate.py.
"""

from utils.errors import IntentResolutionError  # noqa: F401 - re-exported


def resolve_request_intent(
    preview, combined_input, question, *, validate, fast_classify, ai_classify
):
    """Return the original classifier result without changing its structure.

    Only the AI classifier exception is wrapped, matching the old call site.
    Exceptions in preview validation and fast classification still propagate.
    """
    decision = validate(preview)
    if decision is None:
        decision = fast_classify(combined_input, question)
    if decision is None:
        try:
            decision = ai_classify(combined_input)
        except Exception as exc:  # noqa: BLE001 - any classifier failure becomes one typed error
            raise IntentResolutionError("intent classification failed") from exc
    return decision
