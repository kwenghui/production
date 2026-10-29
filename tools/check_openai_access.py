"""Check that the OpenAI key and the ChromaDB index work the way Sage uses them.

Run from the Sage folder, with the same key Sage uses (in .env, .streamlit/secrets.toml
or the OPENAI_API_KEY environment variable):

    python tools/check_openai_access.py

Each check prints OK or the exact reason it failed. The key itself is never printed.
Costs: one tiny AI reply and one embedding (a fraction of a cent).
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from utils.redaction import redact_diagnostic  # noqa: E402

CHAT_MODEL = os.getenv("SAGE_CHAT_MODEL", "gpt-5.6-luna")
EMBEDDING_MODEL = "text-embedding-3-small"  # the model the Sage index was built with
COLLECTION = "sage_singstat_official"
QUESTION = "Singapore residents by single year of age, ethnic group and sex"


def key_fingerprint(key):
    """First 8 hex of SHA-256: identifies a key without revealing it.
    Same formula as utils.credentials.key_fingerprint (kept here so this script
    also works next to older Sage files)."""
    import hashlib

    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8] if key else "none"


def report(step, ok, detail=""):
    print(f"[{'OK  ' if ok else 'FAIL'}] {step}" + (f"\n       {detail}" if detail else ""))
    return ok


def why(exc):
    text = redact_diagnostic(str(exc), 500)
    hint = ""
    if "Missing scopes" in text or "insufficient permissions" in text:
        hint = (
            "\n       -> The key is valid but not allowed to use this service. In the OpenAI "
            "dashboard,"
            "\n          edit the key's permissions: set 'All', or under Restricted give Model"
            "\n          capabilities 'Request' (covers both Responses and Embeddings)."
        )
    elif "Incorrect API key" in text or "invalid_api_key" in text:
        hint = "\n       -> This key is wrong or revoked. Check which key Sage is actually loading."
    return f"{type(exc).__name__}: {text}{hint}"


KEY_NAMES = ("OPENAI_API_KEY", "KEY")


def _toml_keys(path):
    if not path.is_file():
        return {}
    import tomllib

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"       (could not read {path}: {type(exc).__name__})")
        return {}
    return {n: str(data[n]).strip() for n in KEY_NAMES if data.get(n)}


def key_sources():
    """Every place Sage can take a key from, in the order Sage prefers them."""
    sources = []
    project = ROOT / ".streamlit" / "secrets.toml"
    user = Path.home() / ".streamlit" / "secrets.toml"
    # Streamlit merges both secrets files; the project file wins on equal names.
    for label, path in (("project secrets.toml", project), ("GLOBAL secrets.toml", user)):
        for name, value in _toml_keys(path).items():
            sources.append((f"{label} {name}  [{path}]", value))
    from utils.credentials import _key_from_file

    from_file = _key_from_file()
    if from_file:
        sources.append(("key file", from_file))
    for name in KEY_NAMES:  # Windows / process environment (set before Sage starts)
        if os.environ.get(name):
            sources.append((f"Windows/process environment {name}", os.environ[name].strip()))
    # Sage loads ONLY the .env in its own folder.
    try:
        from dotenv import dotenv_values

        own = ROOT / ".env"
        if own.is_file():
            for name, value in dotenv_values(own).items():
                if name in KEY_NAMES and value:
                    sources.append((f".env {name}  [{own}]", value.strip()))
    except ImportError:
        pass
    # Older Sage versions loaded the first .env found walking up from app.py or
    # utils\ -- list any such stray files so they can be deleted.
    for folder in {ROOT / "utils", *ROOT.parents}:
        stray = folder / ".env"
        if stray.is_file():
            print(
                f"   NOTE: stray {stray} (not loaded by this Sage version; older versions could) "
                f"- delete it"
            )
    return sources


def load_key():
    """The key Sage itself would use (first source in Sage's order)."""
    sources = key_sources()
    print("   Key sources found (fingerprints only):")
    if not sources:
        print("     none")
    fingerprints = {key_fingerprint(v) for _, v in sources}
    for i, (label, value) in enumerate(sources):
        mark = "  <- Sage uses this one" if i == 0 else ""
        print(f"     {key_fingerprint(value)}  {label}{mark}")
    if len(fingerprints) > 1:
        print(
            "   WARNING: different keys are configured. Keep ONE (the working one) and delete "
            "the others."
        )
    print()
    return sources[0][1] if sources else None


def main() -> int:
    print("Sage OpenAI / ChromaDB check\n")
    ok_all = True

    # 1) Key
    try:
        key = load_key()
    except Exception as exc:  # noqa: BLE001 - diagnostic tool
        report("1. Load API key", False, why(exc))
        return 1
    if not key:
        report(
            "1. Load API key",
            False,
            "No key found (.env, .streamlit/secrets.toml or OPENAI_API_KEY).",
        )
        return 1
    others = [
        n
        for n in ("OPENAI_API_KEY", "KEY", "CHROMA_OPENAI_API_KEY")
        if os.environ.get(n) and os.environ.get(n) != key
    ]
    report(
        "1. Load API key",
        True,
        f"key loaded (length {len(key)}, fingerprint {key_fingerprint(key)})"
        + (
            f"; NOTE: different keys also set in: {', '.join(others)} - remove old ones"
            if others
            else ""
        ),
    )

    from openai import OpenAI

    client = OpenAI(api_key=key, timeout=30.0, max_retries=0)

    # 2) Responses (what answers use)
    try:
        reply = client.responses.create(
            model=CHAT_MODEL,
            input="Reply with the single word OK.",
            max_output_tokens=16,
            store=False,
        )
        ok_all &= report(
            f"2. Responses API ({CHAT_MODEL})",
            True,
            f"reply: {(getattr(reply, 'output_text', '') or '').strip()[:40]!r}",
        )
    except Exception as exc:  # noqa: BLE001
        ok_all &= report(f"2. Responses API ({CHAT_MODEL})", False, why(exc))

    # 3) Embeddings (what semantic search uses)
    vector = None
    try:
        result = client.embeddings.create(model=EMBEDDING_MODEL, input=[QUESTION])
        vector = list(result.data[0].embedding)
        ok_all &= report(
            f"3. Embeddings API ({EMBEDDING_MODEL})", True, f"vector length {len(vector)}"
        )
    except Exception as exc:  # noqa: BLE001
        ok_all &= report(f"3. Embeddings API ({EMBEDDING_MODEL})", False, why(exc))

    # 4) ChromaDB itself (your test idea, with fixed vectors: no key, no model download)
    try:
        import chromadb
        from chromadb.config import Settings

        # ignore_cleanup_errors: on Windows ChromaDB keeps its files open until exit,
        # so deleting the temporary folder can fail even though the test passed.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            test = chromadb.PersistentClient(
                path=folder, settings=Settings(anonymized_telemetry=False)
            )
            col = test.get_or_create_collection("singstat_test")
            col.upsert(
                ids=["M810811_2025"],
                documents=["Singapore's total population in 2025 was 6,111,175."],
                metadatas=[{"table_id": "M810811", "year": 2025, "value": 6111175}],
                embeddings=[[0.1, 0.2, 0.3]],
            )
            got = col.get(ids=["M810811_2025"])
            hit = col.query(query_embeddings=[[0.1, 0.2, 0.31]], n_results=1)
            ok_all &= report(
                f"4. ChromaDB {chromadb.__version__} basic read/write",
                True,
                f"get -> {got['ids']}, query -> {hit['ids'][0]}",
            )
    except Exception as exc:  # noqa: BLE001
        ok_all &= report("4. ChromaDB basic read/write", False, why(exc))

    # 5) The real Sage index, searched the way Sage now searches it (by vector)
    index = Path(os.getenv("SAGE_CHROMA_DIR", "data/chroma_singstat"))
    if not (index / "chroma.sqlite3").is_file():
        report("5. Sage index", False, f"not found at {index}")
        return 1
    try:
        real = chromadb.PersistentClient(
            path=str(index), settings=Settings(anonymized_telemetry=False)
        )
        col = real.get_collection(COLLECTION)
        count = col.count()
        detail = f"{count:,} records"
        if vector is not None:
            hit = col.query(query_embeddings=[vector], n_results=3, where={"type": "table"})
            detail += f"; top tables for the test question: {hit['ids'][0]}"
        else:
            detail += "; search skipped because step 3 failed"
        ok_all &= report("5. Sage ChromaDB index", True, detail)
    except Exception as exc:  # noqa: BLE001
        ok_all &= report("5. Sage ChromaDB index", False, why(exc))

    print("\nAll checks passed." if ok_all else "\nSome checks failed - see the reasons above.")
    print(
        f"Compare fingerprint {key_fingerprint(key)} with Sage > Show technical details > Token "
        f"usage:"
        "\nif they differ, the deployed app uses a different key."
    )
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
