"""WebSocket chat endpoint — streams agent responses to the browser."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ...agent import run_agent_streaming
from ...database import get_session_history, list_projects_for_user, save_message
from ..oidc import get_current_user_ws

router = APIRouter()

# Cloudflare closes idle WebSockets after ~100 s. Send a keepalive every 20 s.
_KEEPALIVE_INTERVAL = 20


@router.websocket("/ws/chat/{session_id}")
async def chat_ws(websocket: WebSocket, session_id: str, token: str = ""):
    user = await get_current_user_ws(websocket, token)
    if not user:
        print(f"[WS] AUTH FAIL session={session_id}", flush=True)
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await websocket.accept()
    print(f"[WS] ACCEPTED session={session_id} user={user.get('username')} role={user.get('role')}", flush=True)

    persona = "devops" if user["role"] in ("admin", "devops") else "developer"

    accessible = list_projects_for_user(user["id"], user["role"])
    project_names = [p["name"] for p in accessible]

    async def keepalive(stop: asyncio.Event) -> None:
        """Send a ping frame every _KEEPALIVE_INTERVAL seconds until stop is set."""
        while not stop.is_set():
            try:
                await asyncio.wait_for(asyncio.shield(stop.wait()), timeout=_KEEPALIVE_INTERVAL)
            except asyncio.TimeoutError:
                try:
                    await websocket.send_text(json.dumps({"type": "ping"}))
                except Exception:
                    break

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)

            # Ignore pong replies from client
            if data.get("type") == "pong":
                continue

            user_text = data.get("message", "").strip()
            if not user_text:
                continue

            active_project = data.get("project") or None
            print(f"[WS] MSG session={session_id} project={active_project} text={user_text[:80]!r}", flush=True)

            save_message(session_id, "user", user_text)
            history = get_session_history(session_id)

            # Start keepalive task while agent is running
            stop_ping = asyncio.Event()
            ping_task = asyncio.create_task(keepalive(stop_ping))

            text_chunks: list[str] = []
            try:
                async for chunk in run_agent_streaming(
                    history, persona=persona, user=user,
                    project_names=project_names, active_project=active_project,
                ):
                    if isinstance(chunk, dict):
                        await websocket.send_text(json.dumps(chunk))
                    else:
                        text_chunks.append(chunk)
                        await websocket.send_text(json.dumps({"type": "delta", "text": chunk}))
            except Exception as exc:
                import traceback
                print(f"[WS] AGENT ERROR session={session_id}: {exc}", flush=True)
                traceback.print_exc()
                try:
                    await websocket.send_text(json.dumps({
                        "type": "delta",
                        "text": f"\n\n⚠️ Agent error: {exc}",
                    }))
                except Exception:
                    pass
            finally:
                stop_ping.set()
                ping_task.cancel()
                try:
                    await ping_task
                except asyncio.CancelledError:
                    pass

            full_response = "".join(text_chunks)
            save_message(session_id, "assistant", full_response)
            try:
                await websocket.send_text(json.dumps({"type": "done"}))
            except Exception:
                pass

    except (WebSocketDisconnect, RuntimeError):
        print(f"[WS] CLOSED session={session_id}", flush=True)
    except Exception as exc:
        import traceback
        print(f"[WS] UNEXPECTED ERROR session={session_id}: {exc}", flush=True)
        traceback.print_exc()
