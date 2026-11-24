"""The pure parts of scripts/housekeeping.py: reading what the package tools
print, and choosing the cargo build output a clean would delete.

The script is not part of the package; it is loaded from its file.
"""

import importlib.util
import os
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "housekeeping.py"
_spec = importlib.util.spec_from_file_location("housekeeping", SCRIPT)
housekeeping = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = housekeeping
_spec.loader.exec_module(housekeeping)

INSTALLED = 1_800_000_000.0  # when the active toolchain was installed
OLD = INSTALLED - 10 * 86400
NEW = INSTALLED + 86400


def test_reads_cargo_update_as_cargo_1_100_prints_it():
    verbose = """\
    Updating crates.io index
     Locking 2 packages to highest Rust 1.96 compatible versions
    Updating itoa v1.0.18 -> v1.0.19
    Updating toml v1.1.6+spec-1.1.0 -> v1.2.2+spec-1.1.0 (available: v2.0.0)
   Unchanged rand v0.8.8 (available: v0.10.4)
note: to see how you depend on a package, run `cargo tree --invert <dep>@<ver>`
warning: not updating lockfile due to dry run
"""
    found = housekeeping.parse_cargo_update(verbose)
    assert found.newer == ["itoa", "toml"]
    assert found.behind == ["toml", "rand"]
    quiet = "note: pass `--verbose` to see 2 unchanged dependencies behind latest\n"
    assert housekeeping.parse_cargo_update(quiet).behind_unnamed == 2


def test_reads_uv_lock_rustup_check_and_tmutil():
    uv = "Resolved 48 packages in 830ms\nUpdate anyio v4.14.0 -> v4.16.0\nRemove six v1.17.0\nAdd tzdata v2026.6\n"
    found = housekeeping.parse_uv_lock(uv)
    assert (found.updated, found.added, found.removed) == (["anyio"], ["tzdata"], ["six"])
    rustup = (
        "stable-aarch64-apple-darwin - Update available : 1.99.1 (abc 2026-10-01) -> 1.100.0 (def 2026-11-09)\n"
        "rustup - up to date : 1.29.3\n"
    )
    assert housekeeping.parse_rustup_check(rustup) == [
        ("stable-aarch64-apple-darwin", True, "1.99.1 -> 1.100.0"),
        ("rustup", False, "1.29.3"),
    ]
    tmutil = "Snapshots for disk /System/Volumes/Data:\ncom.apple.TimeMachine.2026-11-24-101500.local\n"
    assert housekeeping.parse_tmutil_snapshots(tmutil) == ["com.apple.TimeMachine.2026-11-24-101500.local"]
    assert housekeeping.parse_tmutil_snapshots("Snapshots for disk /:\n") == []


def _put(root: Path, relative: str, when: float) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 100)
    os.utime(path, (when, when))
    return path


def _build_folder(root: Path) -> Path:
    target = root / "target"
    _put(target, "debug/.cargo-lock", OLD)
    _put(target, "debug/build/serde/aaaa1111bbbb2222/out/old.rmeta", OLD)
    _put(target, "debug/build/serde/cccc3333dddd4444/out/new.rmeta", NEW)
    _put(target, "debug/build/mixed/1234123412341234/out/old.o", OLD)
    _put(target, "debug/build/mixed/1234123412341234/out/new.o", NEW)
    _put(target, "debug/build/libc-0123456789abcdef/output", OLD)
    _put(target, "debug/deps/libold-0123456789abcdef.rlib", OLD)
    _put(target, "debug/incremental/foo-1abc2def3/s-old/work.bin", OLD)
    _put(target, "notes/left-here.txt", OLD)  # outside a profile folder
    outside = _put(root, "outside/precious.txt", OLD)
    os.symlink(outside, target / "debug/build/serde/aaaa1111bbbb2222/out/escape")
    os.symlink(outside.parent, target / "debug/deps/linked")
    return target


def _chosen(scan, unused_since=None):
    return {
        os.path.relpath(found.unit.path, scan.folder): found.reason
        for found in housekeeping.select_stale(scan.units, INSTALLED, unused_since)
    }


def test_chooses_whole_units_built_before_the_toolchain(tmp_path):
    scan = housekeeping.scan_build_folder(_build_folder(tmp_path))
    assert _chosen(scan) == {
        "debug/build/serde/aaaa1111bbbb2222": "toolchain",
        "debug/build/libc-0123456789abcdef": "toolchain",
        "debug/deps/libold-0123456789abcdef.rlib": "toolchain",
        "debug/incremental/foo-1abc2def3": "toolchain",
    }
    later = _chosen(scan, unused_since=NEW + 1)
    assert later["debug/build/serde/cccc3333dddd4444"] == "unused"
    assert "debug/build/mixed/1234123412341234" in later


def test_delete_keeps_links_and_units_that_changed(tmp_path):
    target = _build_folder(tmp_path)
    scan = housekeeping.scan_build_folder(target)
    found = {os.path.relpath(c.unit.path, scan.folder): c for c in housekeeping.select_stale(scan.units, INSTALLED)}
    root = str(scan.folder)

    serde = found["debug/build/serde/aaaa1111bbbb2222"]
    assert housekeeping.delete(serde, root) == (1, 0, 0)  # the link stays, so its folder does
    assert (tmp_path / "outside/precious.txt").exists()
    assert (target / "debug/build/serde/aaaa1111bbbb2222/out/escape").is_symlink()

    libc = found["debug/build/libc-0123456789abcdef"]
    _put(target, "debug/build/libc-0123456789abcdef/output-new", NEW)  # a build wrote here since the listing
    assert housekeeping.delete(libc, root) is None
    assert (target / "debug/build/libc-0123456789abcdef/output").exists()

    incremental = found["debug/incremental/foo-1abc2def3"]
    assert housekeeping.delete(incremental, root) == (1, 0, 2)
    assert not (target / "debug/incremental/foo-1abc2def3").exists()
    assert (target / "notes/left-here.txt").exists()
