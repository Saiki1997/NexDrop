"""NEXDROP patch (3.1.1): take dates out of file and folder names.

"2021-06-10-PIYO-118-I-Want-My" -> "PIYO-118-I-Want-My", "2020 06 11 [EMOI-014] Shy" ->
"[EMOI-014] Shy", "(2022-07-12) - (1080p)" -> "(1080p)", "Beach - 2021.07.02 - Day 2" -> "Beach - Day 2".

Only whole calendar dates are removed: year, month and day with the same separator between them
("2021-07-02", "2021 07 02", "2021.07.02", "2021_07_02"), or day, month, year ("02.07.2021"), or the
same in brackets. Other numbers (ids, "1080p", "IMG_20210702") are left alone. When nothing worth
keeping is left, the name is returned unchanged.
"""

from __future__ import annotations

import re

_YMD = r"(?:19|20)\d{2}(?P<s1>[-_. ])(?:0[1-9]|1[0-2])(?P=s1)(?:0[1-9]|[12]\d|3[01])"
_DMY = r"(?:0[1-9]|[12]\d|3[01])(?P<s2>[-_. ])(?:0[1-9]|1[0-2])(?P=s2)(?:19|20)\d{2}"
_DATE = rf"(?:{_YMD}|{_DMY})"
_BRACKETED = re.compile(rf"[(\[]\s*{_DATE}\s*[)\]]")
_BARE = re.compile(rf"(?<![0-9A-Za-z]){_DATE}(?![0-9A-Za-z])")
_SEPARATOR_RUN = re.compile(r"\s*[-_.]\s*(?:[-_.]\s*)+")
_EDGES = " -_.,;~"


def _tidy_run(match: re.Match) -> str:
    text = match.group(0)
    return " - " if " " in text else text.strip()[0]


def strip_dates(name: str) -> str:
    """`name` (a file name without its extension, or a folder name) without the dates in it."""
    if not name:
        return name
    out = _BRACKETED.sub(" ", name)
    out = _BARE.sub(" ", out)
    if out == name:
        return name
    out = _SEPARATOR_RUN.sub(_tidy_run, out)
    out = re.sub(r"\s{2,}", " ", out).strip(_EDGES)
    out = re.sub(r"[(\[]\s*[)\]]", "", out).strip(_EDGES)  # brackets left empty
    return out if sum(ch.isalnum() for ch in out) >= 2 else name
