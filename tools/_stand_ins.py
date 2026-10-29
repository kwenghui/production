"""In-memory stand-ins so the characterisation tools run without a browser,
network or API key. Real packages, when installed, are used instead (except
Streamlit, which is always replaced: its session state needs a running app).

Used by: tools/characterise_process_question.py, tools/characterise_ask_sage.py.
"""

import sys
import types


def _install_streamlit_stand_in():
    """Register minimal 'streamlit' (and, if absent, 'openai') modules.

    Installing twice in one process keeps the first stand-in, so every module
    (and every test) shares the same st.session_state.
    """
    if getattr(sys.modules.get("streamlit"), "_sage_stand_in", False):
        return

    class SessionState(dict):
        def __getattr__(self, key):
            try:
                return self[key]
            except KeyError:
                raise AttributeError(key) from None

        def __setattr__(self, key, value):
            self[key] = value

        def __delattr__(self, key):
            del self[key]

    noop = lambda *a, **k: None
    st_mod = types.ModuleType("streamlit")
    st_mod.session_state = SessionState()
    st_mod.context = None
    st_mod.fragment = lambda f=None, **k: f if f else (lambda g: g)
    st_mod.cache_data = st_mod.cache_resource = (
        lambda *a, **k: a[0] if a and callable(a[0]) else (lambda f: f)
    )
    st_mod.__getattr__ = lambda name: noop
    errors = types.ModuleType("streamlit.errors")
    errors.StreamlitAPIException = type("StreamlitAPIException", (Exception,), {})
    st_mod._sage_stand_in = True
    sys.modules.update({"streamlit": st_mod, "streamlit.errors": errors})
    try:
        import openai  # noqa: F401
    except ImportError:
        fake = types.ModuleType("openai")
        for name in (
            "OpenAI",
            "APIError",
            "APIConnectionError",
            "APITimeoutError",
            "RateLimitError",
            "AuthenticationError",
            "PermissionDeniedError",
            "BadRequestError",
            "NotFoundError",
            "APIStatusError",
            "InternalServerError",
            "OpenAIError",
        ):
            setattr(fake, name, type(name, (Exception,), {"__init__": lambda s, *a, **k: None}))

        class OpenAI:  # the client: endpoints exist but are replaced by each tool
            def __init__(self, *a, **k):
                endpoint = types.SimpleNamespace(create=lambda **kw: None)
                self.responses, self.embeddings = endpoint, endpoint
                self.chat = types.SimpleNamespace(completions=endpoint)

        fake.OpenAI = OpenAI
        sys.modules["openai"] = fake


def _install_plotly_stand_in():
    """A do-nothing Plotly, ALWAYS: results must not depend on whether (or which)
    Plotly is installed, so a recorded result is the same on every machine."""

    class Figure:
        def __init__(self, *a, **k):
            self.data = []

        def __getattr__(self, name):
            return lambda *a, **k: None

    plotly = types.ModuleType("plotly")
    graph_objects = types.ModuleType("plotly.graph_objects")
    graph_objects.Figure = Figure
    graph_objects.__getattr__ = lambda name: (lambda *a, **k: None)
    plotly.graph_objects = graph_objects
    sys.modules.update({"plotly": plotly, "plotly.graph_objects": graph_objects})


def install_stand_ins():
    """Register the stand-ins and a dummy OpenAI key for this process only."""
    import os

    _install_streamlit_stand_in()
    _install_plotly_stand_in()
    os.environ.setdefault("OPENAI_API_KEY", "sk-test-" + "a" * 40)
