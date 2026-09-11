"""The public DocSpec command entry points."""

from docspec.cli.parser import build_parser, main
from docspec.cli_io import CliError

__all__ = ["CliError", "build_parser", "main"]
