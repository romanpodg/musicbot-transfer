#!/usr/bin/env python
"""Recover files deleted from this project out of the Windows Recycle Bin.

The Recycle Bin stores each deleted file twice:

* ``$I<id>[.ext]`` - metadata: version, original size, deletion FILETIME and
  the original absolute path encoded as UTF-16LE.
* ``$R<id>``       - the original file content, unchanged.

This script parses every ``$I`` entry, keeps the ones whose original path
lives under the project root, and restores them from the matching ``$R``
payload.  It is deliberately read-only until ``--apply`` is passed.
"""

from __future__ import annotations

import argparse
import shutil
import struct
import sys
from pathlib import Path

RECYCLE_BIN = Path("C:/$Recycle.Bin")
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Layout of an $I stream (Windows 10 1809+ format, version 2):
#   0x00  u32  version
#   0x04  u32  reserved
#   0x08  i64  original file size in bytes
#   0x10  i64  deletion time as a FILETIME
#   0x18  u32  path length in UTF-16 code units, *including* the trailing NUL
#   0x1c  ..   UTF-16LE absolute path, NUL terminated
_SIZE_OFFSET = 0x08
_PATH_LENGTH_OFFSET = 0x18
_PATH_OFFSET = 0x1C


def _parse(entry: Path) -> tuple[str, int] | None:
    """Return (original_path, original_size) for an $I entry, or None."""
    try:
        raw = entry.read_bytes()
    except OSError:
        return None
    if len(raw) < _PATH_OFFSET + 2:
        return None
    size = struct.unpack_from("<q", raw, _SIZE_OFFSET)[0]
    units = struct.unpack_from("<I", raw, _PATH_LENGTH_OFFSET)[0]
    blob = raw[_PATH_OFFSET : _PATH_OFFSET + units * 2]
    if len(blob) < units * 2:
        return None
    path = blob.decode("utf-16-le", "replace").rstrip("\x00")
    return path, size


def _content_candidates(entry: Path) -> list[Path]:
    """The $R payload(s) that could belong to this $I entry.

    A deleted *directory* is recycled as a directory too, so the payload may
    be a folder holding the whole subtree rather than a single file.
    """
    stem = entry.name[2:]  # drop the leading "$I"
    without_ext = stem.split(".")[0]
    candidates = [entry.with_name(f"$R{without_ext}"), entry.with_name(f"$R{stem}")]
    return [c for c in candidates if c.is_file() or c.is_dir()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually restore the files (default: dry run)",
    )
    parser.add_argument(
        "--filter",
        default="",
        help="only consider original paths containing this text (e.g. 'tests')",
    )
    args = parser.parse_args()

    # Scanning every SID folder is cheap (a few thousand entries) and avoids
    # having to guess which SID is ours from a locale-dependent shell command.
    folders = sorted(p for p in RECYCLE_BIN.iterdir() if p.is_dir())

    recovered: list[tuple[Path, Path, int]] = []
    for folder in folders:
        if not folder.is_dir():
            continue
        for entry in folder.glob("$I*"):
            parsed = _parse(entry)
            if parsed is None:
                continue
            path, size = parsed
            normalized = path.replace("\\", "/").lower()
            if str(PROJECT_ROOT).replace("\\", "/").lower() not in normalized:
                continue
            # The virtualenv is disposable and huge; restoring it would just
            # put thousands of irrelevant files back on disk.
            if "/.venv/" in normalized:
                continue
            # Git internals and __pycache__ are noise.
            if "/.git/" in normalized or "__pycache__" in normalized:
                continue
            if args.filter and args.filter.lower() not in normalized:
                continue
            payloads = _content_candidates(entry)
            if not payloads:
                print(f"!! no payload found for {path}", file=sys.stderr)
                continue
            recovered.append((Path(path), payloads[0], size))

    if not recovered:
        print("No Recycle Bin entries match.")
        return 1

    recovered.sort()
    print(f"Found {len(recovered)} recoverable entr(y/ies):\n")
    for target, payload, size in recovered:
        kind = "dir " if payload.is_dir() else "file"
        if payload.is_dir():
            count = sum(1 for _ in payload.rglob("*") if _.is_file())
            detail = f"{kind} {count} file(s)"
        else:
            actual = payload.stat().st_size
            ok = "" if actual == size else f" <-- SIZE MISMATCH (expected {size})"
            detail = f"{kind} {actual} bytes{ok}"
        print(f"  {target.relative_to(PROJECT_ROOT)}  [{detail}]")

    if not args.apply:
        print("\nDry run. Re-run with --apply to restore.")
        return 0

    print("\nRestoring...")
    restored = 0
    for target, payload, _size in recovered:
        if target.exists():
            print(f"  skip (already exists): {target}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if payload.is_dir():
            shutil.copytree(payload, target)
        else:
            shutil.copy2(payload, target)
        restored += 1
    print(f"\nRestored {restored} entr(y/ies).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
