"""Trading-R1 tool contract surface.

The harvest references ``trading_r1.contract.spec`` and
``trading_r1.contract.units`` but upstream keeps them outside the harvested
``compute`` subtree.  These are the real contracts: a pydantic request/output
pair, an immutable provenance record, a ``Result`` envelope, and typed units.
"""

from __future__ import annotations

from .spec import (
    Output,
    Provenance,
    Result,
    ToolError,
    ToolSpec,
    hint,
    rendered,
)
from .units import (
    Code,
    Count,
    Day,
    Exact,
    Fraction,
    Money,
    Multiple,
    Number,
    Price,
    Text,
)

__all__ = [
    "Code",
    "Count",
    "Day",
    "Exact",
    "Fraction",
    "Money",
    "Multiple",
    "Number",
    "Output",
    "Price",
    "Provenance",
    "Result",
    "Text",
    "ToolError",
    "ToolSpec",
    "hint",
    "rendered",
]
