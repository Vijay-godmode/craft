"""Minimal resume parser prototype.
Provides helpers to extract text from .docx/.pdf/.txt bytes and to parse resume text into sections.
"""
import io
import os
from docx import Document
import pdfplumber


def extract_text_from_docx_bytes(b: bytes) -> str:
    f = io.BytesIO(b)
    doc = Document(f)
    parts = [para.text for para in doc.paragraphs]
    return '\n'.join(parts)


def extract_text_from_pdf_bytes(b: bytes) -> str:
    f = io.BytesIO(b)
    text_parts = []
    with pdfplumber.open(f) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                text_parts.append(text)
    return '\n'.join(text_parts)


def extract_text_from_bytes(filename: str, b: bytes) -> str:
    lower = filename.lower()
    if lower.endswith('.pdf'):
        return extract_text_from_pdf_bytes(b)
    if lower.endswith('.docx'):
        return extract_text_from_docx_bytes(b)
    try:
        return b.decode('utf-8', errors='ignore')
    except Exception:
        return ''


def parse_resume_text(text: str) -> dict:
    """Return a simple structured dict with sections by heuristic headings."""
    if not text:
        return {}
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    sections = {}
    current = 'header'
    sections[current] = []
    for line in lines:
        low = line.lower()
        if low.startswith('experience') or low.startswith('work'):
            current = 'experience'
            sections[current] = []
            continue
        if low.startswith('education'):
            current = 'education'
            sections[current] = []
            continue
        if low.startswith('skills'):
            current = 'skills'
            sections[current] = []
            continue
        sections.setdefault(current, []).append(line)
    # post-process
    for k in sections:
        sections[k] = '\n'.join(sections[k])
    return sections


def read_text_file(path: str) -> str:
    """Read a file from disk and return extracted text based on extension."""
    _, ext = os.path.splitext(path)
    ext = ext.lower()
    with open(path, 'rb') as f:
        b = f.read()
    return extract_text_from_bytes(path, b)


if __name__ == '__main__':
    # quick smoke test
    sample = 'Name\nSkills\nPython, Django, Flask\nExperience\nSoftware Engineer at X'
    print(parse_resume_text(sample))
