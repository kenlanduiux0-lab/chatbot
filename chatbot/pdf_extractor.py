import fitz  # PyMuPDF
import os
import json
from datetime import datetime

# Use absolute path based on this file's location
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOC_STORE_PATH = os.path.join(BASE_DIR, "doc_store", "manual.txt")
DOC_META_PATH  = os.path.join(BASE_DIR, "doc_store", "meta.json")


def extract_pdf_to_text(pdf_path: str) -> dict:
    """Extract all text from a PDF and save it to doc_store/manual.txt."""
    doc = fitz.open(pdf_path)
    full_text = []

    for page_num, page in enumerate(doc, start=1):
        text = page.get_text("text")
        if text.strip():
            full_text.append(f"--- Page {page_num} ---\n{text.strip()}")

    extracted = "\n\n".join(full_text)
    page_count = len(doc)
    doc.close()

    os.makedirs(os.path.dirname(DOC_STORE_PATH), exist_ok=True)
    with open(DOC_STORE_PATH, "w", encoding="utf-8") as f:
        f.write(extracted)

    meta = {
        "filename": os.path.basename(pdf_path),
        "page_count": page_count,
        "uploaded_at": datetime.now().isoformat(),
        "char_count": len(extracted),
    }
    with open(DOC_META_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    return meta


def load_doc_text() -> str:
    """Load the stored documentation text."""
    if not os.path.exists(DOC_STORE_PATH):
        raise FileNotFoundError(
            "No documentation loaded. Please upload a PDF via the dashboard first."
        )
    with open(DOC_STORE_PATH, "r", encoding="utf-8") as f:
        return f.read()


def get_doc_meta() -> dict:
    """Return metadata about the currently loaded document."""
    if not os.path.exists(DOC_META_PATH):
        return {}
    with open(DOC_META_PATH, "r", encoding="utf-8") as f:
        return json.load(f)
