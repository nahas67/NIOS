"""Text splitting helpers for the MiroFish ontology generator."""

from __future__ import annotations

import re
from typing import Iterable

__all__ = ["FileParser", "split_text_into_chunks"]

_DEFAULT_CHUNK = 2_000
_DEFAULT_OVERLAP = 120


def split_text_into_chunks(
    text: str,
    chunk_size: int = _DEFAULT_CHUNK,
    overlap: int = _DEFAULT_OVERLAP,
) -> list[str]:
    """Split ``text`` on paragraph boundaries with a bounded tail overlap."""
    source = str(text or "")
    if len(source) <= chunk_size:
        return [source] if source else []
    paragraphs = [part for part in re.split(r"\n\s*\n", source) if part.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= chunk_size:
            current = candidate
            continue
        if current:
            chunks.append(current)
            tail = current[-overlap:] if overlap > 0 else ""
            current = f"{tail}\n\n{paragraph}" if tail else paragraph
        else:
            step = max(1, chunk_size - overlap)
            chunks.extend(
                source[index : index + chunk_size]
                for index in range(0, len(paragraph), step)
            )
            current = ""
    if current:
        chunks.append(current)
    return chunks


class FileParser:
    """Read and split local text documents."""

    def parse(self, path: str) -> list[str]:
        from pathlib import Path

        return split_text_into_chunks(Path(path).read_text(encoding="utf-8"))

    def split(self, text: str) -> list[str]:
        return split_text_into_chunks(text)
