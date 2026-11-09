"""Upstream namespace aliases for harvested Ring 2 donor code.

The donor harvest kept each upstream file verbatim, so their absolute imports
still name the upstream repository layout:

    src.factors.*          -> ring2_brain/quant/vibe_trading/factors
    src.quantlib.*         -> ring2_brain/quant/vibe_trading/quantlib
    src.agent.*            -> ring2_brain/truth/vibe_trading
    backtest.*             -> ring2_brain/quant/vibe_trading
    trading_r1.*           -> ring2_brain/quant/trading_r1 + truth/trading_r1
    tradingagents.*        -> ring2_brain/cognition/tradingagents + truth/tradingagents

Rather than rewriting 900+ imports (and losing the ability to diff the harvest
against upstream), this module builds synthetic package objects whose
``__path__`` merge the two harvested roots.  The donor files are then imported
under their original names and execute unchanged.

The installer is idempotent and only touches ``sys.modules``; it never imports
Ring 0, a broker adapter, or a credential store.
"""

from __future__ import annotations

import sys
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType
from typing import Iterable

__all__ = [
    "install_donor_aliases",
    "DONOR_ROOTS",
]

_RING2 = Path(__file__).resolve().parent
_QUANT_VIBE = _RING2 / "quant" / "vibe_trading"
_TRUTH_VIBE = _RING2 / "truth" / "vibe_trading"
_QUANT_R1 = _RING2 / "quant" / "trading_r1"
_TRUTH_R1 = _RING2 / "truth" / "trading_r1"
_COGNITION_TA = _RING2 / "cognition" / "tradingagents"
_TRUTH_TA = _RING2 / "truth" / "tradingagents"

#: Human-readable map used by the activation report and tests.
DONOR_ROOTS: dict[str, tuple[Path, ...]] = {
    "src": (_QUANT_VIBE, _TRUTH_VIBE),
    "backtest": (_QUANT_VIBE,),
    # Phase 2 Step 3 exposes the harvested data plane under its NIOS package
    # name as well, so a caller may import ``vibe_trading.loaders.*`` without
    # knowing the upstream ``backtest.*`` spelling.
    "vibe_trading": (_QUANT_VIBE,),
    "trading_r1": (_QUANT_R1, _TRUTH_R1),
    "tradingagents": (_COGNITION_TA, _TRUTH_TA),
}

_INSTALLED = False


def _merge_path(module: ModuleType, paths: Iterable[Path]) -> None:
    current = list(getattr(module, "__path__", ()) or ())
    for path in paths:
        text = str(path)
        if path.exists() and text not in current:
            current.append(text)
    module.__path__ = current
    module.__package__ = module.__name__
    spec = getattr(module, "__spec__", None)
    if spec is None:
        spec = ModuleSpec(module.__name__, loader=None, is_package=True)
        module.__spec__ = spec
    spec.submodule_search_locations = current


def _ensure_package(
    name: str,
    paths: Iterable[Path],
    *,
    attrs: dict[str, object] | None = None,
) -> ModuleType:
    """Return a synthetic package whose search path is ``paths`` (merged)."""
    module = sys.modules.get(name)
    if module is None:
        module = ModuleType(name)
        module.__file__ = None  # a namespace bridge, not a source file
        sys.modules[name] = module
    _merge_path(module, paths)
    for key, value in (attrs or {}).items():
        setattr(module, key, value)
    return module


def install_donor_aliases() -> bool:
    """Install the upstream aliases.  Returns ``True`` on first installation."""
    global _INSTALLED
    if _INSTALLED:
        return False
    _INSTALLED = True

    # ``src`` merges the quant and truth harvests.  ``src.agent`` lives only in
    # the truth tree, while ``src.factors`` / ``src.quantlib`` / ``src.config``
    # live only in the quant tree, so a single merged root resolves both.
    _ensure_package("src", (_QUANT_VIBE, _TRUTH_VIBE))
    _ensure_package("src.agent", (_TRUTH_VIBE,))

    # The backtest engine tree is the quant root (engines/, loaders/).
    _ensure_package("backtest", (_QUANT_VIBE,))

    # NIOS-native spelling of the same harvested data plane.
    _ensure_package("vibe_trading", (_QUANT_VIBE,))

    # Trading-R1 has a compute tree in quant and a verification tree in truth.
    _ensure_package("trading_r1", (_QUANT_R1, _TRUTH_R1))
    # Upstream spells the compute package ``trading_r1.tools.compute``.
    _ensure_package("trading_r1.tools", (_QUANT_R1,))

    # TradingAgents: agents/graph in cognition, vendor truth bridges in truth.
    _ensure_package(
        "tradingagents",
        (_COGNITION_TA, _TRUTH_TA),
        attrs={"__version__": "0.2.4"},
    )

    return True
