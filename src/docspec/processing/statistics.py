"""Local content statistics, independent of the execution or retention format."""

from docspec.domain.identity import sha256_digest


def content_statistics(content, segment_id, evidence):
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    return {
        "segmentId": segment_id,
        "contentDigest": sha256_digest(content),
        "byteCount": len(content),
        "utf8CodepointCount": len(text) if text is not None else None,
        "lineCount": len(text.splitlines()) if text else 0 if text is not None else None,
        "wordCount": len(text.split()) if text is not None else None,
        "evidence": evidence.to_dict(),
    }
