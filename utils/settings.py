"""Reading on/off settings from the environment, the same way everywhere.

Two kinds of switch, on purpose:

* env_flag(name, default)  - ordinary features. "1/true/yes/on" switch on,
  "0/false/no/off" switch off; anything else (unset, empty, a typo) keeps the
  default, so a typo never silently changes behaviour.
* env_opt_in(name)        - switches that RELAX a safeguard (unlimited budgets,
  any signed-in user, shared admin password, debug files). Only the exact
  value "1" turns them on; "true", "yes" or a typo leave the safeguard in place.

Integer settings use utils.session_security.bounded_int_env (clamped, typo-safe).
The full list of settings is in .env.example.

Used by: ui/config.py, ui/core_loader.py, utils/ai_discovery.py,
utils/core_secure.py, utils/kb.py, utils/kb_discovery_bridge.py,
utils/official_source.py, utils/privacy.py, utils/rag_engine.py,
utils/security_selfcheck.py, utils/token_usage.py.
Tested in: tests/test_settings_documented.py.
"""

import os
from collections.abc import Mapping

_ON = frozenset({"1", "true", "yes", "on"})
_OFF = frozenset({"0", "false", "no", "off"})


def env_flag(name: str, default: bool, env: Mapping[str, str] | None = None) -> bool:
    """An ordinary on/off feature setting (see the module docstring)."""
    value = str((os.environ if env is None else env).get(name) or "").strip().lower()
    if value in _ON:
        return True
    if value in _OFF:
        return False
    return default


def env_opt_in(name: str, env: Mapping[str, str] | None = None) -> bool:
    """A safeguard-relaxing switch: on ONLY for the exact value "1"."""
    return str((os.environ if env is None else env).get(name) or "").strip() == "1"
