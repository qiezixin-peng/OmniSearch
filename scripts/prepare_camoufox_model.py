"""Materialize fpgen's indexed model for Windows builds that cannot open seekable zstd."""

from pathlib import Path

import zstandard
from fpgen.unpacker import VALUES_DATA


def main() -> None:
    destination = Path(VALUES_DATA)
    if destination.is_file() and destination.stat().st_size:
        print(f"Camoufox fpgen model ready: {destination}")
        return
    source = destination.with_suffix(".dat.zst")
    if not source.is_file():
        raise FileNotFoundError(f"Camoufox fpgen model is missing: {source}")
    temporary = destination.with_suffix(".dat.tmp")
    with source.open("rb") as compressed, temporary.open("wb") as expanded:
        zstandard.ZstdDecompressor().copy_stream(compressed, expanded)
    temporary.replace(destination)
    print(f"Camoufox fpgen model materialized: {destination}")


if __name__ == "__main__":
    main()
