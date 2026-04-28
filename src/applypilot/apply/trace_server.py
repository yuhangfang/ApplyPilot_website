"""Localhost hub: web UI + SSE trace + REST APIs (127.0.0.1 only)."""

from __future__ import annotations

import json
import logging
import queue
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from applypilot.apply.observe import (
    NoOpApplyObserver,
    form_trace_rows,
    normalize_tool_name,
    serialize_tool_result_content,
)

logger = logging.getLogger(__name__)

_sub_lock = threading.Lock()
_subscribers: list[queue.Queue[str]] = []

_hub_server: ThreadingHTTPServer | None = None
_hub_port: int | None = None
_hub_thread: threading.Thread | None = None

_apply_lock = threading.Lock()
_pipeline_lock = threading.Lock()


def broadcast_hub_event(event: dict[str, Any]) -> None:
    """Publish one JSON event to all SSE clients."""
    line = json.dumps(event, ensure_ascii=False)
    with _sub_lock:
        for q in list(_subscribers):
            try:
                q.put_nowait(line)
            except queue.Full:
                pass


class HubApplyObserver(NoOpApplyObserver):
    """Stream apply events to the hub SSE subscribers."""

    def on_job_prompt(self, worker_id: int, job_title: str, job_url: str, prompt: str) -> None:
        preview = prompt if len(prompt) <= 120_000 else prompt[:120_000]
        broadcast_hub_event(
            {
                "kind": "job_prompt",
                "worker_id": worker_id,
                "job_title": job_title,
                "job_url": job_url,
                "prompt_preview": preview,
            }
        )

    def on_raw_ndjson(self, worker_id: int, line: str) -> None:
        broadcast_hub_event({"kind": "raw", "worker_id": worker_id, "line": line[:16_000]})

    def on_assistant_text(self, worker_id: int, text: str) -> None:
        broadcast_hub_event({"kind": "assistant_text", "worker_id": worker_id, "text": text})

    def on_tool_use(self, worker_id: int, tool_name: str, tool_input: dict[str, Any]) -> None:
        short = normalize_tool_name(tool_name)
        broadcast_hub_event(
            {
                "kind": "tool_use",
                "worker_id": worker_id,
                "tool_name": short,
                "tool_input": tool_input,
            }
        )
        try:
            rows = form_trace_rows(short, tool_input)
            if rows and tool_input.get("fields"):
                broadcast_hub_event({"kind": "form_rows", "worker_id": worker_id, "rows": rows})
        except Exception:
            logger.debug("form_trace_rows failed", exc_info=True)
        nav_url = tool_input.get("url") if isinstance(tool_input, dict) else None
        if nav_url and isinstance(nav_url, str) and "navigate" in short.lower():
            broadcast_hub_event(
                {"kind": "browser_navigate", "worker_id": worker_id, "url": nav_url[:8000]}
            )

    def on_tool_result(
        self,
        worker_id: int,
        tool_use_id: str,
        content: Any,
        is_error: bool,
    ) -> None:
        ser = serialize_tool_result_content(content)
        broadcast_hub_event(
            {
                "kind": "tool_result",
                "worker_id": worker_id,
                "tool_use_id": tool_use_id,
                "is_error": is_error,
                "content_format": ser["format"],
                "content_preview": ser["text"],
                "truncated": ser["truncated"],
            }
        )

    def on_user_message_text(self, worker_id: int, text: str) -> None:
        preview = text if len(text) <= 120_000 else text[:120_000]
        broadcast_hub_event(
            {"kind": "user_message_text", "worker_id": worker_id, "text": preview}
        )

    def on_assistant_usage(self, worker_id: int, usage: dict[str, Any]) -> None:
        broadcast_hub_event(
            {"kind": "assistant_usage", "worker_id": worker_id, "usage": dict(usage)}
        )

    def on_stream_result(self, worker_id: int, message: dict[str, Any]) -> None:
        u = message.get("usage") or {}
        usage = {
            "input_tokens": u.get("input_tokens", 0),
            "output_tokens": u.get("output_tokens", 0),
            "cache_read_input_tokens": u.get("cache_read_input_tokens", 0),
            "cache_creation_input_tokens": u.get("cache_creation_input_tokens", 0),
            "total_cost_usd": message.get("total_cost_usd", 0),
            "num_turns": message.get("num_turns", 0),
            "duration_ms": message.get("duration_ms"),
            "duration_api_ms": message.get("duration_api_ms"),
        }
        broadcast_hub_event({"kind": "stream_result", "worker_id": worker_id, "usage": usage})


_default_hub_observer: HubApplyObserver | None = None


def get_hub_apply_observer() -> HubApplyObserver:
    global _default_hub_observer
    if _default_hub_observer is None:
        _default_hub_observer = HubApplyObserver()
    return _default_hub_observer


def load_hub_html() -> str:
    return Path(__file__).with_name("hub.html").read_text(encoding="utf-8")


def _stats_dict() -> dict[str, Any]:
    from applypilot.database import get_connection, get_stats

    s = get_stats(get_connection())
    out: dict[str, Any] = {}
    for k, v in s.items():
        if k == "by_site":
            out[k] = [[a, b] for a, b in v]
        elif k == "score_distribution":
            out[k] = [[a, b] for a, b in v]
        else:
            out[k] = v
    return out


def _register_sub() -> queue.Queue[str]:
    q: queue.Queue[str] = queue.Queue(maxsize=4000)
    with _sub_lock:
        _subscribers.append(q)
    return q


def _unregister_sub(q: queue.Queue[str]) -> None:
    with _sub_lock:
        try:
            _subscribers.remove(q)
        except ValueError:
            pass


class HubRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug("%s - %s", self.address_string(), fmt % args)

    def _host_ok(self) -> bool:
        h = (self.headers.get("Host") or "").split(":")[0].lower()
        return h in ("127.0.0.1", "localhost")

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, obj: Any) -> None:
        b = json.dumps(obj).encode("utf-8")
        self._send(code, b, "application/json; charset=utf-8")

    def _read_body(self, max_len: int = 25_000_000) -> bytes:
        n = int(self.headers.get("Content-Length", "0") or "0")
        if n > max_len:
            raise ValueError("body too large")
        if n <= 0:
            return b""
        return self.rfile.read(n)

    def do_GET(self) -> None:
        if not self._host_ok():
            self.send_error(403)
            return
        path = urlparse(self.path).path
        try:
            if path == "/":
                self._send(200, load_hub_html().encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/events":
                self._handle_sse()
            elif path == "/api/stats":
                self._send_json(200, _stats_dict())
            elif path == "/api/profile":
                from applypilot.profile_store import JsonProfileStore

                data = JsonProfileStore().load_profile()
                self._send_json(200, data if data else {})
            elif path == "/api/searches":
                from applypilot.profile_store import load_searches_text

                self._send(200, load_searches_text().encode("utf-8"), "text/plain; charset=utf-8")
            elif path == "/api/env-status":
                from applypilot.config import llm_credentials_configured
                from applypilot.profile_store import env_key_status

                status = env_key_status()
                status["llm_configured"] = llm_credentials_configured()
                self._send_json(200, status)
            elif path == "/api/doctor":
                from applypilot.doctor_report import collect_doctor_report, doctor_tier_summary

                checks = [c.to_dict() for c in collect_doctor_report()]
                self._send_json(200, {"checks": checks, "tier": doctor_tier_summary()})
            elif path == "/api/resume-status":
                from applypilot.profile_store import resume_paths_status

                self._send_json(200, resume_paths_status())
            elif path == "/api/dashboard-html":
                from applypilot.view import generate_dashboard_html

                html = generate_dashboard_html()
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            else:
                self.send_error(404)
        except Exception as e:
            logger.exception("hub GET error")
            self._send_json(500, {"error": str(e)})

    def _handle_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = _register_sub()
        try:
            while True:
                try:
                    item = q.get(timeout=20)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(f"data: {item}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            _unregister_sub(q)

    def do_POST(self) -> None:
        if not self._host_ok():
            self.send_error(403)
            return
        path = urlparse(self.path).path
        try:
            if path == "/api/profile":
                body = self._read_body(5_000_000)
                data = json.loads(body.decode("utf-8"))
                from applypilot.profile_store import JsonProfileStore

                JsonProfileStore().save_profile(data)
                self._send_json(200, {"ok": True})
            elif path == "/api/searches":
                text = self._read_body(5_000_000).decode("utf-8")
                from applypilot.profile_store import save_searches_text

                save_searches_text(text)
                self._send_json(200, {"ok": True})
            elif path == "/api/resume":
                qs = urlparse(self.path).query
                kind = "txt"
                for part in qs.split("&"):
                    if part.startswith("kind="):
                        kind = part.split("=", 1)[1].strip() or "txt"
                raw = self._read_body(6_000_000)
                from applypilot.profile_store import save_resume_bytes

                save_resume_bytes(kind, raw)
                self._send_json(200, {"ok": True, "kind": kind})
            elif path == "/api/pipeline/run":
                payload = json.loads(self._read_body(1_000_000).decode("utf-8"))

                def _run() -> None:
                    with _pipeline_lock:
                        broadcast_hub_event({"kind": "pipeline", "phase": "start", "payload": payload})
                        try:
                            from applypilot.config import get_tier, load_env, ensure_dirs
                            from applypilot.database import init_db
                            from applypilot.pipeline import run_pipeline

                            load_env()
                            ensure_dirs()
                            init_db()
                            if get_tier() < 2:
                                broadcast_hub_event(
                                    {
                                        "kind": "pipeline",
                                        "phase": "error",
                                        "message": "Tier 2 required (LLM API key). Run applypilot doctor.",
                                    }
                                )
                                return
                            stages = payload.get("stages") or ["all"]
                            result = run_pipeline(
                                stages=stages,
                                min_score=int(payload.get("min_score", 7)),
                                dry_run=bool(payload.get("dry_run")),
                                stream=bool(payload.get("stream")),
                                workers=int(payload.get("workers", 1)),
                                validation_mode=str(payload.get("validation", "normal")),
                            )
                            broadcast_hub_event(
                                {"kind": "pipeline", "phase": "done", "errors": result.get("errors")}
                            )
                        except Exception as e:
                            logger.exception("pipeline from hub")
                            broadcast_hub_event({"kind": "pipeline", "phase": "error", "message": str(e)})

                threading.Thread(target=_run, daemon=True).start()
                self._send_json(202, {"ok": True, "message": "pipeline started"})
            elif path == "/api/apply/session":
                payload = json.loads(self._read_body(256_000).decode("utf-8"))

                def _apply() -> None:
                    with _apply_lock:
                        try:
                            from applypilot.apply.launcher import run_hub_apply_session

                            run_hub_apply_session(
                                target_url=str(payload.get("url") or "").strip(),
                                min_score=int(payload.get("min_score", 7)),
                                headless=bool(payload.get("headless")),
                                model=str(payload.get("model") or "haiku"),
                                dry_run=bool(payload.get("dry_run")),
                                test_form=bool(payload.get("test_form")),
                            )
                        except Exception as e:
                            logger.exception("apply from hub")
                            broadcast_hub_event({"kind": "apply_session_end", "error": str(e), "applied": 0, "failed": 1})

                threading.Thread(target=_apply, daemon=True).start()
                self._send_json(202, {"ok": True, "message": "apply session started"})
            elif path == "/api/apply/stop":
                from applypilot.apply.launcher import request_hub_apply_stop

                request_hub_apply_stop()
                self._send_json(200, {"ok": True, "message": "stop requested"})
            else:
                self.send_error(404)
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
        except Exception as e:
            logger.exception("hub POST error")
            self._send_json(500, {"error": str(e)})


def start_hub_background(open_browser: bool = True) -> int:
    """Start hub in a daemon thread if not already running; return port."""
    from applypilot.config import warn_missing_llm_credentials

    global _hub_server, _hub_port, _hub_thread
    if _hub_port is not None and _hub_server is not None:
        if open_browser:
            webbrowser.open(f"http://127.0.0.1:{_hub_port}/")
        return _hub_port

    warn_missing_llm_credentials(hub=True)

    bind: tuple[str, int] = ("127.0.0.1", 0)
    httpd = ThreadingHTTPServer(bind, HubRequestHandler)
    _hub_server = httpd
    _hub_port = httpd.server_address[1]

    def _serve() -> None:
        try:
            httpd.serve_forever()
        except Exception:
            logger.exception("hub server stopped")

    _hub_thread = threading.Thread(target=_serve, name="applypilot-hub", daemon=True)
    _hub_thread.start()
    if open_browser:
        webbrowser.open(f"http://127.0.0.1:{_hub_port}/")
    return _hub_port


def run_hub_forever(port: int = 0, open_browser: bool = True) -> None:
    """Block serving the hub (use for `applypilot hub`). Ctrl+C to stop."""
    from applypilot.config import warn_missing_llm_credentials

    warn_missing_llm_credentials(hub=True)

    bind: tuple[str, int] = ("127.0.0.1", port if port > 0 else 0)
    httpd = ThreadingHTTPServer(bind, HubRequestHandler)
    p = httpd.server_address[1]
    from rich.console import Console

    Console().print(f"[green]ApplyPilot hub[/green] http://127.0.0.1:{p}/  [dim]Ctrl+C to stop[/dim]")
    if open_browser:
        webbrowser.open(f"http://127.0.0.1:{p}/")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
