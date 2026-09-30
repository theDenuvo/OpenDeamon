"""Strip a UTF-8 BOM from files in place. Idempotent."""
import pathlib
import sys

BOM = "﻿"
targets = [pathlib.Path(p) for p in sys.argv[1:]]
for p in targets:
    raw = p.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        p.write_bytes(raw[3:])
        print(f"stripped BOM: {p}  ({len(raw)} -> {len(raw) - 3} B)")
    else:
        print(f"no BOM: {p}")