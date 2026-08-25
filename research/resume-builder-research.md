# Resume Builder Research Summary

This document summarizes findings for building an ATS-aware resume builder that detects role-specific skills from job postings, scores and adapts resumes, and provides safe LinkedIn application assistance.

## Key Findings

- Skill extraction: use a hybrid approach — rule-based section parsing + keyword extraction (KeyBERT/RAKE) + semantic matching using sentence embeddings (SentenceTransformers).
- Resume parsing: prefer `python-docx` for DOCX and `pdfplumber` / `pdfminer.six` for PDF text extraction; fallback to plain-text parsing for robust checks.
- Scoring: weighted keyword overlap + embedding similarity; prioritize skills appearing in job requirements and seniority words ("senior", "lead").
- ATS best practices: semantic section headings, no images or tables for critical content, standard fonts, avoid headers/footers for contact info, prefer DOCX or simple PDF, and include a Skills section.
- LinkedIn automation: avoid unauthorized scraping; prefer user-driven automation (downloadable Playwright/selenium scripts) or official APIs where permissions allow.

## Vendors & Features (examples)
- Jobscan: resume vs job match scoring, tailored suggestions (paid features).
- Rezi: ATS-optimized resume generation and templates.
- Zety / Resume Worded: templates, keyword suggestions, and writing tips.

## OSS Libraries & Models
- Python: spaCy, SentenceTransformers, keybert, rake-nltk, python-docx, pdfplumber, pdfminer.six
- Node: pdf-parse, textract, natural, compromise, sentence-transformers via ONNX or API

## Risks & Legal
- Respect LinkedIn Terms of Service; do not provide covert application automation that acts on behalf of users without clear consent.
- Store resumes only with explicit consent and retention policy; support deletion endpoints.

## Recommended Next Steps
1. Build small prototypes (Python + Node) for parsing, extraction, and scoring.
2. Create an evaluation dataset of job postings and resumes to validate scoring and extraction.
3. Implement ATS-friendly DOCX generator and manual user-run application helpers.

***
Generated as project scaffold starting point.
