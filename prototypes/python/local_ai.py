"""Optional local AI assistance through Ollama, with a transparent fallback.

CareerCraft never sends a resume to a cloud model. If Ollama is installed,
requests stay on 127.0.0.1. If it is unavailable, a labelled rule-based review
still provides simple spelling and resume-quality checks.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from shutil import which
import subprocess
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen2.5:1.5b"
MAX_TEXT = 14000
MODEL_NAME_RE = re.compile(r"[A-Za-z0-9_.:-]{1,100}")


def local_ollama_binary() -> str | None:
    """Find Ollama without relying solely on a PATH refresh after install."""
    candidates = [which("ollama")]
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates.extend(
        [
            str(Path(program_files) / "Ollama" / "ollama.exe"),
            str(Path(local_app_data) / "Programs" / "Ollama" / "ollama.exe") if local_app_data else "",
        ]
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate))
    return None


def valid_model_name(value: str) -> bool:
    return bool(MODEL_NAME_RE.fullmatch(str(value or "")))


def _json_request(path: str, payload: dict[str, Any] | None = None, timeout: int = 45) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        OLLAMA_BASE_URL + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if data is not None else "GET",
    )
    with urlopen(request, timeout=timeout) as response:  # nosec B310 - fixed localhost endpoint
        return json.loads(response.read().decode("utf-8"))


def ollama_status(selected_model: str = DEFAULT_MODEL) -> dict[str, Any]:
    binary = local_ollama_binary()
    try:
        response = _json_request("/api/tags", timeout=2)
        models = [str(item.get("name", "")) for item in response.get("models", []) if item.get("name")]
        return {
            "available": True,
            "installed": bool(binary),
            "provider": "Ollama (local)",
            "selected_model": selected_model,
            "models": models,
            "selected_installed": selected_model in models,
            "binary_path": binary or "",
            "setup_command": f"ollama pull {selected_model}",
        }
    except (URLError, TimeoutError, OSError, json.JSONDecodeError):
        return {
            "available": False,
            "installed": bool(binary),
            "provider": "Built-in review",
            "selected_model": selected_model,
            "models": [],
            "selected_installed": False,
            "binary_path": binary or "",
            "setup_command": f"ollama pull {selected_model}",
            "setup_url": "https://ollama.com/download",
        }


def start_ollama_service(model: str = DEFAULT_MODEL) -> dict[str, Any]:
    """Start a local Ollama server only after an explicit user action."""
    if not valid_model_name(model):
        raise ValueError("Use a valid local Ollama model name.")
    status = ollama_status(model)
    if status["available"]:
        status["message"] = "Ollama is already running locally."
        return status
    binary = status.get("binary_path")
    if not binary:
        raise RuntimeError("Ollama is not installed. Install it from the official Ollama page, then return here.")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen(
        [binary, "serve"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    status["started"] = True
    status["pid"] = process.pid
    status["message"] = "Starting Ollama locally. Refresh the status in a few seconds."
    return status


def pull_local_model(model: str = DEFAULT_MODEL) -> dict[str, Any]:
    """Begin an explicit local model download. It never contacts a cloud LLM."""
    if not valid_model_name(model):
        raise ValueError("Use a valid local Ollama model name.")
    status = ollama_status(model)
    binary = status.get("binary_path")
    if not binary:
        raise RuntimeError("Ollama is not installed. Install it before downloading a local model.")
    if status["selected_installed"]:
        status["message"] = f"{model} is already installed locally."
        return status
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen(
        [binary, "pull", model],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    status["pull_started"] = True
    status["pid"] = process.pid
    status["message"] = f"Downloading {model} locally. Refresh this status after the download completes."
    return status


def clean_text(value: Any, limit: int = MAX_TEXT) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\x00", " ")).strip()[:limit]


def fallback_proofread(text: str) -> dict[str, Any]:
    suggestions: list[dict[str, str]] = []
    revised = re.sub(r"\s+", " ", text).strip()
    replacements = {
        r"\bresponsible for\b": "Use a concrete action verb and outcome instead of 'responsible for'.",
        r"\bworked on\b": "Name what you tested or built and the outcome instead of 'worked on'.",
        r"\bvarious\b": "Replace 'various' with the exact scope, tools, or number.",
        r"\bhelped\b": "Use a precise contribution verb such as tested, automated, validated, or collaborated.",
    }
    for pattern, reason in replacements.items():
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            suggestions.append({"kind": "impact", "original": match.group(0), "suggestion": "Rewrite this phrase with factual scope and result.", "reason": reason})
    if re.search(r"\b(teh|recieve|seperate|succesful|enviroment)\b", text, re.IGNORECASE):
        suggestions.append({"kind": "spelling", "original": "Possible spelling issue", "suggestion": "Review the highlighted wording carefully.", "reason": "The built-in check found a common spelling pattern."})
    if len(revised) > 260 and not re.search(r"[.!?]", revised):
        suggestions.append({"kind": "clarity", "original": "Long sentence", "suggestion": "Split this into two concise, factual sentences.", "reason": "Shorter sentences improve recruiter and ATS readability."})
    if not suggestions:
        suggestions.append({"kind": "check", "original": "No simple issue detected", "suggestion": "Confirm every sentence has a tool, scope, action or measurable result where possible.", "reason": "Rule-based checks are limited; local AI can provide a fuller review when enabled."})
    return {
        "provider": "Built-in review",
        "mode": "rule_based",
        "summary": "Basic local spelling and clarity scan completed. No cloud service was used.",
        "revised_text": revised,
        "suggestions": suggestions[:6],
        "strengths": [],
        "risks": [],
    }


def fallback_resume_review(resume: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    risks = []
    strengths = []
    summary = clean_text(resume.get("summary"))
    if summary:
        strengths.append("A professional summary is present.")
    else:
        risks.append("Add a concise factual professional summary.")
    experience = resume.get("experience") or []
    bullets = [bullet for entry in experience if isinstance(entry, dict) for bullet in (entry.get("bullets") or [])]
    if bullets:
        strengths.append(f"The draft includes {len(bullets)} experience bullet(s).")
    else:
        risks.append("Add evidence-based bullets beneath each relevant role.")
    missing = analysis.get("missing_skills") or []
    if missing:
        risks.append("Review missing role requirements only when you can support them honestly: " + ", ".join(missing[:4]) + ".")
    return {
        "provider": "Built-in review",
        "mode": "rule_based",
        "summary": "Local resume structure review completed. Enable Ollama for a fuller language review without sending your data to the cloud.",
        "revised_text": "",
        "suggestions": [],
        "strengths": strengths,
        "risks": risks,
    }


def parse_model_json(content: str) -> dict[str, Any]:
    cleaned = str(content or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    payload = json.loads(cleaned)
    if not isinstance(payload, dict):
        raise ValueError("The local model returned an unsupported response.")
    return payload


def sanitise_model_result(payload: dict[str, Any], provider: str) -> dict[str, Any]:
    def strings(value: Any, limit: int = 6) -> list[str]:
        return [clean_text(item, 500) for item in value if clean_text(item, 500)][:limit] if isinstance(value, list) else []

    suggestions = []
    for item in payload.get("suggestions", []) if isinstance(payload.get("suggestions"), list) else []:
        if isinstance(item, dict):
            suggestions.append(
                {
                    "kind": clean_text(item.get("kind"), 40) or "suggestion",
                    "original": clean_text(item.get("original"), 500),
                    "suggestion": clean_text(item.get("suggestion"), 700),
                    "reason": clean_text(item.get("reason"), 700),
                }
            )
    return {
        "provider": provider,
        "mode": "local_llm",
        "summary": clean_text(payload.get("summary"), 1000),
        "revised_text": clean_text(payload.get("revised_text"), MAX_TEXT),
        "suggestions": suggestions[:8],
        "strengths": strings(payload.get("strengths")),
        "risks": strings(payload.get("risks")),
    }


def local_ai_review(task: str, payload: dict[str, Any], model: str = DEFAULT_MODEL) -> dict[str, Any]:
    """Run an optional local model; transparently fall back when unavailable."""
    status = ollama_status(model)
    text = clean_text(payload.get("text"))
    if not status["available"] or not status["selected_installed"]:
        return fallback_proofread(text) if task == "proofread" else fallback_resume_review(payload.get("resume") or {}, payload.get("analysis") or {})

    if task == "proofread":
        task_prompt = (
            "Review the following resume text for spelling, clarity and stronger factual wording. "
            "Do not invent achievements, metrics, employers, skills, dates or credentials. Return concise suggestions and, "
            "only when it preserves facts, a revised_text.\n\nTEXT:\n" + text
        )
    else:
        resume_text = json.dumps(payload.get("resume") or {}, ensure_ascii=False)[:MAX_TEXT]
        analysis = json.dumps(payload.get("analysis") or {}, ensure_ascii=False)[:5000]
        task_prompt = (
            "Review this QA/Test Engineer resume against its job-match analysis for language, evidence, ATS readability and truthfulness. "
            "Never invent claims or tell the candidate to add a skill they cannot support. Return strengths, risks and suggestions.\n\n"
            f"RESUME:\n{resume_text}\n\nANALYSIS:\n{analysis}"
        )
    system = (
        "You are a careful local resume editor. Preserve factual truth. Return only valid JSON with keys: "
        "summary, revised_text, strengths, risks, suggestions. Each suggestion must have kind, original, suggestion and reason."
    )
    try:
        response = _json_request(
            "/api/chat",
            {
                "model": model,
                "stream": False,
                "format": "json",
                "keep_alive": "2m",
                "options": {"temperature": 0.1, "num_predict": 700},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": task_prompt}],
            },
            timeout=70,
        )
        result = parse_model_json(response.get("message", {}).get("content", ""))
        return sanitise_model_result(result, f"Ollama: {model}")
    except Exception:
        fallback = fallback_proofread(text) if task == "proofread" else fallback_resume_review(payload.get("resume") or {}, payload.get("analysis") or {})
        fallback["summary"] = "Local AI could not complete this pass, so the built-in local review was used instead. " + fallback["summary"]
        return fallback
