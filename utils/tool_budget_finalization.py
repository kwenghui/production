"""Final narrative generation after the analytical tool budget is exhausted.

This module owns the last AI call and its existing evidence-aware fallback.
No additional tools are exposed to the model at this stage.

Used by: utils/core_secure.py.
Tested in: tests/test_ui_helpers.py.
"""


def finalize_after_tool_budget(
    *, client, conversation, tool_results, system_instructions, chat_model, logger
):
    """One last model call with NO tools: answer from the verified results gathered so far."""
    finalization_instructions = (
        system_instructions
        + """

FINALIZATION MODE
- The analytical tool budget for this request is exhausted.
- No more tools are available in this step. Do not ask for or imply another tool call.
- Answer using only validated analytical results already present in the conversation.
- Do not invent missing values, table IDs, series, units, dates or sources.
- If the verified results are insufficient, say specifically what could not be validated.
- Prefer a concise useful answer over discussing internal tool limits.
"""
    )

    try:
        final_response = client.responses.create(
            sage_stage="Final answer after tool budget",
            sage_purpose="Write the final answer using previously retrieved verified observations; "
            "no more analytical tools are available.",
            model=chat_model,
            instructions=finalization_instructions,
            input=conversation,
        )
        model_answer = final_response.output_text or (
            "Sage gathered verified statistical results, but could not produce a "
            "complete narrative answer from them."
        )
    except Exception as error:  # noqa: BLE001 - optional model call: a plain list of results instead
        logger.exception("Final answer generation failed after tool budget exhaustion: %s", error)
        successful_results = [
            item for item in tool_results if isinstance(item, dict) and item.get("success") is True
        ]
        if successful_results:
            last = successful_results[-1]
            table_id = last.get("table_id") or ", ".join(last.get("table_ids", []))
            model_answer = (
                "Sage retrieved verified SingStat data"
                + (f" from table {table_id}" if table_id else "")
                + ", but the final narrative response could not be generated. "
                "Please use the verified results shown in the technical details."
            )
        else:
            model_answer = (
                "Sage could not obtain enough validated SingStat data to answer "
                "this request. Try naming a specific statistic or SingStat table ID."
            )

    return model_answer
