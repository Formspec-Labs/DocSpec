"""Optional outer adapter: a pinned third-party token counter.

`docspec.processing.bounded_segmentation` names no tokenizer package. It takes
a `TokenCounter` -- a name, a version, and a count -- so the policy can be run
against an exact test counter with a known budget, and so no third-party
tokenizer reaches DocSpec's core. This module is where the real one lives, on
the same terms as the lazy PDF extractor and the Dagster runtime: the provider
is imported inside the constructor, so importing this module costs nothing and
`docspec` stays importable with the extra uninstalled.

Install with the `tokens` extra. The counter reports the encoding name and the
installed distribution version, and `BoundedSegmentSettings.for_counter` binds
both into the policy digest, so a segmentation carries the exact tokenizer
build that measured it.
"""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version

DEFAULT_TOKEN_ENCODING = "o200k_base"
TOKEN_COUNTER_DISTRIBUTION = "tiktoken"


class TiktokenCounter:
    """Pinned OpenAI-compatible token counter for bounded segmentation."""

    def __init__(self, encoding_name: str = DEFAULT_TOKEN_ENCODING) -> None:
        try:
            provider = import_module(TOKEN_COUNTER_DISTRIBUTION)
        except ModuleNotFoundError as error:
            raise RuntimeError(
                f"the {TOKEN_COUNTER_DISTRIBUTION} token counter requires the DocSpec 'tokens' extra"
            ) from error
        try:
            self.version = distribution_version(TOKEN_COUNTER_DISTRIBUTION)
        except PackageNotFoundError as error:
            raise RuntimeError(
                f"the installed {TOKEN_COUNTER_DISTRIBUTION} package declares no version to pin"
            ) from error
        self.name = encoding_name
        self._encoding = provider.get_encoding(encoding_name)

    def count(self, text: str) -> int:
        return len(self._encoding.encode(text, disallowed_special=()))
