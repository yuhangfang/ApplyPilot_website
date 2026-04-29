"""Website reader helpers for Hub: snapshot + screenshot + LLM field analysis."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
import base64

from applypilot import config
from applypilot.apply.chrome import BASE_CDP_PORT, cleanup_worker, launch_chrome
from applypilot.apply.launcher import _make_mcp_config
from applypilot.apply.observe import serialize_tool_result_content
from applypilot.discovery.smartextract import extract_json
from applypilot.llm import get_client

logger = logging.getLogger(__name__)
_reader_lock = threading.Lock()


def _emit_reader_timing(
    *,
    phase: str,
    ms: int | None = None,
    action: str = "done",
) -> None:
    """Push timing milestones to hub SSE while analyze runs (lazy import avoids cycles)."""
    try:
        from applypilot.apply.trace_server import broadcast_hub_event

        broadcast_hub_event(
            {
                "kind": "website_reader_timing",
                "phase": phase,
                "action": action,
                "ms": ms,
            }
        )
    except Exception:
        logger.debug("website reader timing broadcast failed", exc_info=True)


def _emit_reader_partial(
    *,
    snapshot_text: str | None = None,
    screenshot_base64: str | None = None,
    screenshot_mime: str | None = None,
) -> None:
    """Push partial Website Reader payloads to hub SSE for live rendering."""
    try:
        from applypilot.apply.trace_server import broadcast_hub_event

        event: dict[str, Any] = {"kind": "website_reader_partial"}
        if snapshot_text:
            event["snapshot_text"] = snapshot_text[:200_000]
        if screenshot_base64:
            event["screenshot_base64"] = screenshot_base64
            event["screenshot_mime"] = screenshot_mime or "image/png"
        if len(event) > 1:
            broadcast_hub_event(event)
    except Exception:
        logger.debug("website reader partial broadcast failed", exc_info=True)


_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})


def _guess_image_mime_from_bytes(raw: bytes) -> str:
    if len(raw) >= 8 and raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(raw) >= 3 and raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if len(raw) >= 6 and raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/png"


def _resolve_under_worker(worker_dir: Path, rel: str) -> Path | None:
    """Resolve a path emitted by MCP (often ./file.png or bare filename)."""
    r = rel.strip().strip('"').strip("'")
    if not r or r.startswith(("http://", "https://", "chrome://", "data:")):
        return None
    candidates: list[Path] = []
    if r.startswith("./"):
        candidates.append((worker_dir / r[2:]).resolve())
    else:
        candidates.append((worker_dir / r).resolve())
        if "/" not in r.replace("\\", "/"):
            candidates.append((worker_dir / ".playwright-mcp" / r).resolve())
    for p in candidates:
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None


def _find_latest_image_under(worker_dir: Path, *, min_bytes: int = 400) -> tuple[bytes, str]:
    """Newest image under worker dir (MCP often writes screenshots beside cwd)."""
    if not worker_dir.is_dir():
        return b"", "image/png"
    best: tuple[float, Path] | None = None
    try:
        for path in worker_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in _IMAGE_SUFFIXES:
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            if st.st_size < min_bytes:
                continue
            if best is None or st.st_mtime > best[0]:
                best = (st.st_mtime, path)
    except OSError:
        return b"", "image/png"
    if not best:
        return b"", "image/png"
    raw = best[1].read_bytes()
    return raw, _guess_image_mime_from_bytes(raw)


def _finalize_screenshot_b64(
    screenshot_base64: str,
    worker_dir: Path,
) -> tuple[str, str]:
    """Return (raw_base64, mime) safe for browser data URLs."""
    # Prefer decoding embedded / inlined base64; detect real MIME from bytes.
    s = (screenshot_base64 or "").strip()
    if s.startswith("data:"):
        if ";base64," in s:
            _, _, rest = s.partition(";base64,")
            s = rest.strip()
        else:
            s = ""

    raw: bytes | None = None
    if s:
        try:
            raw = base64.b64decode(s, validate=False)
        except Exception:
            raw = None
        if raw is not None and len(raw) < 32:
            raw = None

    if raw is None or len(raw) < 32:
        fb_raw, fb_mime = _find_latest_image_under(worker_dir)
        if fb_raw:
            return base64.standard_b64encode(fb_raw).decode("ascii"), fb_mime
        return "", "image/png"

    detected = _guess_image_mime_from_bytes(raw)
    return base64.standard_b64encode(raw).decode("ascii"), detected


_FIELD_SCAN_SCRIPT = """
() => {
  const controls = Array.from(document.querySelectorAll("input, textarea, select"));
  function clean(s) { return (s || "").toString().trim(); }
  const out = [];
  for (const el of controls) {
    const tag = (el.tagName || "").toLowerCase();
    const type = tag === "input" ? (el.getAttribute("type") || "text").toLowerCase() : tag;
    if (["hidden", "submit", "button", "image", "reset"].includes(type)) continue;
    const id = clean(el.id);
    const name = clean(el.getAttribute("name"));
    const placeholder = clean(el.getAttribute("placeholder"));
    const aria = clean(el.getAttribute("aria-label"));
    const required = !!(el.required || el.getAttribute("aria-required") === "true");
    let label = "";
    if (id) {
      const lb = document.querySelector(`label[for="${CSS.escape(id)}"]`);
      if (lb) label = clean(lb.textContent);
    }
    if (!label) {
      const parentLabel = el.closest("label");
      if (parentLabel) label = clean(parentLabel.textContent);
    }
    if (!label) {
      const row = el.closest("fieldset, .form-group, .field, .input, .question");
      if (row) {
        const hd = row.querySelector("legend, label, h1, h2, h3, h4, h5");
        if (hd) label = clean(hd.textContent);
      }
    }
    const options = [];
    if (tag === "select") {
      for (const opt of Array.from(el.querySelectorAll("option")).slice(0, 20)) {
        const t = clean(opt.textContent);
        if (t) options.push(t);
      }
    }
    out.push({
      tag,
      type,
      label,
      name,
      id,
      placeholder,
      aria_label: aria,
      required,
      options,
    });
  }
  return out.slice(0, 200);
}
"""


def _llm_field_suggestions(
    *,
    url: str,
    title: str,
    fields: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not fields:
        return [], {
            "prompt_chars": 0,
            "response_chars": 0,
            "est_prompt_tokens": 0,
            "est_response_tokens": 0,
        }
    fields_text = json.dumps(fields, ensure_ascii=False)[:16_000]
    prompt = f"""You are extracting visible form fields from a job application page.

Return STRICT JSON only:
{{
  "fields": [
    {{
      "field_label": "human-readable label",
      "field_type": "text|email|phone|date|select|checkbox|radio|number|textarea|file|other",
      "suggested_value": "",
      "why": "short context about what the field asks for",
      "confidence": 0.0,
      "visible": true
    }}
  ]
}}

Rules:
- Output max 60 fields in top-to-bottom visual order.
- Include only fields visibly rendered to humans.
- Leave suggested_value empty.
- Confidence must be between 0 and 1.

URL: {url}
Page title: {title}

Structured fields extracted from Playwright browser_snapshot:
{fields_text}
"""
    stats: dict[str, Any] = {
        "prompt_chars": len(prompt),
        "response_chars": 0,
        # Approximation; exact usage is not currently returned by our llm client API.
        "est_prompt_tokens": max(1, int(len(prompt) / 4)),
        "est_response_tokens": 0,
    }
    try:
        raw = get_client().ask(prompt, temperature=0.0, max_tokens=4096)
        stats["response_chars"] = len(raw)
        stats["est_response_tokens"] = max(1, int(len(raw) / 4))
        parsed = extract_json(raw)
        rows = parsed.get("fields")
        if isinstance(rows, list):
            cleaned: list[dict[str, Any]] = []
            for row in rows[:40]:
                if not isinstance(row, dict):
                    continue
                try:
                    confidence = float(row.get("confidence") or 0.0)
                except (TypeError, ValueError):
                    confidence = 0.0
                cleaned.append(
                    {
                        "field_label": str(row.get("field_label") or "").strip(),
                        "field_type": str(row.get("field_type") or "other").strip().lower(),
                        "suggested_value": "",
                        "why": str(row.get("why") or "").strip(),
                        "confidence": confidence,
                        "options": [str(x).strip() for x in (row.get("options") or []) if str(x).strip()]
                        if isinstance(row.get("options"), list)
                        else [],
                    }
                )
            return cleaned, stats
    except Exception:
        logger.exception("website reader LLM analysis failed")
    return [], stats


def _fallback_field_suggestions(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f in fields[:60]:
        opts = []
        if isinstance(f.get("options"), list):
            opts = [str(x).strip() for x in (f.get("options") or []) if str(x).strip()][:30]
        out.append(
            {
                "field_label": str(f.get("label") or f.get("name") or f.get("id") or "field"),
                "field_type": str(f.get("type") or "other"),
                "suggested_value": "",
                "why": "Visible field extracted from page snapshot.",
                "confidence": 0.5,
                "options": opts,
            }
        )
    return out


def _norm_text(s: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def _order_suggestions_by_page(
    suggestions: list[dict[str, Any]],
    dom_fields: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep suggestions in strict visual order (top-to-bottom DOM field order)."""
    if not suggestions:
        return suggestions

    dom_keys: list[tuple[int, str, str, str]] = []
    for i, f in enumerate(dom_fields or []):
        label = _norm_text(f.get("label"))
        name = _norm_text(f.get("name"))
        fid = _norm_text(f.get("id"))
        placeholder = _norm_text(f.get("placeholder"))
        aria = _norm_text(f.get("aria_label"))
        key = " ".join(p for p in [label, name, fid, placeholder, aria] if p).strip()
        if key or label:
            dom_keys.append((i, key, label, name))

    used: set[int] = set()
    ranked: list[tuple[int, int, dict[str, Any]]] = []

    for sidx, s in enumerate(suggestions):
        # Match by visible label semantics only (not "why"/type), then place by DOM index.
        skey = _norm_text(s.get("field_label") or s.get("label"))
        best_idx = -1
        best_score = -1
        if skey:
            skey_set = set(skey.split())
            for di, dkey, dlabel, dname in dom_keys:
                if di in used:
                    continue
                dset = set(dkey.split())
                # Prefer direct label containment/equality to keep visual order stable.
                score = 0
                if dlabel and (skey == dlabel or skey in dlabel or dlabel in skey):
                    score += 50
                if dname and (skey == dname or skey in dname or dname in skey):
                    score += 10
                score += len(skey_set & dset)
                if score > best_score:
                    best_score = score
                    best_idx = di
        if best_idx >= 0 and best_score > 0:
            used.add(best_idx)
            item = dict(s)
            if not (isinstance(item.get("options"), list) and item.get("options")):
                dopt = dom_fields[best_idx].get("options") if best_idx < len(dom_fields) else None
                if isinstance(dopt, list):
                    item["options"] = [str(x).strip() for x in dopt if str(x).strip()][:30]
            ranked.append((best_idx, sidx, item))
        # Drop unmatched suggestions so output stays aligned to visible DOM fields only.

    ranked.sort(key=lambda x: (x[0], x[1]))
    return [row[2] for row in ranked]


def _extract_image_base64(content: Any) -> str:
    if isinstance(content, dict):
        for k in ("data", "base64", "image_base64", "bytes_base64", "image"):
            v = content.get(k)
            if isinstance(v, str) and len(v) > 40:
                return v
        for v in content.values():
            got = _extract_image_base64(v)
            if got:
                return got
    if isinstance(content, list):
        for item in content:
            got = _extract_image_base64(item)
            if got:
                return got
    return ""


def _extract_tool_file_paths(content: Any) -> list[str]:
    """Extract relative file paths like ./page-snapshot.md from tool results."""
    paths: list[str] = []

    def push(text: str) -> None:
        if not text:
            return
        for m in re.finditer(r"\((\./[^)\s]+)\)", text):
            paths.append(m.group(1))
        for m in re.finditer(r"(?<!\()(\./[^\s)]+)", text):
            paths.append(m.group(1))
        for m in re.finditer(r"!\[[^\]]*\]\(([^)]+)\)", text):
            paths.append(m.group(1).strip())
        for m in re.finditer(r"\[[^\]]*\]\(([^)]+)\)", text):
            paths.append(m.group(1).strip())

    if isinstance(content, str):
        push(content)
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                for key in ("text", "path", "file", "url"):
                    v = item.get(key)
                    if isinstance(v, str):
                        push(v)
            elif isinstance(item, str):
                push(item)
    elif isinstance(content, dict):
        for v in content.values():
            if isinstance(v, str):
                push(v)
            elif isinstance(v, (list, dict)):
                for p in _extract_tool_file_paths(v):
                    paths.append(p)
    deduped: list[str] = []
    seen: set[str] = set()
    for p in paths:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    return deduped


def _read_snapshot_from_paths(worker_dir: Path, content: Any) -> str:
    for rel in _extract_tool_file_paths(content):
        if not rel.endswith(".md"):
            continue
        p = _resolve_under_worker(worker_dir, rel)
        if not p:
            continue
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            logger.debug("Failed reading snapshot file %s", p, exc_info=True)
    return ""


def _read_screenshot_from_paths(worker_dir: Path, content: Any) -> str:
    for rel in _extract_tool_file_paths(content):
        if not rel.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            continue
        p = _resolve_under_worker(worker_dir, rel)
        if not p:
            continue
        try:
            raw = p.read_bytes()
            if raw:
                return base64.standard_b64encode(raw).decode("ascii")
        except Exception:
            logger.debug("Failed reading screenshot file %s", p, exc_info=True)
    return ""


def _capture_via_prompt_playwright(target: str) -> dict[str, Any]:
    t_capture_start = time.perf_counter()
    _emit_reader_timing(phase="capture", action="start")
    worker_id = 90
    port = BASE_CDP_PORT + worker_id
    mcp_config_path = config.APP_DIR / f".mcp-website-reader-{worker_id}.json"
    mcp_config_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")
    worker_dir = config.APPLY_WORKER_DIR / f"website-reader-{worker_id}"
    worker_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "claude",
        "--model",
        "haiku",
        "-p",
        "--mcp-config",
        str(mcp_config_path),
        "--permission-mode",
        "bypassPermissions",
        "--no-session-persistence",
        "--output-format",
        "stream-json",
        "--verbose",
        "-",
    ]
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)

    prompt = f"""Use Playwright MCP browser tools to inspect this page: {target}

Requirements:
1) browser_navigate to the URL.
2) Run browser_snapshot once.
3) Run browser_take_screenshot with full-page behavior.
   - Prefer tool input that requests full page (for example fullPage/full_page = true if supported).
   - If the tool ignores that and captures only viewport, scroll to bottom once and take another screenshot so we capture lower sections too.
4) Extract form-like fields from the snapshot: label, type, name/id hints, required (best effort), options when obvious.
   - Include ONLY fields visibly present to a human in the rendered page.
   - Exclude hidden/internal/autofill-only/system fields and anything not visibly rendered.
   - Preserve strict visual order from top to bottom exactly as displayed on the page.
5) Final answer MUST be strict JSON only:
{{
  "url": "...",
  "title": "...",
  "fields": [{{"label":"...","type":"...","name":"...","id":"...","required":false,"options":[]}}]
}}
Max 120 fields. No markdown.
"""

    attach_ms: int | None = None
    coordination_ms: int | None = None
    navigation_ms: int | None = None
    t_attach_start = time.perf_counter()
    _emit_reader_timing(phase="attach", action="start")
    chrome_proc = launch_chrome(worker_id=worker_id, port=port, headless=True, initial_url=target)
    attach_ms = int((time.perf_counter() - t_attach_start) * 1000)
    _emit_reader_timing(phase="attach", ms=attach_ms, action="done")
    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(worker_dir),
        )
        if proc.stdin is None or proc.stdout is None:
            raise RuntimeError("Failed to start Claude process for website reader.")
        proc.stdin.write(prompt)
        proc.stdin.close()
        t_coord_start = time.perf_counter()
        _emit_reader_timing(phase="coordination", action="start")

        tool_name_by_id: dict[str, str] = {}
        snapshot_text = ""
        screenshot_base64 = ""
        snapshot_ms: int | None = None
        screenshot_ms: int | None = None
        t_snapshot_start: float | None = None
        t_screenshot_start: float | None = None
        nav_tool_ids: set[str] = set()
        t_nav_start: float | None = None
        assistant_text_parts: list[str] = []

        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg_type = msg.get("type")
            if msg_type == "assistant":
                blocks = msg.get("message", {}).get("content", []) or []
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    bt = block.get("type")
                    if bt == "tool_use":
                        tid = str(block.get("id") or "")
                        tname = str(block.get("name") or "")
                        if tid:
                            tool_name_by_id[tid] = tname
                        if coordination_ms is None:
                            coordination_ms = int((time.perf_counter() - t_coord_start) * 1000)
                            _emit_reader_timing(
                                phase="coordination", ms=coordination_ms, action="done"
                            )
                        lname = tname.lower()
                        if "navigate" in lname and tid:
                            nav_tool_ids.add(tid)
                            if t_nav_start is None:
                                t_nav_start = time.perf_counter()
                                _emit_reader_timing(phase="navigation", action="start")
                        if "snapshot" in lname and t_snapshot_start is None:
                            t_snapshot_start = time.perf_counter()
                            _emit_reader_timing(phase="snapshot", action="start")
                        if "screenshot" in lname and t_screenshot_start is None:
                            t_screenshot_start = time.perf_counter()
                            _emit_reader_timing(phase="screenshot", action="start")
                    elif bt == "text":
                        t = block.get("text")
                        if isinstance(t, str):
                            assistant_text_parts.append(t)
            elif msg_type == "user":
                blocks = msg.get("message", {}).get("content", []) or []
                for block in blocks:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    tool_use_id = str(block.get("tool_use_id") or "")
                    tool_name = tool_name_by_id.get(tool_use_id, "")
                    content = block.get("content")
                    if (
                        navigation_ms is None
                        and tool_use_id
                        and tool_use_id in nav_tool_ids
                        and t_nav_start is not None
                    ):
                        navigation_ms = int((time.perf_counter() - t_nav_start) * 1000)
                        _emit_reader_timing(phase="navigation", ms=navigation_ms, action="done")
                    if "snapshot" in tool_name and not snapshot_text:
                        ser = serialize_tool_result_content(content, max_chars=180_000)
                        snapshot_text = ser["text"]
                        if "./" in snapshot_text:
                            file_snapshot = _read_snapshot_from_paths(worker_dir, content)
                            if file_snapshot:
                                snapshot_text = file_snapshot
                        _emit_reader_partial(snapshot_text=snapshot_text)
                        if t_snapshot_start is not None:
                            snapshot_ms = int((time.perf_counter() - t_snapshot_start) * 1000)
                        else:
                            snapshot_ms = int((time.perf_counter() - t_capture_start) * 1000)
                        _emit_reader_timing(phase="snapshot", ms=snapshot_ms, action="done")
                    if "screenshot" in tool_name and not screenshot_base64:
                        screenshot_base64 = _read_screenshot_from_paths(worker_dir, content)
                        if not screenshot_base64:
                            screenshot_base64 = _extract_image_base64(content)
                        if screenshot_base64:
                            shot_inline, shot_inline_mime = _finalize_screenshot_b64(
                                screenshot_base64, worker_dir
                            )
                            if shot_inline:
                                screenshot_base64 = shot_inline
                                _emit_reader_partial(
                                    screenshot_base64=shot_inline,
                                    screenshot_mime=shot_inline_mime,
                                )
                            if t_screenshot_start is not None:
                                screenshot_ms = int((time.perf_counter() - t_screenshot_start) * 1000)
                            else:
                                screenshot_ms = int((time.perf_counter() - t_capture_start) * 1000)
                            _emit_reader_timing(phase="screenshot", ms=screenshot_ms, action="done")
            elif msg_type == "result":
                res = msg.get("result")
                if isinstance(res, str):
                    assistant_text_parts.append(res)

        proc.wait(timeout=180)
        joined = "\n".join(assistant_text_parts)
        parsed = extract_json(joined) if joined.strip() else {}
        fields = parsed.get("fields")
        if not isinstance(fields, list):
            fields = []
        capture_ms = int((time.perf_counter() - t_capture_start) * 1000)
        _emit_reader_timing(phase="capture", ms=capture_ms, action="done")
        clean_shot, shot_mime = _finalize_screenshot_b64(screenshot_base64, worker_dir)
        if clean_shot:
            _emit_reader_partial(screenshot_base64=clean_shot, screenshot_mime=shot_mime)
        if coordination_ms is None:
            coordination_ms = int((time.perf_counter() - t_coord_start) * 1000)
            _emit_reader_timing(phase="coordination", ms=coordination_ms, action="done")
        if t_nav_start is not None and navigation_ms is None:
            navigation_ms = int((time.perf_counter() - t_nav_start) * 1000)
            _emit_reader_timing(phase="navigation", ms=navigation_ms, action="done")
        return {
            "url": str(parsed.get("url") or target),
            "title": str(parsed.get("title") or ""),
            "fields": fields[:120],
            "snapshot_text": snapshot_text[:200_000],
            "screenshot_base64": clean_shot,
            "screenshot_mime": shot_mime,
            "timings": {
                "attach_ms": attach_ms,
                "coordination_ms": coordination_ms,
                "navigation_ms": navigation_ms,
                "snapshot_ms": snapshot_ms,
                "screenshot_ms": screenshot_ms,
                "capture_ms": capture_ms,
            },
        }
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        cleanup_worker(worker_id, chrome_proc)


def analyze_website(
    url: str,
) -> dict[str, Any]:
    """Read one website via prompt-driven Playwright MCP tool flow."""
    target = (url or "").strip()
    if not target:
        raise ValueError("url is required")
    if not target.startswith(("http://", "https://")):
        raise ValueError("url must start with http:// or https://")

    t_total_start = time.perf_counter()
    _emit_reader_timing(phase="total", action="start")
    with _reader_lock:
        captured = _capture_via_prompt_playwright(target)
    snapshot_text = str(captured.get("snapshot_text") or "")
    dom_fields = captured.get("fields") if isinstance(captured.get("fields"), list) else []
    current_url = str(captured.get("url") or target)
    title = str(captured.get("title") or "")

    t_llm_start = time.perf_counter()
    _emit_reader_timing(phase="llm", action="start")
    llm_fields, llm_stats = _llm_field_suggestions(
        url=current_url,
        title=title,
        fields=dom_fields,
    )
    llm_ms = int((time.perf_counter() - t_llm_start) * 1000)
    if not llm_fields:
        t_fb_start = time.perf_counter()
        llm_fields = _fallback_field_suggestions(dom_fields)
        llm_ms = int((time.perf_counter() - t_fb_start) * 1000)
    llm_fields = _order_suggestions_by_page(llm_fields, dom_fields)
    _emit_reader_timing(phase="llm", ms=llm_ms, action="done")
    total_ms = int((time.perf_counter() - t_total_start) * 1000)
    _emit_reader_timing(phase="total", ms=total_ms, action="done")

    return {
        "ok": True,
        "url": current_url,
        "requested_url": target,
        "title": title,
        "http_status": None,
        "snapshot_text": snapshot_text,
        "snapshot_source": "mcp_browser_snapshot",
        "dom_fields": dom_fields[:120],
        "llm_fields": llm_fields,
        "screenshot_base64": str(captured.get("screenshot_base64") or ""),
        "screenshot_mime": str(captured.get("screenshot_mime") or "image/png"),
        "screenshot_encoding": "base64",
        "timings": {
            "attach_ms": (captured.get("timings") or {}).get("attach_ms"),
            "coordination_ms": (captured.get("timings") or {}).get("coordination_ms"),
            "navigation_ms": (captured.get("timings") or {}).get("navigation_ms"),
            "snapshot_ms": (captured.get("timings") or {}).get("snapshot_ms"),
            "screenshot_ms": (captured.get("timings") or {}).get("screenshot_ms"),
            "capture_ms": (captured.get("timings") or {}).get("capture_ms"),
            "llm_ms": llm_ms,
            "total_ms": total_ms,
        },
        "llm_stats": llm_stats,
        "llm_sources": {"structured_fields": True},
    }


def refresh_llm_analysis(
    *,
    url: str,
    title: str,
    dom_fields: list[dict[str, Any]],
) -> dict[str, Any]:
    """Regenerate Website Reader field suggestions without recapturing page artifacts."""
    current_url = (url or "").strip()
    if not current_url:
        raise ValueError("url is required")
    fields = dom_fields if isinstance(dom_fields, list) else []
    t_llm_start = time.perf_counter()
    _emit_reader_timing(phase="llm", action="start")
    llm_fields, llm_stats = _llm_field_suggestions(
        url=current_url,
        title=title or "",
        fields=fields,
    )
    llm_ms = int((time.perf_counter() - t_llm_start) * 1000)
    if not llm_fields:
        t_fb_start = time.perf_counter()
        llm_fields = _fallback_field_suggestions(fields)
        llm_ms = int((time.perf_counter() - t_fb_start) * 1000)
    llm_fields = _order_suggestions_by_page(llm_fields, fields)
    _emit_reader_timing(phase="llm", ms=llm_ms, action="done")
    return {
        "ok": True,
        "url": current_url,
        "title": title or "",
        "llm_fields": llm_fields,
        "timings": {"llm_ms": llm_ms},
        "llm_stats": llm_stats,
        "llm_sources": {"structured_fields": True},
    }
