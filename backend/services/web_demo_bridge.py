"""Web Demo Bridge: Directly connect browser audio to Gemini Live.
Allows testing the prompted AI through the web dashboard.
"""

import asyncio
import base64
import json
from typing import Any, Callable, Optional
from fastapi import WebSocket
from loguru import logger
import websockets as ws_client

from config import settings
from core.state import get_state
from services.sandbox_manager import get_agent
from services.conversation_log import (
    append_session_meta,
    append_turn,
    new_session_id,
)

async def handle_web_voice_demo(
    ws: WebSocket,
    role: str = "data_edge",
    agent_id: Optional[str] = None,
) -> None:
    """Bridge Browser <-> Gemini Live.
    Resolves prompt/voice based on role or agent_id.
    """
    await ws.accept()
    
    # 1. Resolve Config
    system_prompt = ""
    voice = "Puck"
    
    if role == "factory" and agent_id:
        agent = get_agent(agent_id)
        if agent:
            system_prompt = agent.get("prompt", "")
            voice = agent.get("voice", "Puck")
        else:
            logger.error(f"Test Bridge: Agent {agent_id} not found")
            await ws.close(code=4004)
            return
    else:
        state = get_state(role)
        system_prompt = state.get("prompt", "")
        # Fallback to default prompt if empty
        if not system_prompt:
            from prompts.priya import get_system_prompt
            system_prompt = get_system_prompt()
            
        # Add static RAG if available
        rag = state.get("rag", "")
        if rag:
            system_prompt += f"\n[Knowledge Base]\n{rag}\n"
            
        # Voice (hardcoded for now or from settings)
        voice = settings.gemini_live_voice

    api_key = settings.gemini_api_key
    model = settings.gemini_live_model

    session_id = new_session_id("web-demo")
    
    # Log dir mapping
    from core.state import _get_role_path
    log_dir = str(_get_role_path(role, "logs"))

    append_session_meta(
        session_id,
        "web-demo",
        path="web_voice",
        model=model,
        base_dir=log_dir
    )

    gemini_url = (
        f"wss://generativelanguage.googleapis.com/ws/"
        f"google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
        f"?key={api_key}"
    )

    try:
        async with ws_client.connect(
            gemini_url,
            max_size=2 * 1024 * 1024,
            ping_interval=10,
            close_timeout=2,
        ) as gws:
            # Setup Config
            setup_msg = {
                "setup": {
                    "model": f"models/{model}",
                    "generationConfig": {
                        "responseModalities": ["audio"],
                        "speechConfig": {
                            "voiceConfig": {
                                "prebuiltVoiceConfig": {"voiceName": voice}
                            },
                            "languageCode": settings.gemini_live_language_code,
                        },
                    },
                    "systemInstruction": {"parts": [{"text": system_prompt}]},
                }
            }
            await gws.send(json.dumps(setup_msg))
            
            # Wait for setup complete
            try:
                resp = await asyncio.wait_for(gws.recv(), timeout=10.0)
                logger.info("Gemini Live Web Demo: Setup complete")
            except Exception as e:
                logger.error(f"Gemini Live Web Demo: Setup timeout/error: {e}")
                await ws.close()
                return

            # Send silence kick to activate VAD
            try:
                from services.vobiz_bridge.gemini_protocol import gemini_send_pcm_silence_kick
                await gemini_send_pcm_silence_kick(gws, duration_ms=200)
            except Exception:
                pass

            async def gemini_to_browser():
                try:
                    async for msg in gws:
                        data = json.loads(msg)
                        if "serverContent" in data:
                            content = data["serverContent"]
                            if "modelTurn" in content:
                                parts = content["modelTurn"]["parts"]
                                for p in parts:
                                    if "inlineData" in p:
                                        audio_b64 = p["inlineData"]["data"]
                                        await ws.send_json({"type": "audio", "data": audio_b64})
                            
                            if content.get("turnComplete"):
                                await ws.send_json({"type": "status", "event": "turn_complete"})

                        if data.get("interrupted"):
                            await ws.send_json({"type": "interrupted"})
                except Exception as e:
                    logger.debug(f"Gemini->Browser closed: {e}")

            async def browser_to_gemini():
                try:
                    while True:
                        msg = await ws.receive_json()
                        if msg["type"] == "audio":
                            gemini_msg = {
                                "realtimeInput": {
                                    "audio": {
                                        "data": msg["data"],
                                        "mimeType": "audio/pcm;rate=16000",
                                    }
                                }
                            }
                            await gws.send(json.dumps(gemini_msg))
                except Exception as e:
                    logger.debug(f"Browser->Gemini closed: {e}")

            await asyncio.gather(gemini_to_browser(), browser_to_gemini())
    except Exception as e:
        logger.error(f"Web Bridge Connection Error: {e}")
    finally:
        try: await ws.close()
        except: pass
