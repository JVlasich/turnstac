"""Shared bits of the pre-processing tools. No opals import, so c_copc stays standalone."""

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

_BIN = Path(__file__).resolve().parents[1] / "bin"   # turnstac/pre/<mod> -> turnstac/bin
_COPCINDEX = "lascopcindex64" + (".exe" if os.name == "nt" else "") # linux inclusive :) not tested tho


def resolve_inputs(raw, suffixes: tuple, skip: str = "") -> list:
    """Expand a path or list of paths/dirs into a deduped list of input files.

    Args:
        raw: one path string or an iterable of paths; a dir is scanned, a file taken as is
        suffixes: accepted lowercase suffixes, e.g. (".tif", ".tiff")
        skip: lowercase filename ending marking already-converted output, skipped in dir scans
    Returns:
        List of resolved Paths, dirs sorted by name, input order kept, duplicates dropped.
    """
    entries = [raw] if isinstance(raw, str) else list(raw)
    resolved = []
    seen = set()
    for entry in entries:
        p = Path(entry).resolve()
        if p.is_dir():
            hits = sorted(f for f in p.iterdir()
                          if f.is_file() and f.suffix.lower() in suffixes
                          and not (skip and f.name.lower().endswith(skip)))
        elif p.exists():
            hits = [p]
        else:
            raise FileNotFoundError(f"Input path not found: {p}")
        for f in hits:
            if f not in seen:
                seen.add(f)
                resolved.append(f)

    if not resolved:
        raise Exception(f"No {'/'.join(suffixes)} inputs resolved from --infile {entries}")
    return resolved


def beep():
    """Windows beep on completion, no-op elsewhere."""
    if os.name == "nt":
        import winsound
        winsound.MessageBeep()
