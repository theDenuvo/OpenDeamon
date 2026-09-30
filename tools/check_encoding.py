"""Scan the project for encoding damage: BOM and double-encoded Cyrillic.

Both defects came from the same PowerShell habit:
  Get-Content (no -Encoding)  -> reads UTF-8 as CP1251
  Set-Content -Encoding UTF8  -> writes the mojibake back, plus a BOM
TODO П.7 reports the same corruption in memories/MEMORY.md, so it is worth
scanning for rather than fixing one file at a time.
"""
import pathlib
import sys

ROOT = pathlib.Path(r"A:\OpenDeamon")
SKIP_DIRS = {".git", "cache", "node_modules", "ovui", "owui", "desktop",
             "venvs", ".venv", "_setup_tmp", "generated", "models"}
EXTS = {".md", ".py", ".ps1", ".yaml", ".yml", ".json", ".txt", ".jsonc"}
BOM = "﻿"
# Tell-tale of CP1251-misread UTF-8: Cyrillic letters rendered as Latin
# lookalikes such as "Рџ", "Р°", "С‚" appearing where prose should be.
MOJI = ("Рџ", "СЂ", "Р°", "Рё", "Рµ", "Рѕ", "Р°")


def main() -> int:
    me = pathlib.Path(__file__).resolve()
    bom_files, moji_files = [], []
    for p in ROOT.rglob("*"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() not in EXTS or not p.is_file():
            continue
        if p.resolve() == me:
            continue          # this file holds the markers as data, by design
        try:
            raw = p.read_bytes()
            text = raw.decode("utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = p.relative_to(ROOT)
        if text.startswith(BOM):
            bom_files.append((rel, len(raw)))
        # A file that legitimately contains Russian will contain "Р" only in
        # code, so require several distinct mojibake markers to flag it.
        hits = sum(1 for m in MOJI if m in text)
        if hits >= 3:
            moji_files.append((rel, hits))

    print(f"BOM present: {len(bom_files)}")
    for rel, size in bom_files[:20]:
        print(f"   {rel}  ({size} B)")
    print(f"\nsuspected double-encoded: {len(moji_files)}")
    for rel, hits in moji_files[:20]:
        print(f"   {rel}  ({hits} markers)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())