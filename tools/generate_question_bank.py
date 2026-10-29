"""Rebuild questions/questions.txt and its audit CSV from the local knowledge base.

This used to be a button in the web UI. It spends API budget and rewrites files,
so it is now an operator command run deliberately on the server.

Usage (from the repository root):
    python tools/generate_question_bank.py             # plain wording, no API calls
    python tools/generate_question_bank.py --ai        # polish wording with the model
    python tools/generate_question_bank.py --target 500
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _model_caller():
    """Callable ai(stage, purpose, instructions, payload) -> text, or None."""
    from openai import OpenAI
    from utils.credentials import load_openai_api_key

    key = load_openai_api_key()
    if not key:
        raise SystemExit("--ai needs OPENAI_API_KEY (or SAGE_OPENAI_API_KEY_FILE).")
    client = OpenAI(api_key=key, timeout=45.0, max_retries=2)
    model = os.getenv("SAGE_CHAT_MODEL", "gpt-5.6-luna")

    def call(stage, purpose, instructions, payload):
        result = client.responses.create(model=model, instructions=instructions, input=payload)
        return getattr(result, "output_text", "") or ""

    return call


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--ai",
        action="store_true",
        help="polish wording with the language model (costs API budget)",
    )
    parser.add_argument(
        "--target", type=int, default=1000, help="number of questions (default 1000)"
    )
    args = parser.parse_args(argv)
    if not 1 <= args.target <= 5000:
        parser.error("--target must be between 1 and 5000")

    from utils.question_generator import generate_question_bank

    kb = ROOT / "data" / "knowledge_base.sqlite3"
    if not kb.is_file():
        raise SystemExit(f"Knowledge base not found: {kb}")
    result = generate_question_bank(
        kb,
        ROOT / "questions" / "questions.txt",
        target=args.target,
        metadata_db_paths=(kb,),
        ai=_model_caller() if args.ai else None,
    )
    print(
        f"Question bank rebuilt: {result['questions']} questions across "
        f"{result.get('tables_covered', '?')} tables ({result['uncategorised']} uncategorised; "
        f"{result['simplified_by_ai']} polished by AI). Restart Sage to load it."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
