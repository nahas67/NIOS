"""Typed units for Trading-R1 compute outputs.

They are annotations rather than unit-conversion objects: pydantic validates
the carrier type and the field name carries the meaning.  Keeping them
``Annotated`` preserves the donor's schema titles without imposing constraints
the harvested math might legitimately violate (a correlation can be NaN, a
return can be negative, a price can be zero after a corporate action).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

__all__ = [
    "Code",
    "Count",
    "Day",
    "Exact",
    "Fraction",
    "Money",
    "Multiple",
    "Number",
    "Price",
    "Text",
]

Number = Annotated[float, Field(title="Number", description="A dimensionless real number.")]
Price = Annotated[float, Field(title="Price", description="A price or price level.")]
Money = Annotated[float, Field(title="Money", description="A monetary amount.")]
Multiple = Annotated[float, Field(title="Multiple", description="A valuation multiple or ratio.")]
Fraction = Annotated[
    float, Field(title="Fraction", description="A decimal fraction (0.05 is five percent).")
]
Exact = Annotated[float, Field(title="Exact", description="An exactly-solved real number.")]
Count = Annotated[int, Field(title="Count", ge=0, description="A non-negative count.")]
Code = Annotated[str, Field(title="Code", description="A short machine-readable code.")]
Text = Annotated[str, Field(title="Text", description="Free-form human-readable text.")]
Day = Annotated[str, Field(title="Day", description="A calendar day in YYYY-MM-DD form.")]
