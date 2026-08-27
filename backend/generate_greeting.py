"""Generate and cache the greeting PCM for the data_edge role.
Run this once to create backend/data/greetings/greeting_data_edge.pcm.
This ensures the greeting plays IMMEDIATELY when a call connects,
eliminating the 5-6 second silence gap that causes Vobiz to hang up."""

import asyncio
import sys
from pathlib import Path

# Add backend dir to path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.greeting_pcm import _generate_and_cache_greeting, greeting_pcm_paths
from config import settings
from loguru import logger


async def main():
    role = "data_edge"
    voice = settings.gemini_live_voice or settings.gemini_tts_voice or "Leda"
    greeting_text = (
        "Hi, this is Priya from Data Edge. I'm a career counselor — got a quick minute?"
    )

    logger.info(f"Generating greeting PCM for role={role} voice={voice}")
    logger.info(f"Greeting text: {greeting_text}")

    out_path, meta_path = greeting_pcm_paths(role)
    logger.info(f"Target path: {out_path}")

    result = await _generate_and_cache_greeting(role, greeting_text, voice)

    if result:
        pcm, sr = result
        logger.info(f"SUCCESS: Generated {len(pcm)} bytes @ {sr}Hz")
        logger.info(f"Saved to: {out_path}")
        logger.info(f"Metadata: {meta_path}")
    else:
        logger.error("FAILED: Greeting PCM generation returned no result")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
