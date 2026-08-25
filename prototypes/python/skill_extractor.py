"""Simple skill extraction prototype.

This module provides a minimal extractor that finds likely skills from job posting text.
It uses simple heuristics and optional TF-IDF/embedding hooks for later improvement.
"""
from collections import Counter
import re

COMMON_STOPWORDS = set(["and","or","the","a","an","with","to","of","in","for","on","by","as","at"]) 

SKILL_CANDIDATE_REGEX = re.compile(r"[A-Za-z+#\.]{2,}(-[A-Za-z+#\.]{2,})?")


def extract_skills_from_text(text, top_n=30):
    """Return list of candidate skills from a job posting text."""
    if not text:
        return []
    text = re.sub(r"[\n\r]+"," ", text)
    tokens = SKILL_CANDIDATE_REGEX.findall(text)
    tokens = [t[0] if isinstance(t, tuple) else t for t in tokens]
    tokens = [t.strip().lower() for t in tokens if len(t) > 1]
    tokens = [t for t in tokens if t not in COMMON_STOPWORDS and not t.isdigit()]
    # simple frequency-based selection
    freq = Counter(tokens)
    common = [w for w,_ in freq.most_common(top_n)]
    # post-filter: remove too-generic words
    filtered = [w for w in common if len(w) > 2]
    return filtered


if __name__ == "__main__":
    sample = """
    We are hiring a Senior Python Developer with experience in Django, Flask, REST APIs, AWS (Lambda, S3), Docker, Kubernetes, CI/CD, and SQL.
    Experience with machine learning frameworks such as PyTorch or TensorFlow is a plus.
    """
    print(extract_skills_from_text(sample))
