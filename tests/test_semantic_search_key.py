"""Semantic search must use Sage's current key (audit showed 'AuthenticationError')."""

import sys, types, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# Import these BEFORE any test patches sys.modules. mock.patch.dict(sys.modules, ...)
# restores the module table exactly, removing modules first imported during a test; a
# later re-import then creates a second copy, and a label written to one copy is not
# seen in the other. (This only showed when this file ran on its own, e.g. in CI.)
from utils import credentials, kb, token_usage  # noqa: E402,F401


class FakeOpenAI:
    created = []

    def __init__(self, api_key=None, **kwargs):
        self.api_key = api_key
        FakeOpenAI.created.append(api_key)
        owner = self

        class Embeddings:
            def create(self, model, input):
                owner.last_model = model
                return types.SimpleNamespace(
                    data=[types.SimpleNamespace(embedding=[0.1, 0.2, 0.3])],
                    usage={"total_tokens": 5},
                )

        self.embeddings = Embeddings()
        self.responses = types.SimpleNamespace(create=lambda **k: None)
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=lambda **k: None)
        )


class EmbedQuery(unittest.TestCase):
    def setUp(self):
        from utils import kb

        self.kb = kb
        kb._EMBED_STATE.update(client=None, key_digest=None)
        FakeOpenAI.created.clear()
        self.patch = mock.patch.dict(
            sys.modules, {"openai": types.SimpleNamespace(OpenAI=FakeOpenAI)}
        )
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.kb._EMBED_STATE.update(client=None, key_digest=None)

    def test_uses_the_current_in_memory_key_and_rebuilds_after_rotation(self):
        with mock.patch("utils.credentials.get_openai_api_key", return_value="fake-current-key"):
            self.assertEqual(self.kb.embed_query("residents by age"), [0.1, 0.2, 0.3])
            self.kb.embed_query("again")  # same key: client reused
        with mock.patch("utils.credentials.get_openai_api_key", return_value="fake-rotated-key"):
            self.kb.embed_query("after rotation")
        self.assertEqual(FakeOpenAI.created, ["fake-current-key", "fake-rotated-key"])

    def test_missing_key_fails_clearly(self):
        from utils.errors import CredentialError

        with mock.patch("utils.credentials.get_openai_api_key", return_value=None):
            with self.assertRaises(CredentialError):
                self.kb.embed_query("x")


class VectorQuery(unittest.TestCase):
    def test_chroma_receives_a_vector_not_text(self):
        from utils import hybrid_discovery as hd

        calls = []
        chroma = types.SimpleNamespace(
            col=types.SimpleNamespace(
                query=lambda **kw: calls.append(kw) or {"ids": [["t:M810011"]]}
            )
        )
        self.assertEqual(hd._vector_ids(chroma, "q", "table", 5, [0.1, 0.2]), ["t:M810011"])
        self.assertEqual(calls[0]["query_embeddings"], [[0.1, 0.2]])
        self.assertNotIn("query_texts", calls[0])

    def test_question_is_embedded_once_for_both_lookups(self):
        # Compare with whitespace collapsed, so line wrapping cannot break the check.
        src = " ".join((ROOT / "utils" / "hybrid_discovery.py").read_text(encoding="utf-8").split())
        self.assertEqual(src.count("_in_context(kb.embed_query, question)"), 1)
        self.assertIn("_in_context(_vector_ids, chroma, question, kind, 60, query_embedding)", src)


if __name__ == "__main__":
    unittest.main()


class ReadableReason(unittest.TestCase):
    def test_missing_scope_and_masked_key_messages(self):
        from utils.hybrid_discovery import _error_summary

        AuthenticationError = type("AuthenticationError", (Exception,), {})
        scope = _error_summary(
            AuthenticationError(
                "Error code: 401 - {'error': {'message': 'You have insufficient permissions for this "
                "operation. Missing scopes: model.request. Check that you have the correct role'}}"
            )
        )
        self.assertEqual(
            scope,
            "AuthenticationError - API key lacks permission for embeddings "
            "(missing scope: model.request)",
        )
        masked = _error_summary(
            AuthenticationError("Incorrect API key provided: " "sk-proj-abcd******WXYZ.")
        )
        self.assertEqual(masked, "AuthenticationError - API key rejected (incorrect or revoked)")

    def test_masked_keys_are_fully_redacted(self):
        from utils.redaction import redact_diagnostic

        out = redact_diagnostic(
            "Incorrect API key provided: sk-proj-abcd****************WXYZ. See " "docs."
        )
        self.assertNotIn("abcd", out)
        self.assertNotIn("WXYZ", out)
        self.assertIn("[REDACTED_KEY]", out)


class Fingerprint(unittest.TestCase):
    def test_stable_short_and_never_the_key(self):
        from utils.credentials import key_fingerprint

        fp = key_fingerprint("fake-current-key")
        self.assertEqual(fp, key_fingerprint("fake-current-key"))
        self.assertEqual(len(fp), 8)
        self.assertNotEqual(fp, key_fingerprint("fake-rotated-key"))
        self.assertNotIn("fake", fp)
        with mock.patch("utils.credentials.get_openai_api_key", return_value=None):
            self.assertEqual(key_fingerprint(), "none")


class OneKeyPerProcess(unittest.TestCase):
    """Answers worked but semantic search was rejected: two keys were in use."""

    def setUp(self):
        from utils import credentials

        self.c = credentials
        credentials._reset_for_tests()
        credentials._SOURCE_STATE.update(sources=[], chosen=None, warned=set())
        self.env = mock.patch.dict("os.environ", {}, clear=False)
        self.env.start()
        for name in ("OPENAI_API_KEY", "KEY"):
            __import__("os").environ.pop(name, None)

    def tearDown(self):
        self.env.stop()
        self.c._reset_for_tests()

    def test_key_is_not_replaced_after_it_is_loaded(self):
        good = "sk-proj-" + "G" * 48  # test fixture  check-secrets: allow
        stale = "sk-proj-" + "S" * 48  # test fixture  check-secrets: allow
        self.assertEqual(self.c.load_openai_api_key({"OPENAI_API_KEY": good}), good)
        # Later, an older secret shows up again (e.g. republished into the environment).
        self.assertEqual(self.c.load_openai_api_key({"OPENAI_API_KEY": stale}), good)
        self.assertEqual(self.c.get_openai_api_key(), good)

    def test_conflicting_keys_are_reported_by_fingerprint_only(self):
        good = "sk-proj-" + "G" * 48  # test fixture  check-secrets: allow
        stale = "sk-proj-" + "S" * 48  # test fixture  check-secrets: allow
        __import__("os").environ["OPENAI_API_KEY"] = good
        with self.assertLogs(self.c.LOG, level="WARNING") as logs:
            self.c.load_openai_api_key({"OPENAI_API_KEY": stale})
        text = " ".join(logs.output)
        self.assertIn("Different OpenAI keys are configured", text)
        self.assertIn(self.c.key_fingerprint(good), text)
        self.assertNotIn(good, text)
        self.assertNotIn(stale, text)
        warning = self.c.conflicting_keys_warning()  # shown on the page
        self.assertIn("Other OpenAI keys are also configured", warning)
        self.assertNotIn(good, warning)
        self.assertNotIn(stale, warning)
        self.assertNotIn(self.c.key_fingerprint(good), warning)  # no fingerprint on the page

    def test_no_warning_when_only_one_key_is_configured(self):
        # test fixture  check-secrets: allow
        __import__("os").environ["OPENAI_API_KEY"] = "sk-proj-" + "G" * 48
        self.c.load_openai_api_key({})
        self.assertIsNone(self.c.conflicting_keys_warning())


class OnlyOwnDotenv(unittest.TestCase):
    """A stray .env in utils\\ or a parent folder supplied an old KEY."""

    def test_every_load_dotenv_call_names_the_sage_folder(self):
        import re

        # The page loads .env in ui/startup.py; the core in utils/core_config.py.
        for rel in ("ui/startup.py", "utils/core_config.py"):
            lines = [
                line.split("#", 1)[0]
                for line in (ROOT / rel).read_text(encoding="utf-8").splitlines()
            ]
            calls = [line for line in lines if "load_dotenv(" in line and "import" not in line]
            self.assertTrue(calls, rel)
            for line in calls:
                self.assertIn('.env"', line, f"{rel}: load_dotenv() without an explicit path")


class LabelledAndReused(unittest.TestCase):
    """Usage report showed 'Unlabelled call' twice per question for the same text."""

    def setUp(self):
        from utils import kb, token_usage

        self.kb, self.tu = kb, token_usage
        kb._EMBED_STATE.update(client=None, key_digest=None)
        kb._VECTOR_CACHE.clear()
        self.seen = []
        tu = token_usage

        class Recording(FakeOpenAI):
            def __init__(inner, api_key=None, **kw):
                super().__init__(api_key, **kw)
                original = inner.embeddings.create

                def create(model, input):
                    self.seen.append(tu._OPERATION.get())
                    return original(model=model, input=input)

                inner.embeddings.create = create

        self.patch = mock.patch.dict(
            sys.modules, {"openai": types.SimpleNamespace(OpenAI=Recording)}
        )
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.kb._VECTOR_CACHE.clear()
        self.kb._EMBED_STATE.update(client=None, key_digest=None)

    def test_semantic_search_call_is_labelled_and_reused(self):
        with mock.patch("utils.credentials.get_openai_api_key", return_value="fake-current-key"):
            first = self.kb.embed_query("How has GFCF changed?")
            second = self.kb.embed_query("How has GFCF changed?")
        self.assertEqual(first, second)
        self.assertEqual(len(self.seen), 1)  # second came from memory
        self.assertEqual(self.seen[0][0], "Semantic search")

    def test_core_embedding_calls_are_labelled(self):
        # The core's embedding calls live in utils/rag_engine.py (split out of core_secure.py).
        # Layout-proof: optional type hint, any spacing or line breaks.
        src = " ".join((ROOT / "utils" / "rag_engine.py").read_text(encoding="utf-8").split())
        self.assertRegex(src, r'stage(?:: str)? ?= ?"Reviewed notes search"')
        self.assertRegex(src, r'stage(?:: str)? ?= ?"Index reviewed notes"')
        self.assertIn("with api_operation(stage, purpose):", src)
