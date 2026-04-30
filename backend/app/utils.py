"""
Shared utility helpers for PGMRec.

# TODO: Future improvement: migrate to timezone-aware datetimes using UTC everywhere.
#       For now we standardize on naive UTC to avoid offset-aware vs offset-naive
#       comparison errors (the SQLite DateTime column always stores naive values).
"""
from __future__ import annotations

from datetime import datetime


def utc_now() -> datetime:
    """Return the current time as a naive UTC datetime.

    Prefer this helper over ``datetime.now(timezone.utc)`` throughout the
    codebase so that all timestamps are consistently naive UTC and can be
    safely compared with values read back from the SQLite DateTime columns
    (which are always stored and returned as naive datetimes).
    """
    return datetime.utcnow()
