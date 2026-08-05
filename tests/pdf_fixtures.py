"""Generate small PDFs through DocSpec's selected PDF implementation."""

from __future__ import annotations

import pymupdf


def make_pdf(pages_text: list[str]) -> bytes:
    document = pymupdf.open()
    try:
        for text in pages_text:
            page = document.new_page()
            page.insert_text((72, 72), text)
        return document.tobytes()
    finally:
        document.close()

def make_textless_pdf() -> bytes:
    document = pymupdf.open()
    try:
        document.new_page()
        return document.tobytes()
    finally:
        document.close()
