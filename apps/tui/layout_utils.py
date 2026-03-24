#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MascotLayout:
    variant: str  # full | tiny | hidden
    max_lines: int


def choose_mascot_layout(term_width: int, term_height: int) -> MascotLayout:
    """Choose mascot rendering mode from terminal dimensions.

    full: normal sprite
    tiny: downscaled sprite for constrained space
    hidden: no sprite when terminal is too small
    """
    width = max(0, int(term_width))
    height = max(0, int(term_height))

    # keep enough room for header, footer, chat history, and input
    if width < 78 or height < 18:
        return MascotLayout(variant='hidden', max_lines=0)
    if width < 112 or height < 30:
        return MascotLayout(variant='tiny', max_lines=max(4, height - 10))
    return MascotLayout(variant='full', max_lines=max(8, height - 10))
