"""Test bootstrap.

The integration's pure logic (optimiser, learning, tariff parsing) is written
without Home Assistant imports so it can be tested on any Python. To import
those submodules without executing the HA-dependent package ``__init__``, we
pre-register stub parent packages that point at the real directories.
"""

from __future__ import annotations

import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _stub(name: str, path: pathlib.Path) -> None:
    if name in sys.modules:
        return
    module = types.ModuleType(name)
    module.__path__ = [str(path)]  # type: ignore[attr-defined]
    sys.modules[name] = module


_stub("custom_components", ROOT / "custom_components")
_stub("custom_components.ess_controller", ROOT / "custom_components" / "ess_controller")
