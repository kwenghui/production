"""Single entry point for Sage request security, intent and scope gating.

Called once per question, as STEP 1 of ``core_secure.ask_sage``. It answers
one question: "may Sage work on this request at all?"

Why so many function arguments? The real checks (injection patterns, the
intent classifier, the standard refusal messages) live in ``core_secure.py``.
That file imports this one, so this one cannot import it back (a circular
import). Instead ``ask_sage`` passes the functions in. This file only fixes
the ORDER in which they run.

No model prompts or security policies live here: existing callbacks are used
in their original order. The caller owns all downstream discovery and evidence.

Used by: utils/core_secure.py.
Tested in: tests/test_security.py.
"""

from dataclasses import dataclass
import re
from typing import Optional
from utils.request_security import prepare_request
from utils.intent_resolution import IntentResolutionError, resolve_request_intent
from utils.vocabulary import CLARIFICATION_TOPICS


@dataclass(frozen=True)
class RequestGateResult:
    """Outcome of the gate. If ``blocked_response`` is not None, return it to
    the user as-is and do nothing else; otherwise continue with the other fields."""

    question: str
    combined_input: Optional[str]
    intent_decision: Optional[dict]
    blocked_response: Optional[dict]


# Clarification is a user-experience response only. It cannot override the
# existing injection/security checks or mark a request in scope.
_TOPIC_HINTS = CLARIFICATION_TOPICS  # vocabulary lives in utils/vocabulary.py


def _clarification_if_ambiguous(question, reason, response):
    """A "did you mean Singapore's <topic>?" reply for a short question hinting at one topic, else
    the refusal."""
    if reason in ("intent_classification_failed", "security_blocked", "prompt_injection"):
        return response
    clean = re.sub(r"\s+", " ", question.strip().lower())
    # Never reinterpret an obvious unrelated question as a SingStat request.
    if not clean or len(clean.split()) > 12:
        return response
    hints = [
        label
        for label, words in _TOPIC_HINTS
        if any(re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", clean) for word in words)
    ]
    if len(hints) != 1:
        return response
    result = dict(response)
    result["answer"] = (
        "Are you asking about Singapore's "
        + hints[0]
        + " statistics? If so, you can say what you would like to know "
        "(for example, how it has changed over time), or rephrase your question. "
        "I won't assume which figures you mean until your intention is clearer."
    )
    result["clarification_requested"] = True
    return result


def gate_request(
    question,
    *,
    chat_history,
    combined_user_input,
    intent_decision,
    sanitize,
    is_injection,
    security_response,
    combine,
    on_injection,
    validate,
    fast_classify,
    ai_classify,
    out_of_scope,
    on_intent_error,
    on_out_of_scope,
):
    """Run the security and intent gates, with no discovery side effects.

    Order: (1) sanitise and look for injection in the new question and the
    whole conversation; (2) classify intent: reuse the caller's decision if
    valid, else a fast keyword rule for explicit table requests, else one AI
    call; (3) refuse out-of-scope requests, offering a clarification when a
    short question clearly hints at one statistics topic.
    """
    # (1) Input safety: sanitise text and block prompt/code injection.
    prepared = prepare_request(
        question,
        chat_history=chat_history,
        combined_user_input=combined_user_input,
        sanitize=sanitize,
        is_injection=is_injection,
        security_response=security_response,
        combine=combine,
        on_injection=on_injection,
    )
    if prepared.blocked_response is not None:
        return RequestGateResult(
            prepared.question, prepared.combined_input, None, prepared.blocked_response
        )
    # (2) Is this a Singapore-statistics request, and which tables are relevant?
    try:
        decision = resolve_request_intent(
            intent_decision,
            prepared.combined_input,
            prepared.question,
            validate=validate,
            fast_classify=fast_classify,
            ai_classify=ai_classify,
        )
    except IntentResolutionError:
        on_intent_error()
        return RequestGateResult(
            prepared.question,
            prepared.combined_input,
            None,
            out_of_scope(
                summary="The current request could not be classified " "safely.",
                reason="intent_classification_failed",
            ),
        )
    # (3) Polite refusal (or clarification question) for anything else.
    if not decision['in_scope']:
        on_out_of_scope()
        return RequestGateResult(
            prepared.question,
            prepared.combined_input,
            decision,
            _clarification_if_ambiguous(
                prepared.question,
                decision.get('reason', 'out_of_scope'),
                out_of_scope(
                    summary=decision['summary'], reason=decision.get('reason', 'out_of_scope')
                ),
            ),
        )
    return RequestGateResult(prepared.question, prepared.combined_input, decision, None)
