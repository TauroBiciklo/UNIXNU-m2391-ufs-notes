#!/usr/bin/env python3
"""Dump a UniXNU ADT (kernel.macho.adt) as an indented tree.

The ADT is what the platform expert reads; it only carries DTS nodes marked with
`unixnu,adt-name`. Use this to confirm /cpus, /arm-io children (intc, ufshc,
ufsphy) and their property counts.

    mcopy -n -i payload.img ::/EFI/UniXNU/Payloads/kernel.macho.adt /tmp/kernel.adt
    python3 dump-adt.py /tmp/kernel.adt
"""
import struct
import sys
from pathlib import Path

ZERO = b"\0"


def align(off):
    return (off + 3) & ~3


def parse(data, off, depth, lines):
    nprops, nchildren = struct.unpack_from("<II", data, off)
    off += 8
    props = {}
    for _ in range(nprops):
        raw = struct.unpack_from("<32sI", data, off)
        key = raw[0].split(ZERO)[0].decode("ascii", "replace")
        size = raw[1]
        off += 36
        props[key] = data[off:off + size]
        off = align(off + size)
    name = props.get("name", b"?").split(ZERO)[0].decode("ascii", "replace")
    compat = props.get("compatible", b"").split(ZERO)[0].decode("ascii", "replace")
    lines.append("  " * depth + name + (f"  [{compat}]" if compat else "") + f"  ({nprops}p/{nchildren}c)")
    for _ in range(nchildren):
        off = parse(data, off, depth + 1, lines)
    return off


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    data = Path(sys.argv[1]).read_bytes()
    lines = []
    try:
        end = parse(data, 0, 0, lines)
        print("\n".join(lines))
        print(f"consumed {end} of {len(data)} bytes")
    except Exception as exc:                      # partial output is still useful
        print("\n".join(lines))
        print("parse failed:", exc)


if __name__ == "__main__":
    main()
