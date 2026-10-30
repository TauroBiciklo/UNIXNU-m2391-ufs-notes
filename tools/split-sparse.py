#!/usr/bin/env python3
"""Split a raw userdata image into fastboot-able Android sparse parts.

Each part carries a leading DONT_CARE chunk for its own offset inside the
partition, so flashing the parts in order reconstructs the image without any
offset support from fastboot.

Usage:
    python3 split-sparse.py <image> <outdir> [--part-mib N]

Notes that cost us a night:
  * the chunk header is (type, reserved, chunk_sz, total_sz) -- swapping the last
    two makes fastboot answer "Invalid sparse file format";
  * total_blks must count the skipped prefix, not just this part;
  * the phone's fastbootd accepted at most 256 MiB per transfer;
  * the bootloader on this device accepted sparse images and wrote nothing, so
    flash sparse parts through fastbootd, not the bootloader.
"""
import argparse
import struct
from pathlib import Path

BLK = 4096
READ = 16384
HDR = struct.Struct("<IHHHHIIII")   # magic, major, minor, file_sz, chunk_sz, blk, blks, chunks, csum
CHUNK = struct.Struct("<HHII")      # type, reserved, chunk_sz, total_sz


def skip(blocks):
    return CHUNK.pack(0xCAC3, 0, blocks, 12)


def raw(blocks, data):
    return CHUNK.pack(0xCAC4, 0, blocks, 12 + len(data)) + data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("outdir")
    ap.add_argument("--part-mib", type=int, default=250,
                    help="max payload per part in MiB (default 250, below the 256 MiB limit)")
    args = ap.parse_args()

    src = Path(args.image)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    for old in outdir.glob("userdata.part*.sparse"):
        old.unlink()

    total = src.stat().st_size
    if total % BLK:
        raise SystemExit(f"image size {total} is not a multiple of {BLK}")
    total_blks = total // BLK
    per_part = (args.part_mib * 1024 * 1024) // BLK
    parts = (total_blks + per_part - 1) // per_part
    print(f"raw {total:,} B = {total_blks:,} blocks -> {parts} parts")

    with src.open("rb") as f:
        for index in range(parts):
            start = index * per_part
            count = min(per_part, total_blks - start)
            dst = outdir / f"userdata.part{index:02d}.sparse"
            f.seek(start * BLK)
            with dst.open("wb") as out:
                out.write(b"\0" * HDR.size)
                chunks = 0
                if start:
                    out.write(skip(start))
                    chunks += 1
                left = count * BLK
                while left:
                    block = f.read(min(READ, left))
                    if not block:
                        raise SystemExit("short read")
                    left -= len(block)
                    n = len(block) // BLK
                    out.write(skip(n) if block == bytes(len(block)) else raw(n, block))
                    chunks += 1
                out.seek(0)
                out.write(HDR.pack(0xED26FF3A, 1, 0, 28, 12, BLK, start + count, chunks, 0))
            print(f"  part{index:02d}: blocks {start:,}..{start + count:,}  {dst.stat().st_size:,} B")


if __name__ == "__main__":
    main()
