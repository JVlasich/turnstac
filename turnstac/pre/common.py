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


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        for name in ("a.laz", "b.LAZ", "b.copc.laz", "c.txt"):
            (d / name).touch()
        got = [f.name for f in resolve_inputs(str(d), (".laz",), ".copc.laz")]
        assert got == ["a.laz", "b.LAZ"], got

        for name in ("r.tif", "r_cog.tif", "s.tiff"):
            (d / name).touch()
        got = [f.name for f in resolve_inputs([str(d / "r.tif"), str(d)], (".tif", ".tiff"), "_cog.tif")]
        assert got == ["r.tif", "s.tiff"], got          # explicit file first, no duplicate from the scan

        try:
            resolve_inputs(str(d / "nope.tif"), (".tif",))
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("missing path did not raise")

        try:
            resolve_inputs(str(d), (".las",))
        except Exception as e:
            assert "No .las inputs" in str(e), e
    print("common self-check ok")
