"""Simple scoring between job posting skills and resume content.

Robust imports: ensure module works when `app.py` is run as a script
by adding the module directory to `sys.path` before importing.
"""
from collections import Counter
import os
import sys

# Ensure current package directory is on sys.path so absolute imports work
HERE = os.path.dirname(__file__)
if HERE and HERE not in sys.path:
    sys.path.insert(0, HERE)

from skill_extractor import extract_skills_from_text


def score_resume_against_posting(posting_text, resume_text):
    # import parser lazily to avoid import-time package issues
    # attempt absolute import first (works when sys.path includes module dir)
    try:
        from resume_parser import parse_resume_text
    except Exception:
        # fallback: try relative import
        from .resume_parser import parse_resume_text

    posting_skills = set(extract_skills_from_text(posting_text, top_n=100))
    sections = parse_resume_text(resume_text)
    resume_text_full = '\n'.join(sections.values())
    resume_skills = set(extract_skills_from_text(resume_text_full, top_n=200))
    matched = posting_skills & resume_skills
    score = 0
    if posting_skills:
        score = int(100 * len(matched) / len(posting_skills))
    suggestions = list(posting_skills - resume_skills)[:20]
    return {
        'posting_skill_count': len(posting_skills),
        'resume_skill_count': len(resume_skills),
        'matched_count': len(matched),
        'score_percent': score,
        'matched': sorted(matched),
        'suggestions': suggestions,
    }

if __name__ == '__main__':
    posting = 'Senior Python Developer with Django, Flask, AWS, Docker, Kubernetes'
    resume = 'Skills\nPython, Flask, Docker\nExperience\nDeveloper at Y'
    print(score_resume_against_posting(posting, resume))
