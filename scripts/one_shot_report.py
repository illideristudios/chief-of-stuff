"""A one-shot report file, as the launcher writes it and `result` reads it: the size bound and the reader."""

from __future__ import annotations

from pathlib import Path

from _vendor.toon_format import ToonDecodeError, decode as toon_decode

LIMIT = 1 << 20  # a report is a few lines; the cap bounds what a runaway writer could leave
FIELD_MAX = LIMIT // 16  # characters of `reason` or `changes` the launcher keeps: two fields, 6 bytes a character, stay under LIMIT
# What reading a file that is not a report can raise: a link, a directory or an unreadable file (OSError), invalid UTF-8,
# not a report or not TOON (ValueError, ToonDecodeError), or TOON nested past the decoder's depth (RecursionError).
UNREADABLE = (OSError, ValueError, ToonDecodeError, RecursionError)


def read_report(path: Path, read) -> dict:
    """The report at `path`, or one of UNREADABLE when it is a link, too big, not UTF-8 TOON, or not the launcher's shape.

    `read` is the driver's file read (`git_trees.read_regular`): this module stays inside the adapter circle.
    """
    raw = read(path, LIMIT + 1, nofollow=True)
    if raw is None or len(raw) > LIMIT:
        raise ValueError("not a report")
    data = toon_decode(raw.decode())
    if not (isinstance(data, dict) and all(isinstance(data.get(k), str) for k in ("task", "worker", "status"))):
        raise ValueError("not a report")
    return data
