"""Load and serve Privacy Policy, Terms of Service, and IAP disclosure documents."""
from __future__ import annotations

import os
from functools import lru_cache

LEGAL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "legal")

LEGAL_DOCUMENTS = {
    "privacy": {
        "id": "privacy",
        "title": "Beyond the Stats Privacy Policy",
        "effective_date": "2026-07-19",
        "filename": "privacy_policy.txt",
        "path": "/privacy",
        "api_path": "/api/legal/privacy",
    },
    "terms": {
        "id": "terms",
        "title": "Beyond the Stats Terms of Service",
        "effective_date": "2026-07-19",
        "filename": "terms_of_service.txt",
        "path": "/terms",
        "api_path": "/api/legal/terms",
    },
    "subscriptions": {
        "id": "subscriptions",
        "title": "Auto-Renewable Subscription Disclosure",
        "effective_date": "2026-07-19",
        "filename": "subscription_disclosure.txt",
        "path": "/subscriptions",
        "api_path": "/api/legal/subscriptions",
    },
    "draftit_privacy": {
        "id": "draftit_privacy",
        "title": "Privacy Policy for Draft It!",
        "effective_date": "2026-08-01",
        "filename": "draftit_privacy_policy.txt",
        "path": "/draftit/privacy",
        "api_path": "/api/legal/draftit_privacy",
    },
}


@lru_cache(maxsize=8)
def _read_legal_file(filename: str) -> str:
    path = os.path.join(LEGAL_DIR, filename)
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read().strip()


def list_legal_documents() -> list[dict]:
    return [
        {
            "id": meta["id"],
            "title": meta["title"],
            "effective_date": meta["effective_date"],
            "url": meta["path"],
            "api_url": meta["api_path"],
        }
        for meta in LEGAL_DOCUMENTS.values()
    ]


def get_legal_document(doc_id: str) -> dict | None:
    meta = LEGAL_DOCUMENTS.get(str(doc_id or "").strip().lower())
    if not meta:
        return None
    try:
        body = _read_legal_file(meta["filename"])
    except OSError:
        return None
    return {
        "ok": True,
        "id": meta["id"],
        "title": meta["title"],
        "effective_date": meta["effective_date"],
        "url": meta["path"],
        "api_url": meta["api_path"],
        "content_type": "text/plain",
        "body": body,
    }


def plain_text_to_html_paragraphs(text: str) -> str:
    """Convert plain-text legal copy into simple HTML paragraphs/lists."""
    import html as html_lib

    blocks = []
    paragraph: list[str] = []
    list_items: list[str] = []

    def flush_paragraph():
        nonlocal paragraph
        if paragraph:
            blocks.append("<p>" + " ".join(html_lib.escape(line) for line in paragraph) + "</p>")
            paragraph = []

    def flush_list():
        nonlocal list_items
        if list_items:
            items = "".join(f"<li>{html_lib.escape(item)}</li>" for item in list_items)
            blocks.append(f"<ul>{items}</ul>")
            list_items = []

    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            flush_list()
            continue
        if line.startswith("- "):
            flush_paragraph()
            list_items.append(line[2:].strip())
            continue
        flush_list()
        # Section headings like "1. Title" or all-caps short titles
        if (len(line) < 80 and line[:1].isdigit() and ". " in line[:4]) or (
            line.isupper() and len(line) < 80
        ):
            flush_paragraph()
            blocks.append(f"<h3>{html_lib.escape(line)}</h3>")
            continue
        if line.startswith("Beyond the Stats") or line.startswith("Auto-Renewable"):
            flush_paragraph()
            blocks.append(f"<h2>{html_lib.escape(line)}</h2>")
            continue
        if line.startswith("Effective Date:"):
            flush_paragraph()
            blocks.append(f"<p class=\"legal-meta\"><em>{html_lib.escape(line)}</em></p>")
            continue
        paragraph.append(line)

    flush_paragraph()
    flush_list()
    return "\n".join(blocks)
