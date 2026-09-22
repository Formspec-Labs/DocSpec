"""Shared source-reading choices included in processing identities.

``JSON_SOURCE_PROFILE`` bounds JSON bytes, nodes, depth and numbers for the JSON
extractor and record segmenter, and rides inside their configuration digests.
"""

from types import MappingProxyType

JSON_SOURCE_PROFILE = MappingProxyType(
    {
        "number_policy": "finite-float",
        "max_bytes": 64 * 1024 * 1024,
        "max_nodes": 1_000_000,
        "max_depth": 256,
    }
)
