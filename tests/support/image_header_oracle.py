"""Frozen DocSpec 97ffe02 image observations; test-only pre-port oracle."""

import struct

def _image_dimensions(content: bytes) -> tuple[str, int | None, int | None]:
    """Return ``(kind, width, height)``; short, unknown or unparsable bytes report no dimensions."""
    if content.startswith(b"\x89PNG\r\n\x1a\n") and len(content) >= 24:
        return "png", int.from_bytes(content[16:20], "big"), int.from_bytes(content[20:24], "big")
    if content[:6] in {b"GIF87a", b"GIF89a"} and len(content) >= 10:
        return "gif", int.from_bytes(content[6:8], "little"), int.from_bytes(content[8:10], "little")
    if content.startswith(b"\xff\xd8"):
        dimensions = _jpeg_dimensions(content)
        return ("jpeg", *dimensions) if dimensions is not None else ("jpeg", None, None)
    return "unknown", None, None


def _jpeg_dimensions(content: bytes) -> tuple[int, int] | None:
    """Scan JPEG markers for a start-of-frame; ``None`` when no well-formed frame is found."""
    position = 2
    start_of_frame = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})
    while position + 4 <= len(content):
        if content[position] != 0xFF:
            position += 1
            continue
        marker = content[position + 1]
        position += 2
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        length = struct.unpack(">H", content[position : position + 2])[0]
        if length < 2 or position + length > len(content):
            return None
        if marker in start_of_frame and length >= 7:
            height, width = struct.unpack(">HH", content[position + 3 : position + 7])
            return width, height
        position += length
    return None
