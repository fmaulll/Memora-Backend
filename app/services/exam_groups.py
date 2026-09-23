"""Shared chapter boundaries for exam content and schedule projections."""
from typing import Sequence, TypeVar

Chapter = TypeVar("Chapter")


def split_chapters(chapters: Sequence[Chapter]) -> tuple[list[Chapter], list[Chapter]]:
    midpoint = (len(chapters) + 1) // 2
    return list(chapters[:midpoint]), list(chapters[midpoint:])
