"""Structured setup diagnostics (shared by CLI doctor and localhost hub)."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Literal

from applypilot.config import (
    PROFILE_PATH,
    RESUME_PATH,
    RESUME_PDF_PATH,
    SEARCH_CONFIG_PATH,
    get_chrome_path,
    get_tier,
    load_env,
)


DoctorStatus = Literal["ok", "warn", "fail", "info"]


@dataclass(frozen=True)
class DoctorCheck:
    """One row in the doctor report."""

    id: str
    label: str
    status: DoctorStatus
    note: str

    def to_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "status": self.status, "note": self.note}


def collect_doctor_report() -> list[DoctorCheck]:
    """Run all doctor checks and return structured rows (no Rich markup)."""
    load_env()
    results: list[DoctorCheck] = []

    if PROFILE_PATH.exists():
        results.append(DoctorCheck("profile", "profile.json", "ok", str(PROFILE_PATH)))
    else:
        results.append(
            DoctorCheck("profile", "profile.json", "fail", "Run 'applypilot init' to create")
        )

    if RESUME_PATH.exists():
        results.append(DoctorCheck("resume_txt", "resume.txt", "ok", str(RESUME_PATH)))
    elif RESUME_PDF_PATH.exists():
        results.append(
            DoctorCheck(
                "resume_txt",
                "resume.txt",
                "warn",
                "Only PDF found — plain-text needed for AI stages",
            )
        )
    else:
        results.append(
            DoctorCheck("resume_txt", "resume.txt", "fail", "Run 'applypilot init' to add your resume")
        )

    if SEARCH_CONFIG_PATH.exists():
        results.append(DoctorCheck("searches", "searches.yaml", "ok", str(SEARCH_CONFIG_PATH)))
    else:
        results.append(
            DoctorCheck(
                "searches",
                "searches.yaml",
                "warn",
                "Will use example config — run 'applypilot init'",
            )
        )

    try:
        import jobspy  # noqa: F401

        results.append(DoctorCheck("jobspy", "python-jobspy", "ok", "Job board scraping available"))
    except ImportError:
        results.append(
            DoctorCheck(
                "jobspy",
                "python-jobspy",
                "warn",
                "pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex",
            )
        )

    has_gemini = bool(os.environ.get("GEMINI_API_KEY"))
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    has_local = bool(os.environ.get("LLM_URL"))
    if has_gemini:
        model = os.environ.get("LLM_MODEL", "gemini-2.0-flash")
        results.append(DoctorCheck("llm", "LLM API key", "ok", f"Gemini ({model})"))
    elif has_openai:
        model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
        results.append(DoctorCheck("llm", "LLM API key", "ok", f"OpenAI ({model})"))
    elif has_local:
        results.append(DoctorCheck("llm", "LLM API key", "ok", f"Local: {os.environ.get('LLM_URL')}"))
    else:
        results.append(
            DoctorCheck(
                "llm",
                "LLM API key",
                "fail",
                "Set GEMINI_API_KEY in ~/.applypilot/.env (run 'applypilot init')",
            )
        )

    claude_bin = shutil.which("claude")
    if claude_bin:
        results.append(DoctorCheck("claude_cli", "Claude Code CLI", "ok", claude_bin))
    else:
        results.append(
            DoctorCheck(
                "claude_cli",
                "Claude Code CLI",
                "fail",
                "Install from https://claude.ai/code (needed for auto-apply)",
            )
        )

    try:
        chrome_path = get_chrome_path()
        results.append(DoctorCheck("chrome", "Chrome/Chromium", "ok", chrome_path))
    except FileNotFoundError:
        results.append(
            DoctorCheck(
                "chrome",
                "Chrome/Chromium",
                "fail",
                "Install Chrome or set CHROME_PATH env var (needed for auto-apply)",
            )
        )

    npx_bin = shutil.which("npx")
    if npx_bin:
        results.append(DoctorCheck("npx", "Node.js (npx)", "ok", npx_bin))
    else:
        results.append(
            DoctorCheck(
                "npx",
                "Node.js (npx)",
                "fail",
                "Install Node.js 18+ from nodejs.org (needed for auto-apply)",
            )
        )

    if os.environ.get("CAPSOLVER_API_KEY"):
        results.append(DoctorCheck("capsolver", "CapSolver API key", "ok", "CAPTCHA solving enabled"))
    else:
        results.append(
            DoctorCheck(
                "capsolver",
                "CapSolver API key",
                "info",
                "Set CAPSOLVER_API_KEY in .env for CAPTCHA solving (optional)",
            )
        )

    return results


def doctor_tier_summary() -> dict:
    """Tier number and human label for API/UI."""
    from applypilot.config import TIER_LABELS

    tier = get_tier()
    return {"tier": tier, "label": TIER_LABELS.get(tier, str(tier))}
