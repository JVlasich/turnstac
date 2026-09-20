"""tac_pcl cleanup: a run only ever empties its own scratch directory."""
import sys

import pytest

pytest.importorskip("opals")  # tac_pcl imports opals at module load

from turnstac.pre import tac_pcl


def test_cleanup_spares_the_tmp_root(tmp_path, monkeypatch):
    """--no-keepodm cleans the run scratch dir; anything already in --tmp_path survives."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    sentinel = scratch / "keep_me.txt"
    sentinel.write_text("not mine to delete", encoding="utf-8")

    broken = tmp_path / "broken.laz"       # fails on import, the run still reaches cleanup
    broken.write_bytes(b"")

    monkeypatch.setattr(sys, "argv", [
        "tac_pcl", "--infile", str(broken), "--tmp_path", str(scratch),
        "--outdir", str(tmp_path / "out"), "--no-keepodm", "--loglevel", "none",
    ])
    with pytest.raises(SystemExit):
        tac_pcl.main()

    assert sentinel.read_text(encoding="utf-8") == "not mine to delete"
    assert sorted(p.name for p in scratch.iterdir()) == ["keep_me.txt", "tac_pcl"]
