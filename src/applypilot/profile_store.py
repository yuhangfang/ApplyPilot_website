"""Load/save user profile and searches config (shared by hub and future wizard)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Protocol

import yaml

from applypilot.config import PROFILE_PATH, RESUME_PATH, RESUME_PDF_PATH, SEARCH_CONFIG_PATH


class UserProfileStore(Protocol):
    def load_profile(self) -> dict[str, Any]: ...
    def save_profile(self, data: dict[str, Any]) -> None: ...
    def validate_profile(self, data: dict[str, Any]) -> list[str]: ...


# Core sections required for apply; skills_boundary / resume_facts default to {} if omitted.
REQUIRED_TOP_LEVEL = (
    "personal",
    "work_authorization",
    "compensation",
    "experience",
    "eeo_voluntary",
    "availability",
)


class JsonProfileStore:
    """Default store: ~/.applypilot/profile.json."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or PROFILE_PATH

    def load_profile(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save_profile(self, data: dict[str, Any]) -> None:
        merged = dict(data)
        for k in ("skills_boundary", "resume_facts"):
            if k not in merged:
                merged[k] = {}
        errors = self.validate_profile(merged)
        if errors:
            raise ValueError("; ".join(errors))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".json", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(merged, f, indent=2, ensure_ascii=False)
                f.write("\n")
            Path(tmp).replace(self.path)
        except Exception:
            try:
                Path(tmp).unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def validate_profile(self, data: dict[str, Any]) -> list[str]:
        errs: list[str] = []
        if not isinstance(data, dict):
            return ["Profile must be a JSON object"]
        for key in REQUIRED_TOP_LEVEL:
            if key not in data:
                errs.append(f"Missing required section: {key}")
        personal = data.get("personal")
        if isinstance(personal, dict):
            if not personal.get("email"):
                errs.append("personal.email is required")
            if not personal.get("full_name"):
                errs.append("personal.full_name is required")
        elif "personal" in data:
            errs.append("personal must be an object")
        return errs


def load_searches_text() -> str:
    if not SEARCH_CONFIG_PATH.exists():
        return ""
    return SEARCH_CONFIG_PATH.read_text(encoding="utf-8")


def save_searches_text(text: str) -> None:
    """Parse YAML then write file; raises yaml.YAMLError on bad syntax."""
    yaml.safe_load(text)
    SEARCH_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".yaml", dir=str(SEARCH_CONFIG_PATH.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            if not text.endswith("\n"):
                f.write("\n")
        Path(tmp).replace(SEARCH_CONFIG_PATH)
    except Exception:
        try:
            Path(tmp).unlink(missing_ok=True)
        except OSError:
            pass
        raise


def env_key_status() -> dict[str, bool]:
    """Which common env vars are set (never values)."""
    keys = (
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "LLM_URL",
        "LLM_MODEL",
        "CAPSOLVER_API_KEY",
        "CHROME_PATH",
    )
    return {k: bool(os.environ.get(k)) for k in keys}


def resume_paths_status() -> dict[str, Any]:
    return {
        "resume_txt": str(RESUME_PATH),
        "resume_txt_exists": RESUME_PATH.exists(),
        "resume_pdf": str(RESUME_PDF_PATH),
        "resume_pdf_exists": RESUME_PDF_PATH.exists(),
    }


def save_resume_bytes(kind: str, data: bytes, max_bytes: int = 5_000_000) -> None:
    """kind is 'txt' or 'pdf'."""
    if len(data) > max_bytes:
        raise ValueError(f"File too large (max {max_bytes} bytes)")
    if kind == "txt":
        RESUME_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESUME_PATH.write_bytes(data)
    elif kind == "pdf":
        RESUME_PDF_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESUME_PDF_PATH.write_bytes(data)
    else:
        raise ValueError("kind must be txt or pdf")
