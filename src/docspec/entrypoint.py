"""Console entry point for DocSpec."""

from __future__ import annotations

from docspec.cli import build_parser, main


__all__ = ["build_parser", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
