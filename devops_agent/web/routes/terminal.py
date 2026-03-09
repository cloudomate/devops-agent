"""AI-assisted terminal WebSocket — devops/admin only.

Supports two actions:
  exec   → run a shell command, stream stdout/stderr line-by-line
  ask_ai → send recent terminal context + question to the agent, stream AI response
"""
from __future__ import annotations

import asyncio
import json
import os

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ...agent import run_agent_streaming
from ..oidc import get_current_user_ws

router = APIRouter()

# Commands with side-effects that require explicit confirmation from the user —
# block them here if they look destructive without a --force / -f flag.
_BLOCKED_PATTERNS = [
    "rm -rf /",
    ":(){ :|:& };:",  # fork bomb
]


def _is_safe(cmd: str) -> tuple[bool, str]:
    low = cmd.lower().strip()
    for pat in _BLOCKED_PATTERNS:
        if pat in low:
            return False, f"Blocked: '{pat}' detected"
    return True, ""


@router.websocket("/ws/terminal")
async def terminal_ws(websocket: WebSocket, token: str = ""):
    user = await get_current_user_ws(websocket, token)
    if not user or user.get("role") not in ("admin", "devops"):
        await websocket.close(code=4003, reason="Forbidden — devops/admin only")
        return

    await websocket.accept()

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            action = data.get("action", "exec")

            # ── exec: run a shell command ─────────────────────────────────────
            if action == "exec":
                cmd = data.get("cmd", "").strip()
                if not cmd:
                    continue

                safe, reason = _is_safe(cmd)
                if not safe:
                    await websocket.send_text(json.dumps({
                        "type": "error", "data": f"Blocked: {reason}\n",
                    }))
                    await websocket.send_text(json.dumps({"type": "exec_done", "rc": 1}))
                    continue

                await websocket.send_text(json.dumps({"type": "exec_start", "cmd": cmd}))

                try:
                    proc = await asyncio.create_subprocess_shell(
                        cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                        env={**os.environ, "TERM": "xterm-256color"},
                    )
                    async for line in proc.stdout:  # type: ignore[union-attr]
                        await websocket.send_text(json.dumps({
                            "type": "output",
                            "data": line.decode(errors="replace"),
                        }))
                    await proc.wait()
                    await websocket.send_text(json.dumps({
                        "type": "exec_done", "rc": proc.returncode,
                    }))
                except Exception as exc:
                    await websocket.send_text(json.dumps({
                        "type": "error", "data": f"{exc}\n",
                    }))
                    await websocket.send_text(json.dumps({"type": "exec_done", "rc": 1}))

            # ── ask_ai: AI analysis of terminal context ───────────────────────
            elif action == "ask_ai":
                context = data.get("context", "")[-8000:]  # cap context size
                question = data.get("question", "").strip() or "What should I do next?"

                history = [
                    {
                        "role": "system",
                        "content": (
                            "You are a senior DevOps/SRE assistant. "
                            "The user is working in a terminal. "
                            "Answer concisely and suggest the next command if helpful."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Recent terminal output:\n```\n{context}\n```\n\n"
                            f"Question: {question}"
                        ),
                    },
                ]

                await websocket.send_text(json.dumps({"type": "ai_start"}))
                try:
                    async for chunk in run_agent_streaming(
                        history,
                        persona="devops",
                        user=user,
                        project_names=[],
                        active_project=None,
                    ):
                        if isinstance(chunk, str):
                            await websocket.send_text(json.dumps({
                                "type": "ai_delta", "text": chunk,
                            }))
                        # tool events are not forwarded in terminal AI mode —
                        # keep it simple; the AI just gives text advice here
                except Exception as exc:
                    await websocket.send_text(json.dumps({
                        "type": "ai_delta", "text": f"\n[error: {exc}]",
                    }))
                await websocket.send_text(json.dumps({"type": "ai_done"}))

    except WebSocketDisconnect:
        pass
