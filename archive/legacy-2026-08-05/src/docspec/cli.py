"""DocSpec command-line entry point."""

from __future__ import annotations

import argparse

from docspec.document_release_v3 import DocumentReleaseV3Error
from docspec.document_release_v3_cli import add_document_release_v3_parser


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="docspec")
    commands = parser.add_subparsers(dest="command", required=True)
    add_document_release_v3_parser(commands)
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (DocumentReleaseV3Error, ValueError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
