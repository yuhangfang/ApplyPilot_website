"""Observers for Claude Code stream-json during apply (logs, hub SSE, tests)."""

from __future__ import annotations

import json
from typing import Any, Protocol


class ApplyObserver(Protocol):
    """Receive structured events from the apply Claude session."""

    def on_job_prompt(self, worker_id: int, job_title: str, job_url: str, prompt: str) -> None:
        """Full stdin prompt once per job."""
        ...

    def on_raw_ndjson(self, worker_id: int, line: str) -> None:
        """Each raw line from claude stdout (JSON or not)."""
        ...

    def on_assistant_text(self, worker_id: int, text: str) -> None:
        """Assistant message text block."""
        ...

    def on_tool_use(self, worker_id: int, tool_name: str, tool_input: dict[str, Any]) -> None:
        """One tool_use block (name already shortened from mcp__ prefix if desired)."""
        ...

    def on_tool_result(
        self,
        worker_id: int,
        tool_use_id: str,
        content: Any,
        is_error: bool,
    ) -> None:
        """Tool output fed back to the model (stream-json ``user`` message ``tool_result`` blocks)."""
        ...

    def on_user_message_text(self, worker_id: int, text: str) -> None:
        """Plain text in a ``user`` role message (also counts as model context)."""
        ...

    def on_assistant_usage(self, worker_id: int, usage: dict[str, Any]) -> None:
        """Usage attached to one completed ``assistant`` NDJSON line (per assistant message)."""
        ...

    def on_stream_result(self, worker_id: int, message: dict[str, Any]) -> None:
        """type == result message (usage, cost, etc.)."""
        ...


class NoOpApplyObserver:
    """Default implementations (no-op)."""

    def on_job_prompt(self, worker_id: int, job_title: str, job_url: str, prompt: str) -> None:
        pass

    def on_raw_ndjson(self, worker_id: int, line: str) -> None:
        pass

    def on_assistant_text(self, worker_id: int, text: str) -> None:
        pass

    def on_tool_use(self, worker_id: int, tool_name: str, tool_input: dict[str, Any]) -> None:
        pass

    def on_tool_result(
        self,
        worker_id: int,
        tool_use_id: str,
        content: Any,
        is_error: bool,
    ) -> None:
        pass

    def on_user_message_text(self, worker_id: int, text: str) -> None:
        pass

    def on_assistant_usage(self, worker_id: int, usage: dict[str, Any]) -> None:
        pass

    def on_stream_result(self, worker_id: int, message: dict[str, Any]) -> None:
        pass


def normalize_tool_name(name: str) -> str:
    return name.replace("mcp__playwright__", "").replace("mcp__gmail__", "gmail:")


def serialize_tool_result_content(content: Any, max_chars: int = 48_000) -> dict[str, Any]:
    """Normalize tool_result ``content`` for hub / logs (often very large snapshots)."""
    truncated = False
    if content is None:
        return {"format": "empty", "text": "", "truncated": False}
    if isinstance(content, str):
        s = content
        if len(s) > max_chars:
            s = s[:max_chars]
            truncated = True
        return {"format": "string", "text": s, "truncated": truncated}
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, dict):
                try:
                    parts.append(json.dumps(item, ensure_ascii=False)[:8000])
                except (TypeError, ValueError):
                    parts.append(str(item)[:8000])
            else:
                parts.append(str(item)[:8000])
        joined = "\n\n".join(parts)
        if len(joined) > max_chars:
            joined = joined[:max_chars]
            truncated = True
        return {"format": "blocks", "text": joined, "truncated": truncated}
    try:
        s = json.dumps(content, ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(content)
    if len(s) > max_chars:
        s = s[:max_chars]
        truncated = True
    return {"format": "json", "text": s, "truncated": truncated}


def form_trace_rows(tool_name: str, tool_input: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive human-readable rows for Form trace tab from Playwright-style tool inputs."""
    rows: list[dict[str, Any]] = []
    fields = tool_input.get("fields")
    if isinstance(fields, list):
        for i, f in enumerate(fields):
            if isinstance(f, dict):
                rows.append(
                    {
                        "tool": tool_name,
                        "index": i,
                        "field": f.get("name") or f.get("label") or f.get("ref") or str(f)[:120],
                        "detail": json.dumps(f, ensure_ascii=False)[:2000],
                    }
                )
            else:
                rows.append({"tool": tool_name, "index": i, "field": str(f)[:200], "detail": ""})
    else:
        rows.append(
            {
                "tool": tool_name,
                "summary": json.dumps(tool_input, ensure_ascii=False)[:4000],
            }
        )
    return rows
