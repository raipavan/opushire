"""Live WebSocket session: Vobiz media ↔ Gemini Live."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import time
from typing import Any, Optional

import websockets as ws_client
from fastapi import WebSocket
from loguru import logger

from config import settings
from services.call_recording import CallRecorder
from services.conversation_log import (
    append_artifact,
    append_session_meta,
    append_turn,
    new_session_id,
)

from .audio import (
    load_background_audio,
    mix_voice_and_background_tick,
    pcm_resample,
    pop_l16_chunk,
    resample_24k_to_16k_numpy,
    send_play_audio,
    send_play_audio_batched,
)
from .constants import GEMINI_OUT_SR, VOBIZ_SR
from .gemini_protocol import (
    GEMINI_LIVE_URL_TMPL,
    build_gemini_live_url_and_headers,
    build_live_setup,
    gemini_send_live_opening_turn_nudge,
    gemini_send_live_rag,
    gemini_send_pcm_silence_kick,
    gemini_send_retry_nudge,
    gemini_send_silence_prompt,
)
from .turn_taking_addon import apply_live_voice_turn_addon
from .paths import backend_dir
from .session_state import VobizSessionState
from .telephony import terminate_call, vobiz_send_clear_audio
from .vobiz_client import extract_vobiz_start_numbers

# Track fire-and-forfire-and-forget background tasks so they aren't garbage-collected mid-flight.
_background_tasks: set[asyncio.Task] = set()

# Cache for static system prompt blocks (persona anchors, pacing, context rules).
# These are identical for every call to the same role — no need to rebuild them.
_STATIC_PROMPT_CACHE: dict[str, str] = {}


def _get_static_prompt_blocks(role: str) -> str:
    """Return cached static prompt blocks for the given role."""
    cached = _STATIC_PROMPT_CACHE.get(role)
    if cached is not None:
        return cached

    _PERSONA_ANCHORS: dict[str, str] = {
        "data_edge": (
            "[ANCHOR — HIGHEST PRIORITY, OVERRIDES CONFLICTING LINES BELOW]\n"
            "You are **Priya**, a **career counselor** at **Data Edge** (education & career skilling — "
            "data analytics, AI, cyber security, tech paths).\n"
            "NEVER mention Tirupati, Andhra Pradesh, real estate, CPR COSMOS, or property investment.\n"
            "If the user asks your name, say **Priya**.\n"
            "Your opening line on this call: \"Hi, this is Priya from Data Edge. Got a quick minute?\"\n\n"
        ),
    }
    anchor = _PERSONA_ANCHORS.get(role, "")

    pacing_rule = (
        "\n[VOICE & PACING — APPLIES TO EVERY SPOKEN REPLY]\n"
        "Speak at a natural conversational pace — neither slow nor rushed. "
        "Diction is warm, natural Indian English, friendly and conversational. "
        "When Hindi or Hinglish appears, pronounce it as a native Indian speaker "
        "would.\n\n"
    )

    context_rules = (
        "\n[USING THE CALLEE'S CONTEXT — APPLIES TO EVERY CALL]\n"
        "The block titled 'CURRENT CALL DETAILS' below carries facts about the *exact* "
        "person on this PSTN leg (name, phone, company, email, plus any extra fields "
        "the operator uploaded — like an RFQ subject, product, quantity, last quote, "
        "city, industry, notes, etc.).\n"
        "Rules for using it:\n"
        "1. Treat that block as ground truth for who you are speaking to. Do NOT "
        "invent fields that are not present, and do NOT confuse it with example "
        "scripts elsewhere in the prompt.\n"
        "2. Reference fields *naturally* in conversation when they help — e.g. "
        "'I'm calling about the {RFQ Subject} you sent us' or 'I see you're with "
        "{Company} in {City}'. Do NOT read out the whole list.\n"
        "3. Use the callee's first name only 2–3 times in the entire call — never "
        "after every sentence.\n"
        "4. Never read out the email address, phone number, or any internal IDs "
        "unless the user explicitly asks for them.\n"
        "5. If a field is empty or missing, simply skip it. Don't say 'I don't "
        "have that information' unless the user asked.\n"
        "6. If the user contradicts a field (e.g. 'that's not my company anymore'), "
        "trust them, apologize briefly, and continue.\n\n"
    )

    blocks = (anchor + pacing_rule + context_rules) if anchor else (pacing_rule + context_rules)
    _STATIC_PROMPT_CACHE[role] = blocks
    return blocks


class LatencyTracker:
    """Lightweight per-turn latency instrumentation for Gemini Live voice pipeline.

    Tracks timestamps for each pipeline stage and logs a summary when the turn completes.
    All times are ``time.perf_counter()`` for high-resolution monotonic measurement.
    """

    __slots__ = (
        "_turn_start", "_audio_received", "_gemini_forwarded",
        "_stt_text", "_activity_end", "_first_model_audio",
        "_first_vobiz_send", "_turn_logged",
    )

    def __init__(self) -> None:
        self._turn_start: float = 0.0
        self._audio_received: float = 0.0
        self._gemini_forwarded: float = 0.0
        self._stt_text: float = 0.0
        self._activity_end: float = 0.0
        self._first_model_audio: float = 0.0
        self._first_vobiz_send: float = 0.0
        self._turn_logged: bool = True

    def on_turn_start(self) -> None:
        self._turn_start = time.perf_counter()
        self._audio_received = 0.0
        self._gemini_forwarded = 0.0
        self._stt_text = 0.0
        self._activity_end = 0.0
        self._first_model_audio = 0.0
        self._first_vobiz_send = 0.0
        self._turn_logged = False

    def on_audio_received(self) -> None:
        if not self._audio_received:
            self._audio_received = time.perf_counter()

    def on_gemini_forwarded(self) -> None:
        if not self._gemini_forwarded:
            self._gemini_forwarded = time.perf_counter()

    def on_stt_text(self) -> None:
        if not self._stt_text:
            self._stt_text = time.perf_counter()

    def on_activity_end(self) -> None:
        if not self._activity_end:
            self._activity_end = time.perf_counter()

    def on_first_model_audio(self) -> None:
        if not self._first_model_audio:
            self._first_model_audio = time.perf_counter()

    def on_first_vobiz_send(self) -> None:
        if not self._first_vobiz_send:
            self._first_vobiz_send = time.perf_counter()

    def log_turn_summary(self, logger_ref) -> None:
        if self._turn_logged:
            return
        self._turn_logged = True
        now = time.perf_counter()
        ts = self._turn_start
        if ts <= 0:
            return
        def _ms(t: float) -> float:
            return (t - ts) * 1000.0 if t > 0 else -1.0
        total = (now - ts) * 1000.0
        logger_ref.info(
            "LATENCY | total={:.0f}ms | audio_rx={:.0f}ms fwd={:.0f}ms stt={:.0f}ms "
            "activity_end={:.0f}ms model_audio={:.0f}ms vobiz_tx={:.0f}ms",
            total,
            _ms(self._audio_received),
            _ms(self._gemini_forwarded),
            _ms(self._stt_text),
            _ms(self._activity_end),
            _ms(self._first_model_audio),
            _ms(self._first_vobiz_send),
        )

async def handle_vobiz_ws_live(
    ws: WebSocket,
    camp_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    inbound_role: Optional[str] = None,
    manual_role: Optional[str] = None,
) -> None:
    """Bridge Vobiz <-> Gemini Live (native audio). Low-latency path.

    Resolves configuration from one of several sources, in priority order:
      1. ``_CAMPAIGN_DATA[camp_id]`` — outbound campaign call (we know the lead).
      2. ``agent_id`` — sandbox / factory agent (recovered from DB).
      3. ``inbound_role`` — incoming PSTN call where the dialed DID has been
         mapped to a role by ``main.vobiz_answer`` via ``?role=...``. The
         caller is unknown; we just use that role's persona + KB.
      4. ``camp_id`` starting with ``manual_{role}`` — Make a Call / manual dial.
      5. ``manual_role`` query param — same routing as (4) when ``camp_id`` is
         missing or stripped by the carrier; pair with ``camp_id=manual_*`` when possible.
    """
    from core.state import _CAMPAIGN_DATA, get_state, _get_role_path, normalize_console_role
    from core.utils import _build_opening_line

    await ws.accept()
    live_log_id: str = ""
    logger.info(
        "Vobiz WebSocket accepted for camp={} agent={} inbound_role={} manual_role={}",
        camp_id, agent_id, inbound_role, manual_role,
    )

    # Single ingress queue so we can play scripted PCM before opening Gemini Live while
    # still buffering carrier events (see drain_scripted_opening_before_live_connect).
    _vobiz_incoming: asyncio.Queue[Optional[str]] = asyncio.Queue(maxsize=4096)

    async def _vobiz_ws_reader_task() -> None:
        try:
            while True:
                t = await ws.receive_text()
                await _vobiz_incoming.put(t)
        except Exception as exc:
            logger.warning("Vobiz WS reader stopped: {}", exc)
        finally:
            try:
                await _vobiz_incoming.put(None)
            except Exception as exc:
                logger.debug("Vobiz incoming queue close failed: {}", exc)

    _task = asyncio.create_task(_vobiz_ws_reader_task())
    _background_tasks.add(_task)
    _task.add_done_callback(_background_tasks.discard)

    # 1. Resolve Configuration
    system_prompt = ""
    voice = settings.gemini_live_voice
    opening_line = settings.vobiz_opening_line_default
    role = "data_edge"
    log_dir = str(_get_role_path("data_edge", "logs"))
    recording_dir = settings.call_recording_dir
    api_key = settings.gemini_api_key
    model = settings.gemini_live_model
    language_code = settings.gemini_live_language_code

    # ── Latency opt: start Gemini WS connect EARLY ──────────────────────
    # The TCP+TLS+WS handshake to generativelanguage.googleapis.com takes
    # 100ms–3s and is pure network I/O.  Start it now so it overlaps with
    # all the DB lookups, config resolution, and greeting-text logic below.
    _opening_t0 = time.perf_counter()
    _gemini_url, _gemini_extra_headers = build_gemini_live_url_and_headers(api_key)
    _gemini_ws_connect_task: Optional[asyncio.Task] = asyncio.create_task(
        ws_client.connect(
            _gemini_url,
            max_size=2 * 1024 * 1024,
            ping_interval=10,
            close_timeout=2,
            extra_headers=_gemini_extra_headers,
        ).__aenter__()
    )
    logger.info("OPENING_TIMING | gemini_ws_connect_started | +0ms")

    campaign_role = None
    data = None
    if camp_id:
        if camp_id in _CAMPAIGN_DATA:
            data = _CAMPAIGN_DATA[camp_id]
        else:
            try:
                from core.storage import lead_row_by_call_id
                db_lead = await lead_row_by_call_id(camp_id)
                if db_lead:
                    logger.info("Vobiz WS: Recovered campaign lead from database for camp_id={}", camp_id)
                    data = dict(db_lead)
                    data["_role"] = db_lead.get("role")
                    data["_lead_id"] = db_lead.get("id")
            except Exception as e:
                logger.warning("Vobiz WS: Failed to recover campaign lead from database: {}", e)

    if data:
        role = data.get("_role", "data_edge")
        campaign_role = data.get("_campaign_role", role)
        log_dir = str(_get_role_path(role, "logs"))
        
        # Priority: Sandbox Prompt > Role Prompt > Default
        system_prompt = data.get("_sandbox_prompt")
        voice = data.get("_sandbox_voice", voice)
        opening_line = _build_opening_line(data, role)
    elif inbound_role:
        # Incoming PSTN call: the dialed DID was mapped to a role in the
        # /vobiz/answer webhook. Use that role's inbound service-desk greeting.
        from core.opening_line import build_inbound_opening_line

        role = normalize_console_role(inbound_role)
        log_dir = str(_get_role_path(role, "logs"))
        opening_line = build_inbound_opening_line({"name": ""}, role)
        logger.info("Inbound call routed to role={}", role)
    elif camp_id and str(camp_id).startswith("manual_"):
        # Make a Call / manual dial: camp_id may be ``manual_{role}`` or
        # ``manual_{role}_{token}`` when each attempt gets a unique id.
        from core.state import parse_manual_camp_role_suffix

        suffix = str(camp_id)[len("manual_") :]
        role, _attempt = parse_manual_camp_role_suffix(suffix)
        log_dir = str(_get_role_path(role, "logs"))
        opening_line = _build_opening_line({"name": ""}, role)
        logger.info("Manual call leg routed to role={} (camp_id={})", role, camp_id)
    elif manual_role:
        # Telco / proxy may drop custom camp_id; answer URL can pass manual_role=... as backup.
        role = normalize_console_role(manual_role)
        log_dir = str(_get_role_path(role, "logs"))
        opening_line = _build_opening_line({"name": ""}, role)
        logger.info("Manual call leg routed to role={} (manual_role query param)", role)

    # Restart Resilience: If memory was lost, recover from agent_id or camp_id string
    if not system_prompt and (agent_id or (camp_id and camp_id.startswith("sandbox-"))):
        if not agent_id and camp_id:
            parts = camp_id.split("-")
            if len(parts) >= 2: agent_id = parts[1]
        
        if agent_id:
            from services.sandbox_manager import get_agent
            agent = get_agent(agent_id)
            if agent:
                role = "factory"
                system_prompt = agent.get("prompt", "")
                voice = agent.get("voice", voice)
                # Opening line for sandbox is usually handled by the trigger, but we can default here
                logger.info(f"Vobiz WS: Recovered sandbox agent {agent_id} from database")

    camp_row = data

    prompt_role = role

    if not system_prompt:
        role_config = get_state(prompt_role)
        from prompts.priya import build_role_system_prompt

        system_prompt = build_role_system_prompt(prompt_role, role_config, camp_row)

    # Highest-priority anchors so Live cannot drift to unrelated brands or names from examples elsewhere.
    detail_block = ""
    if isinstance(camp_row, dict):
        from core.opening_line import classify_field_value

        raw_nm = str(camp_row.get("name", "") or "").strip()
        ph = str(camp_row.get("phone", "") or "").strip()
        raw_co = str(camp_row.get("company", "") or "").strip()
        em = str(camp_row.get("email", "") or "").strip()

        # Classify the raw columns dynamically
        nm_type = classify_field_value(raw_nm)
        co_type = classify_field_value(raw_co)

        nm = ""
        co = ""
        location = ""

        # Resolve person's name
        if nm_type == "person":
            nm = raw_nm
        elif co_type == "person":
            nm = raw_co

        # Resolve company name
        if co_type == "company":
            co = raw_co
        elif nm_type == "company":
            co = raw_nm

        # Resolve city/location
        if nm_type == "city":
            location = raw_nm
        elif co_type == "city":
            location = raw_co

        # ``extra`` carries any non-standard CSV columns the operator uploaded
        # (e.g. rfq_subject, product, quantity, last_quote, notes, city, industry).
        # We surface them so the agent can speak about the lead's specific
        # situation — but instruct it to weave details in naturally rather than
        # reading them out as a list, and to NEVER read out emails/IDs unless
        # asked. Empty/blank fields are skipped so the prompt stays compact.
        extra_dict = camp_row.get("extra") or {}
        if not isinstance(extra_dict, dict):
            extra_dict = {}
        if location:
            extra_dict = {**extra_dict, "location": location}
        # Pretty key (snake_case → Title Case) for readability in the prompt.
        def _pretty_key(k: str) -> str:
            return " ".join(w.capitalize() for w in str(k).replace("_", " ").split())

        extra_lines: list[str] = []
        for k, v in extra_dict.items():
            sv = str(v).strip()
            if sv:
                extra_lines.append(f"  {_pretty_key(k)}: {sv}")
        # Trim very long values so a runaway 5KB notes column can't blow up the
        # system prompt token budget. Keep the first 600 chars per field — more
        # than enough for an RFQ subject line, a quote summary, etc.
        extra_lines = [
            (line[:600] + "…") if len(line) > 600 else line for line in extra_lines
        ]

        # When no real name is available but we do have a company, instruct
        # the model to greet the company instead of inventing a name.
        if nm:
            name_hint = nm
        elif co:
            name_hint = (
                f"(personal name not supplied — address the callee as someone "
                f"from {co}, e.g. 'calling for {co}'. NEVER speak the dialed "
                "phone number, lead ID, or any digits as if they were a name.)"
            )
        else:
            name_hint = (
                "(not supplied — use the generic opener line without inventing "
                "a name. NEVER speak the dialed phone number or lead ID as if "
                "it were a name.)"
            )
        callee_lines = [
            "\n\n[CURRENT CALL DETAILS — AUTHORITATIVE FOR THIS PSTN LEG ONLY]",
            "Use only this callee name in speech — do not substitute any other person's name "
            "(names in sample scripts elsewhere are EXAMPLES ONLY, not who is on this call). "
            "When a Company field is present below, you MUST still acknowledge that organisation "
            "once early in the call (together with any personal name) — see RFQ instructions.",
            f"Callee name field: {name_hint}",
            f"Dialed number: {ph}",
        ]
        if co:
            callee_lines.append(f"Company: {co}")
        if em:
            callee_lines.append(f"Email on file: {em} (do NOT read this aloud unless the user asks for it).")
        else:
            callee_lines.append("Email on file: NOT AVAILABLE — do NOT invent one.")

        if extra_lines:
            callee_lines.append("")
            callee_lines.append("Additional context from the lead list (mention these naturally when relevant — do not read out as a list, do not invent fields that are not here):")
            callee_lines.extend(extra_lines)


        detail_block = "\n".join(callee_lines) + "\n"
    # Per-role persona anchors + pacing/context rules (cached for performance).
    static_blocks = _get_static_prompt_blocks(prompt_role)

    # ─── Active campaign Case ────────────────────────────────────────────────
    # The operator can define and activate one "Case" per role from the
    # dashboard (e.g. "April Steel Sheets Push", "Diwali Discount Drive").
    # When set, its description is appended *near the top* of the system
    # prompt — strong enough to steer pitch/offer, but it never replaces
    # the persona anchor or hard rules below.
    case_block = ""
    try:
        from core.storage import get_active_case

        active_case = await get_active_case(role)
        if active_case and (active_case.get("description") or "").strip():
            case_name = (active_case.get("name") or "").strip() or "Active Case"
            case_desc = active_case["description"].strip()
            case_block = (
                "\n[ACTIVE CASE — TODAY'S CAMPAIGN INSTRUCTIONS — APPLIES TO THIS CALL]\n"
                f"Case: {case_name}\n"
                "Follow these instructions naturally during the conversation. "
                "These describe the *current* campaign offer / context / pitch — "
                "blend them into your normal persona and product talk. They take "
                "priority over generic examples in the role prompt below, but "
                "they NEVER override the persona anchor (your name / company / "
                "language rules / end-call rules).\n"
                f"---\n{case_desc}\n---\n\n"
            )
            logger.info(
                "Injecting active case into system prompt for role={}: {!r}",
                role, case_name,
            )
    except Exception as exc:
        logger.warning("Active-case lookup failed for role={}: {}", role, exc)

    # ─── Voice / pacing rule (Gemini Live) ──────────────────────────────────

    inbound_block = ""
    if inbound_role:
        inbound_block = (
            "\n[INBOUND CALL — CUSTOMER DIALED US]\n"
            "This is an INCOMING call — the customer reached our number. "
            "Use your inbound service-desk greeting and help with their enquiry.\n\n"
        )

    if static_blocks.startswith("[ANCHOR"):
        system_prompt = static_blocks + inbound_block + case_block + system_prompt + detail_block
    else:
        system_prompt = inbound_block + static_blocks + case_block + system_prompt + detail_block

    # Truncate overly long system prompts to reduce model inference latency.
    # 4000 chars ≈ 1000 tokens — sufficient for persona + rules + context.
    MAX_SYSTEM_PROMPT_CHARS = int(os.getenv("MAX_SYSTEM_PROMPT_CHARS", "14000"))
    if len(system_prompt) > MAX_SYSTEM_PROMPT_CHARS:
        system_prompt = system_prompt[:MAX_SYSTEM_PROMPT_CHARS]
        logger.warning("System prompt truncated to {} chars for low-latency mode", MAX_SYSTEM_PROMPT_CHARS)

    logger.info(f"Vobiz WS (live): client connected for camp={camp_id} role={role}")

    # 2. Setup Recording & Callbacks
    live_log_id = new_session_id("vobiz-live")
    if camp_id:
        live_log_id = f"camp-{camp_id[:12]}-{new_session_id('').strip('-')[:14]}"

    # 2.5 Mark connection time in memory + persist log_id so the dashboard can render
    # Listen / Audit links once the recording is finalized.
    if camp_id and data:
        if camp_id in _CAMPAIGN_DATA:
            _CAMPAIGN_DATA[camp_id]["_call_connected_at"] = time.time()
            _CAMPAIGN_DATA[camp_id]["_log_id"] = live_log_id
        try:
            lead_id = data.get("_lead_id")
            if lead_id:
                from core.state import update_lead_call_info as _persist_call_info

                _persist_call_info(lead_id, log_id=live_log_id, call_id=camp_id)
                # Update lead status from "dialing" to "in_progress" so the dashboard
                # shows "In Progress" instead of "Dialing" during the active call.
                try:
                    from core.storage import update_lead_status as _uls
                    await _uls(lead_id, "in_progress")
                    logger.info("Lead {} status → in_progress (WS connected for camp={})", lead_id, camp_id)
                except Exception as _status_exc:
                    logger.warning("Failed to set in_progress status for lead {}: {}", lead_id, _status_exc)
                # Also update the manual_calls table so the dashboard shows "In Progress"
                try:
                    from core.storage import mark_manual_call_in_progress as _mcip
                    await _mcip(camp_id)
                    logger.info("Manual call {} status → in_progress", camp_id)
                except Exception as _mcip_exc:
                    logger.warning("Failed to set in_progress for manual call {}: {}", camp_id, _mcip_exc)
        except Exception as _exc:
            logger.warning("Persist live_log_id failed: {}", _exc)
        logger.info(f"Call {camp_id} connected via WebSocket (log_id={live_log_id})")

    call_rec: Optional[CallRecorder] = (
        CallRecorder(live_log_id, channel="vobiz-live", base_dir=recording_dir)
        if settings.call_recording_enabled
        else None
    )

    def on_recording_started_local(cid, lid):
        try:
            from services.campaign_live import push_transcript
            push_transcript(cid, "system", f"Recording started: {lid}")
        except Exception as cb_exc:
            logger.warning("on_recording_started callback failed: {}", cb_exc)

    # 3. Setup RAG Helper (inline, no external dependency).
    # Strategy: try keyword match first (precise lines win). If nothing matches,
    # fall back to a compact digest of the KB so Gemini always has fresh facts to ground in.
    _RAG_DIGEST_CHAR_LIMIT = 1800

    def _rag_digest(rag: str) -> str:
        lines = [ln.strip() for ln in (rag or "").splitlines() if ln.strip() and not ln.strip().startswith("#")]
        digest, total = [], 0
        for ln in lines:
            if total + len(ln) + 1 > _RAG_DIGEST_CHAR_LIMIT:
                break
            digest.append(ln)
            total += len(ln) + 1
        return "\n".join(digest)

    def live_rag_context(q: str) -> Optional[str]:
        if not settings.rag_enabled:
            return None
        if settings.rag_live_low_latency:
            return None
        if not camp_id or not data:
            return None
        # Use prompt_role instead of data.get("_role") so that dynamic RAG matches the dynamic prompt
        call_role = prompt_role
        role_config = get_state(call_role)
        rag = (role_config.get("rag") or "").strip()
        if not rag:
            return None
        query_lower = (q or "").lower()
        keywords = [w for w in query_lower.split() if len(w) >= 3]
        matches: list[str] = []
        if keywords:
            for line in rag.split("\n"):
                ll = line.strip()
                if not ll:
                    continue
                if any(kw in ll.lower() for kw in keywords):
                    matches.append(ll)
        # Always return something so Gemini gets a per-turn reminder of the KB it should use.
        if matches:
            return "\n".join(matches[:8])
        return _rag_digest(rag)
    state = VobizSessionState()
    state.log_session_id = live_log_id
    vobiz_meta_logged = False
    last_user_audio_t: Optional[float] = None
    response_t0: Optional[float] = None
    latency = LatencyTracker()
    # Watchdog: timestamp of the last *meaningful* event (user STT text in or model
    # turn complete out). Updated in pump_gemini_to_queue. If neither side does
    # anything meaningful for SILENCE_HANGUP_SEC, the silence_watchdog task hangs
    # up the call so the campaign worker can move to the next lead.
    last_meaningful_t: float = time.perf_counter()
    # Set True when the first user STT text arrives after the greeting phase.
    # Used by greeting_silence_watchdog to decide whether a nudge is needed.
    greeting_stt_seen: bool = False
    SILENCE_HANGUP_SEC: float = float(os.getenv("CALL_SILENCE_HANGUP_SEC", "30"))
    MAX_CALL_DURATION_SEC: float = float(os.getenv("CALL_MAX_DURATION_SEC", "600"))
    # Grace period: do NOT trigger silence watchdog during the first N seconds
    # after Vobiz stream starts.  Gives time for greeting PCM to play and
    # Gemini to generate its first audio response.
    SILENCE_GRACE_SEC: float = float(os.getenv("CALL_SILENCE_GRACE_SEC", "15"))
    vobiz_stream_started_at: float = 0.0  # set when vobiz_stream_started fires

    # Resolve Vobiz REST credentials for this leg so we can DELETE the call
    # when the agent fires ``end_call``. Uses the same resolution as the
    # campaign worker so dedicated roles never leak into the global account.
    try:
        _role_state = get_state(role) or {}
    except Exception as exc:
        logger.exception("get_state failed for role={}", role)
        _role_state = {}
    _v_cfg = (_role_state.get("vobiz") or {}) if isinstance(_role_state, dict) else {}
    from core.vobiz_credentials import resolve_vobiz_credentials as _resolve_vc
    _resolved = _resolve_vc(role, _v_cfg)
    vobiz_auth_id, vobiz_auth_token = _resolved[0], _resolved[1]

    # ── Outbound scripted PCM + opening instructions ─────────────────────────
    # Must run *before* ``build_live_setup`` so Gemini receives CONTEXT / OPENING
    # fragments in ``system_instruction`` instead of silently mutating a local
    # string after ``setup`` is already sent.

    prior_16k_queue = bytearray()
    vobiz_stream_started = asyncio.Event()
    # Greeting phase: tracks when the scripted greeting finishes playing.
    # During this phase and for a short grace period after, user audio is suppressed
    # (not forwarded to Gemini) and interruption events are ignored — prevents the
    # deadlock where the user says "Hello?" during greeting delay and VAD keeps waiting.
    # Set to the timestamp when greeting phase ends; 0 means greeting phase is active.
    greeting_phase_end_t: float = 0.0
    # Grace period after greeting ends to suppress user audio (prevents VAD deadlock
    # if user says "Hello?" right as greeting finishes)
    GREETING_PHASE_GRACE_SEC: float = 1.5

    def _in_greeting_phase() -> bool:
        """Return True if we're still in the greeting phase (greeting active or grace period)."""
        if greeting_phase_end_t == 0.0:
            return True  # Greeting hasn't ended yet
        return (time.perf_counter() - greeting_phase_end_t) < GREETING_PHASE_GRACE_SEC

    role_config = get_state(prompt_role)
    from core.state import resolved_greeting_text

    if inbound_role:
        from core.opening_line import build_inbound_opening_line

        greeting_text = build_inbound_opening_line({}, prompt_role)
        camp_open_row: dict = {}
    else:
        greeting_text = resolved_greeting_text(prompt_role)
        camp_open_row = data if data else {}
        if isinstance(camp_open_row, dict):
            from core.greeting_text_utils import coerce_stored_greeting

            cg = coerce_stored_greeting(prompt_role, (camp_open_row.get("greeting_text") or "").strip())
            if cg:
                greeting_text = cg
            elif not greeting_text:
                from core.opening_line import build_opening_line as _role_opening_line

                greeting_text = (_role_opening_line(camp_open_row, role) or "").strip()
    if not greeting_text:
        greeting_text = (opening_line or settings.vobiz_opening_line_default or "").strip()
    if greeting_text:
        opening_line = greeting_text

    is_rfq_context = False
    if is_rfq_context:
        if opening_line:
            opening_line = opening_line.replace("Devika", "Radhika").replace("devika", "radhika")
        if greeting_text:
            greeting_text = greeting_text.replace("Devika", "Radhika").replace("devika", "radhika")
    # Live-first = opening audio from Gemini Live (same native voice as rest of call). False = scripted PCM / REST TTS file.
    gemini_live_first = bool(settings.gemini_live_first_opening)
    if not gemini_live_first:
        # 1) Memory primed at dial (campaign / manual) — earliest ready path for recorded audio
        if camp_id and camp_id in _CAMPAIGN_DATA:
            prewarmed = _CAMPAIGN_DATA[camp_id].get("opening_pcm")
            if prewarmed:
                pcm_bytes, in_sr = prewarmed
                if in_sr != VOBIZ_SR:
                    pcm_bytes = pcm_resample(pcm_bytes, in_sr, VOBIZ_SR)
                prior_16k_queue.extend(pcm_bytes)
                logger.info("Loaded pre-primed greeting from campaign memory (before disk).")

        # 2) Disk: inbound legs prefer ``greeting_{role}_inbound.pcm`` (Live-captured opener).
        if len(prior_16k_queue) == 0:
            from core.greeting_pcm import load_recorded_greeting_pcm

            recorded = None
            greet_for_hash = (greeting_text or opening_line or "").strip()
            logger.info(
                "DIAG greeting: role={} greeting_text={!r} opening_line={!r} inbound={}",
                role,
                (greeting_text or "")[:80],
                (opening_line or "")[:80],
                bool(inbound_role),
            )
            if inbound_role:
                recorded = load_recorded_greeting_pcm(
                    role, "inbound", greeting_text=greet_for_hash
                )
            if not recorded:
                recorded = load_recorded_greeting_pcm(role, greeting_text=greet_for_hash)
            if recorded:
                pcm_bytes, in_sr = recorded
                if in_sr != VOBIZ_SR:
                    pcm_bytes = pcm_resample(pcm_bytes, in_sr, VOBIZ_SR)
                prior_16k_queue.extend(pcm_bytes)
                logger.info(
                    "Loaded recorded greeting from disk for role={} inbound={} ({} bytes, sr={})",
                    role,
                    bool(inbound_role),
                    len(pcm_bytes),
                    in_sr,
                )
            else:
                logger.warning(
                    "DIAG: No greeting PCM found on disk for role={} — will rely on Gemini Live opening nudge",
                    role,
                )

    if len(prior_16k_queue) > 0:
        played_line = (greeting_text or opening_line or "").strip()
        if played_line:
            system_prompt += (
                f"\n\n[RECORDED GREETING ALREADY PLAYED — DO NOT REPEAT]\n"
                f"The callee already heard this exact opening on recorded audio:\n\"{played_line}\"\n"
                "Do NOT repeat the greeting or your introduction.\n"
                "Now immediately continue with a brief follow-up question (e.g. ask how you can help, "
                "or check if they have a moment). Keep it short and natural — never the opening line again.\n"
            )
        else:
            system_prompt += (
                "\n\n[RECORDED GREETING ALREADY PLAYED — DO NOT REPEAT]\n"
                "A pre-recorded opening already played on this call. Do NOT greet or introduce "
                "yourself again. Wait for the customer, then continue naturally.\n"
            )
    elif gemini_live_first and opening_line:
        system_prompt += (
            "\n\n[OPENING — YOUR FIRST SPOKEN UTTERANCE ON THIS CALL]\n"
            "You begin the conversation now. Your first audible reply must follow this scripted "
            "opening faithfully (adapt only pacing and natural delivery in the caller's language; "
            "keep names and factual content).\n\""
            + opening_line
            + "\""
        )
    elif gemini_live_first:
        _live_first_hints = {
            "data_edge": (
                "[OPENING — YOUR FIRST SPOKEN UTTERANCE ON THIS CALL]\n"
                "You begin as **Priya**, career counselor at **Data Edge** (say exactly): "
                "\"Hi, this is Priya from Data Edge. I'm a career counselor — got a quick minute?\" "
                "— then continue per your persona. Never mention Tirupati or real estate.\n"
            ),
        }
        hint = _live_first_hints.get(role)
        system_prompt += (
            f"\n\n{hint}"
            if hint
            else (
                "\n\n[OPENING — YOUR FIRST SPOKEN UTTERANCE ON THIS CALL]\n"
                "You begin the conversation now with one short warm, professional greeting, "
                "then continue per your persona.\n"
            )
        )
    elif opening_line:
        logger.warning("No pre-warmed audio found for start of call. Forcing Gemini to initiate.")
        system_prompt += (
            f"\n\n[CRITICAL: The automated greeting failed to play. You MUST start the "
            f"conversation yourself immediately. Say: \"{opening_line}\"]"
        )

    if greeting_text:
        opening_line = greeting_text

    if opening_line:
        append_turn(
            live_log_id,
            "assistant",
            opening_line,
            "vobiz-live",
            base_dir=log_dir,
            note="scripted_opening",
        )
        if camp_id and opening_line:
            try:
                from services.campaign_live import push_transcript

                push_transcript(camp_id, "assistant", opening_line)
            except Exception as _ce:
                logger.warning("live transcript push (opening) failed: {}", _ce)

    _prior_opening_bytes_at_connect = len(prior_16k_queue)
    defer_gemini_until_scripted = len(prior_16k_queue) > 0
    opening_script_pcm = bytearray(prior_16k_queue)

    mix_bg_audio = None
    mix_bg_volume = 0.0
    if getattr(settings, "background_music_enabled", False):
        try:
            bg_path = (getattr(settings, "background_music_path", "") or "").strip()
            bg_vol_raw = getattr(settings, "background_music_volume", 0.0) or 0.0
            vol = float(bg_vol_raw)
            if bg_path and vol > 0:
                mix_bg_audio = load_background_audio(bg_path)
                mix_bg_volume = vol
        except Exception as _bg_err:
            logger.warning("Background music load skipped: {}", _bg_err)

    if defer_gemini_until_scripted:
        prior_16k_queue.clear()
        logger.info(
            "Deferring Gemini Live WebSocket until scripted greeting finishes ({} bytes).",
            len(opening_script_pcm),
        )

    async def drain_scripted_opening_before_live_connect(buf: bytearray) -> bool:
        """Send scripted PCM only; do not connect Gemini Live yet. Returns False if leg ends."""
        opening_done = asyncio.Event()
        abort_scripted = asyncio.Event()

        async def scripted_sender() -> None:
            try:
                # Wait until Vobiz stream is ready (start event received).
                # Vobiz can take 5-10s to emit the start event; waiting avoids
                # sending audio into a closed pipe which causes TCP backpressure.
                await vobiz_stream_started.wait()
                logger.info("scripted_sender: vobiz_stream_started received — beginning greeting playback")
                chunk_samples = int(VOBIZ_SR * 0.02)
                chunk_bytes = chunk_samples * 2
                scripted_bg_pos = 0
                _outbuf = bytearray()
                # Build the ENTIRE greeting in one tight (non-yielding) loop. This
                # must not contain any `await`/sleep: under event-loop starvation
                # (heavy API polling / campaign worker on the single shared loop of
                # a 2-core VPS) a per-chunk `await asyncio.sleep` was being delayed
                # ~300ms each, stretching 2.2s of audio to ~34s. A non-yielding
                # mix loop finishes in <10ms; Vobiz then plays the buffered audio
                # at its own real-time rate.
                while len(buf) > 0:
                    if abort_scripted.is_set():
                        return
                    pcm = pop_l16_chunk(buf, chunk_bytes)
                    mixed, scripted_bg_pos = mix_voice_and_background_tick(
                        pcm,
                        mix_bg_audio,
                        mix_bg_volume,
                        scripted_bg_pos,
                        chunk_samples,
                    )
                    _outbuf.extend(mixed)
                if call_rec is not None:
                    call_rec.add_outbound(bytes(_outbuf))
                # Send the whole greeting in a few rapid (unpaced) batched chunks.
                # Vobiz buffers streamed audio and plays it back at real-time, so
                # dumping ~2.2s at once yields correct-speed playback.
                _t0 = time.perf_counter()
                for _off in range(0, len(_outbuf), 16384):
                    try:
                        await send_play_audio_batched(
                            ws, bytes(_outbuf[_off : _off + 16384]), VOBIZ_SR
                        )
                    except Exception as e:
                        logger.warning("scripted opening send_play_audio failed: {}", e)
                logger.info(
                    "DIAG scripted greeting delivered: bytes={} send_ms={:.0f}",
                    len(_outbuf), (time.perf_counter() - _t0) * 1000,
                )
                # The greeting is already delivered to Vobiz in one shot; Vobiz
                # queues/buffers and plays it at real-time regardless of our loop.
                # Don't park the greeting phase for the full playback duration —
                # under event-loop starvation that sleep stretches to seconds and
                # leaves dead air before Gemini connects. A short settle is enough;
                # Gemini's audio is appended AFTER the greeting in Vobiz's queue, so
                # there is no overlap.
                await asyncio.sleep(0.35)
            finally:
                opening_done.set()

        inbound_script_cb_recorded = False

        async def recv_until_scripted_done() -> bool:
            nonlocal inbound_script_cb_recorded, vobiz_meta_logged, last_user_audio_t, vobiz_stream_started_at
            poll_s = 0.05
            while True:
                if opening_done.is_set():
                    return True
                try:
                    raw = await asyncio.wait_for(_vobiz_incoming.get(), timeout=poll_s)
                except asyncio.TimeoutError:
                    continue
                if raw is None:
                    logger.info("Vobiz inbound closed during scripted opening phase")
                    abort_scripted.set()
                    return False
                try:
                    msg = json.loads(raw)
                except Exception:
                    logger.debug("Ignoring malformed JSON during scripted opening: {}", raw[:200])
                    continue
                ev = msg.get("event")
                if ev == "start":
                    start = msg.get("start") or {}
                    state.call_id = start.get("callId", "")
                    state.stream_id = start.get("streamId", "")
                    if not vobiz_meta_logged:
                        vobiz_meta_logged = True
                        append_session_meta(
                            live_log_id,
                            "vobiz-live",
                            call_id=state.call_id,
                            stream_id=state.stream_id,
                            base_dir=log_dir,
                        )
                    logger.info(
                        "Vobiz live stream start call={} stream={} fmt={} (pre-Gemini scripted)",
                        state.call_id,
                        state.stream_id,
                        start.get("mediaFormat"),
                    )
                    vobiz_stream_started.set()
                    vobiz_stream_started_at = time.perf_counter()
                    logger.info(
                        "DIAG: Vobiz stream STARTED call={} stream={} ({}ms after WS accept)",
                        state.call_id,
                        state.stream_id,
                        int((vobiz_stream_started_at - _opening_t0) * 1000),
                    )

                    if inbound_role and not inbound_script_cb_recorded:
                        inbound_script_cb_recorded = True
                        try:
                            from core.state import _CAMPAIGN_TASKS, normalize_console_role
                            from core.storage import record_inbound_callback

                            r = normalize_console_role(inbound_role)
                            _t = _CAMPAIGN_TASKS.get(r)
                            ca = bool(_t and not _t.done())
                            fnum, tnum = extract_vobiz_start_numbers(start)
                            if not fnum:
                                logger.info(
                                    "Inbound start: no From in payload (keys={})",
                                    sorted(start.keys()),
                                )
                            await record_inbound_callback(
                                r,
                                fnum or "unknown",
                                to_phone=tnum or None,
                                call_uuid=state.call_id or None,
                                campaign_active=ca,
                                raw_start=dict(start),
                            )
                        except Exception as exc:
                            logger.warning("inbound callback record failed: {}", exc)
                elif ev == "media":
                    media = msg.get("media") or {}
                    b64 = media.get("payload") or ""
                    if not b64:
                        continue
                    try:
                        _in_pcm = base64.b64decode(b64)
                    except Exception:
                        logger.debug("Base64 decode failed during scripted opening")
                        continue
                    if _in_pcm and call_rec is not None:
                        try:
                            call_rec.add_inbound(_in_pcm)
                        except Exception:
                            logger.warning("Recording inbound audio failed during scripted opening")
                    last_user_audio_t = time.perf_counter()
                elif ev == "stop":
                    logger.info(
                        "Vobiz live stream stop during scripted opening: {}",
                        msg.get("reason"),
                    )
                    abort_scripted.set()
                    return False

        recv_task = asyncio.create_task(recv_until_scripted_done())
        sender_task = asyncio.create_task(scripted_sender())
        rr, rs = await asyncio.gather(recv_task, sender_task, return_exceptions=True)

        if rr is False:
            logger.warning("Scripted opening phase aborted before Gemini Live connect")
            return False
        if isinstance(rr, Exception):
            logger.warning("Scripted opening recv task failed: {}", rr)
            return False
        if isinstance(rs, Exception):
            logger.warning("Scripted opening sender failed: {}", rs)
            return False

        logger.info("Scripted greeting finished — connecting Gemini Live WebSocket.")
        logger.info(
            "DIAG: Scripted greeting DONE — elapsed={:.0f}ms prior_bytes={} gemini_will_connect_now=True",
            (time.perf_counter() - _opening_t0) * 1000,
            _prior_opening_bytes_at_connect,
        )
        return True

    # ── Latency opt: finalize system prompt early (before greeting) ──
    # The WS handshake (TCP+TLS+WS) was kicked off at line ~260 and overlaps
    # all DB/config work. Here we just prepare the setup payload so it can be
    # sent instantly after the greeting finishes.
    if is_rfq_context:
        system_prompt = system_prompt.replace("Devika", "Radhika").replace("devika", "radhika")
    system_prompt = apply_live_voice_turn_addon(system_prompt)
    if defer_gemini_until_scripted and opening_line:
        system_prompt += (
            f"\n\n[SYSTEM DIRECTION: The outbound call has just connected. The system has already played "
            f"the following greeting to the callee: \"{opening_line}\". Do NOT repeat this greeting. "
            f"Wait for the callee to respond and continue the conversation naturally from there.]"
        )
    vad_ultra = bool(int(os.getenv("GEMINI_LIVE_VAD_ULTRA", "1"))) and role == "data_edge" and _prior_opening_bytes_at_connect == 0
    logger.info(
        "DIAG VAD config: role={} ultra={} aggressive={} start_sens={} end_sens={} silence_ms={} prefix_ms={}",
        role,
        vad_ultra,
        settings.gemini_live_aggressive_activity_detection,
        os.getenv("GEMINI_LIVE_VAD_START_SENSITIVITY", "default"),
        os.getenv("GEMINI_LIVE_VAD_END_SENSITIVITY", "default"),
        settings.gemini_live_vad_silence_duration_ms,
        settings.gemini_live_vad_prefix_padding_ms,
    )
    setup = build_live_setup(
        model=model,
        system_instruction=system_prompt,
        voice=voice,
        language_code=language_code,
        vad_ultra=vad_ultra,
    )

    try:
        if defer_gemini_until_scripted:
            if not await drain_scripted_opening_before_live_connect(opening_script_pcm):
                return
            # Greeting phase is over — record the timestamp so user audio suppression
            # persists for a short grace period (prevents VAD deadlock).
            greeting_phase_end_t = time.perf_counter()
            # CRITICAL FIX: Reset the silence timer AFTER the greeting finishes.
            # Without this, the silence watchdog counts from WebSocket connect time,
            # which includes the greeting playback duration — causing premature hangup
            # at ~40s when the greeting takes ~5-10s and Gemini takes ~25s to respond.
            last_meaningful_t = time.perf_counter()
            logger.info(
                "DIAG greeting phase: ended at +{:.0f}ms — user audio forwarded immediately (grace={:.1f}s) "
                "silence_timer_reset=True",
                (greeting_phase_end_t - _opening_t0) * 1000,
                GREETING_PHASE_GRACE_SEC,
            )
        else:
            # No scripted greeting — greeting phase ends immediately
            greeting_phase_end_t = time.perf_counter()
            # Reset silence timer for non-scripted path too
            last_meaningful_t = time.perf_counter()
            logger.info(
                "DIAG greeting phase: no scripted greeting — ended at +{:.0f}ms silence_timer_reset=True",
                (greeting_phase_end_t - _opening_t0) * 1000,
            )

        # ── Await the early-started Gemini WS connect (handshake only) ──────
        # The connect was kicked off right after api_key resolution so the
        # TCP+TLS+WS handshake overlaps with all the DB/config work above.
        # We do NOT send setup here — that happens inside the async context
        # below where pump_gemini_to_queue is already running to catch setupComplete.
        try:
            _gem = await _gemini_ws_connect_task
        except Exception as _conn_exc:
            logger.error(
                "DIAG: Gemini Live WS connection FAILED ({}): {} — "
                "call will rely on TTS fallback or scripted greeting. "
                "Check: API key validity, network connectivity to generativelanguage.googleapis.com, "
                "model '{}' availability.",
                type(_conn_exc).__name__, _conn_exc, model,
            )
            # Try to play TTS greeting as fallback so the call isn't completely silent
            if opening_line and len(prior_16k_queue) == 0:
                try:
                    from services.gemini_tts import gemini_synthesize_pcm, get_gemini_tts_httpx
                    tts_client = await get_gemini_tts_httpx()
                    pcm_bytes, sr = await asyncio.wait_for(
                        gemini_synthesize_pcm(
                            tts_client,
                            text=opening_line,
                            voice=voice,
                            style_mode="opening",
                        ),
                        timeout=15.0,
                    )
                    if pcm_bytes and len(pcm_bytes) > 100:
                        if sr != VOBIZ_SR:
                            pcm_bytes = pcm_resample(pcm_bytes, sr, VOBIZ_SR)
                        await send_play_audio_batched(ws, pcm_bytes, VOBIZ_SR)
                        if call_rec is not None:
                            call_rec.add_outbound(pcm_bytes)
                        last_meaningful_t = time.perf_counter()
                        append_turn(
                            live_log_id, "assistant", opening_line,
                            "vobiz-live", base_dir=log_dir,
                            note="ws_connect_fail_tts_greeting",
                        )
                        logger.info(
                            "WS_CONNECT_FALLBACK: TTS greeting delivered after Gemini WS failure ({} bytes)",
                            len(pcm_bytes),
                        )
                except Exception as _tts_fallback_exc:
                    logger.warning("WS_CONNECT_FALLBACK: TTS greeting also failed: {}", _tts_fallback_exc)
            return
        _gemini_ws_connect_task = None  # mark consumed
        _gem_live_session_t0 = time.perf_counter()
        _ws_connect_ms = (_gem_live_session_t0 - _opening_t0) * 1000
        logger.info("OPENING_TIMING | gemini_ws_connected | +{:.0f}ms", _ws_connect_ms)
        logger.info(
            "DIAG: Gemini Live WS connected in {:.0f}ms — gem={}",
            _ws_connect_ms,
            type(_gem).__name__ if _gem else "FAILED",
        )

        # Minimal async-context-manager wrapper so the rest of the block
        # keeps the ``async with ... as gem:`` idiom unchanged.
        class _ReuseGeminiWS:
            async def __aenter__(self_): return _gem
            async def __aexit__(self_, *a):
                try: await _gem.__aexit__(*a)
                except Exception: pass

        async with _ReuseGeminiWS() as gem:

            rec_extra: dict[str, Any] = {}
            if call_rec is not None:
                mrec = call_rec.meta()
                rec_extra = {k: v for k, v in mrec.items() if v is not None and v != ""}
            append_session_meta(
                live_log_id,
                "vobiz-live",
                path="gemini_live",
                model=model,
                base_dir=log_dir,
                **rec_extra,
            )

            # Live transcript state (inputAudioTranscription; output optional).
            last_in_user = ""
            last_out_assistant = ""
            had_model_audio_turn = False
            last_rag_inject_key = ""
            activity_end_seq = 0
            # Set once Gemini acknowledges ``setup``; gates all outbound traffic
            # (user audio, nudges, silence kick) so we never send before config.
            gemini_setup_complete = asyncio.Event()
            # Guard: prevents double TTS greeting when both _fallback_greeting_tts
            # and _setup_complete_watchdog try to deliver the greeting.
            fallback_tts_played = False

            # While prior_16k_queue still holds opening audio, we drop Gemini model
            # audio so the first words on the line always match the scripted line.

            # ----- Task 1: Vobiz -> Gemini (audio in) -----
            inbound_callback_recorded = False

            async def pump_vobiz_to_gemini() -> None:
                nonlocal last_user_audio_t, vobiz_meta_logged, inbound_callback_recorded, vobiz_stream_started_at
                connect_t0 = time.perf_counter()
                user_audio_fwd_bytes = 0
                user_audio_fwd_logged = False
                while True:
                    raw = await _vobiz_incoming.get()
                    if raw is None:
                        logger.info("Vobiz inbound queue closed")
                        return
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        logger.debug("Ignoring malformed JSON in pump_vobiz_to_gemini: {}", raw[:200])
                        continue
                    ev = msg.get("event")
                    if ev == "start":
                        start = msg.get("start") or {}
                        state.call_id = start.get("callId", "")
                        state.stream_id = start.get("streamId", "")
                        if not vobiz_meta_logged:
                            vobiz_meta_logged = True
                            append_session_meta(
                                live_log_id,
                                "vobiz-live",
                                call_id=state.call_id,
                                stream_id=state.stream_id,
                                base_dir=log_dir,
                            )
                        logger.info(
                            "Vobiz live stream start call={} stream={} fmt={}",
                            state.call_id, state.stream_id, start.get("mediaFormat"),
                        )
                        vobiz_stream_started.set()
                        vobiz_stream_started_at = time.perf_counter()
                        logger.info(
                            "DIAG: Vobiz stream STARTED (non-scripted path) call={} stream={} ({}ms after WS accept)",
                            state.call_id,
                            state.stream_id,
                            int((vobiz_stream_started_at - _opening_t0) * 1000),
                        )

                        if inbound_role and not inbound_callback_recorded:
                            inbound_callback_recorded = True
                            try:
                                from core.state import _CAMPAIGN_TASKS, normalize_console_role
                                from core.storage import record_inbound_callback

                                r = normalize_console_role(inbound_role)
                                _t = _CAMPAIGN_TASKS.get(r)
                                ca = bool(_t and not _t.done())
                                fnum, tnum = extract_vobiz_start_numbers(start)
                                if not fnum:
                                    logger.info(
                                        "Inbound start: no From in payload (keys={})",
                                        sorted(start.keys()),
                                    )
                                await record_inbound_callback(
                                    r,
                                    fnum or "unknown",
                                    to_phone=tnum or None,
                                    call_uuid=state.call_id or None,
                                    campaign_active=ca,
                                    raw_start=dict(start),
                                )
                            except Exception as exc:
                                logger.warning("inbound callback record failed: {}", exc)
                        
                        # One silence burst to Gemini so Live activates VAD / turn-taking.
                        # Deduped with the pre-start kick when ``prior_16k_queue`` is empty.
                        # Gate on setupComplete (don't send audio before the session is configured).
                        if not state.gemini_silence_kick_sent and gemini_setup_complete.is_set():
                            logger.info("Vobiz stream started: Gemini PCM silence kick (VAD)")
                            try:
                                await gemini_send_pcm_silence_kick(gem, duration_ms=120)
                                state.gemini_silence_kick_sent = True
                            except Exception as e:
                                logger.warning("Gemini PCM silence kick on stream start failed: {}", e)
                    elif ev == "media":
                        media = msg.get("media") or {}
                        b64 = media.get("payload") or ""
                        if not b64:
                            continue
                        # Vobiz sends 16 kHz L16 PCM; Gemini wants exactly that.
                        _in_pcm = None
                        try:
                            _in_pcm = base64.b64decode(b64)
                            if _in_pcm and call_rec is not None:
                                call_rec.add_inbound(_in_pcm)
                        except Exception:
                            logger.debug("Base64 decode or recording failed in pump_vobiz_to_gemini")
                        last_user_audio_t = time.perf_counter()

                        # Forward user audio even during the greeting phase grace period so Gemini
                        # can hear the user responding to the greeting immediately. Only skip while
                        # scripted PCM is still physically queued for the handset (prior_16k_queue).
                        if len(prior_16k_queue) > 0:
                            continue

                        mute_s = max(0.0, settings.vobiz_gemini_live_forward_mute_seconds)
                        if (
                            _prior_opening_bytes_at_connect == 0
                            and mute_s > 0
                            and (time.perf_counter() - connect_t0) < mute_s
                        ):
                            continue

                        latency.on_gemini_forwarded()
                        # Forward base64 directly to Gemini — avoids decode+re-encode overhead (~5ms/frame).
                        # Gate on setupComplete so we never send audio before the session is configured.
                        if not gemini_setup_complete.is_set():
                            if user_audio_fwd_bytes == 0:
                                _elapsed_ms = (time.perf_counter() - _opening_t0) * 1000
                                if _elapsed_ms > 5000:
                                    logger.warning(
                                        "DIAG user-audio: BLOCKED for {:.0f}ms — setupComplete not received. "
                                        "Gemini session may be hung (model={}, camp={}). "
                                        "User audio will NOT reach Gemini until setup completes.",
                                        _elapsed_ms, model, camp_id,
                                    )
                            continue
                        in_len = len(_in_pcm) if _in_pcm else len(b64)
                        user_audio_fwd_bytes += in_len
                        if not user_audio_fwd_logged:
                            user_audio_fwd_logged = True
                            logger.info(
                                "DIAG user-audio: FIRST frame forwarded to Gemini at +{:.0f}ms (role={}, greeting_phase={})",
                                (time.perf_counter() - _opening_t0) * 1000,
                                role,
                                _in_greeting_phase(),
                            )
                        if user_audio_fwd_bytes % 16000 < in_len:
                            logger.info(
                                "DIAG user-audio: forwarded {} bytes (~{:.1f}s) to Gemini so far",
                                user_audio_fwd_bytes,
                                user_audio_fwd_bytes / 32000.0,
                            )
                        await gem.send(json.dumps({
                            "realtimeInput": {
                                "audio": {
                                    "data": b64,
                                    "mimeType": "audio/pcm;rate=16000",
                                }
                            }
                        }))
                    elif ev == "stop":
                        logger.info("Vobiz live stream stop: {}", msg.get("reason"))
                        return

            # ----- Task 2 & 3: Gemini -> Queue -> Vobiz (mixed audio out + transcripts) -----
            pending_audio_24k = bytearray()
            gemini_16k_queue = bytearray()
            gemini_resample_state = None
            model_generation_active = False

            bg_audio = mix_bg_audio
            bg_volume = mix_bg_volume

            # RAG prefetch cache: key = user query text, value = prebuilt KB block.
            # Populated on inputTranscription deltas in a worker thread so the context is
            # ready the instant activityEnd fires — no SQLite FTS on the hot path.
            rag_prefetch_cache: dict[str, str] = {}
            rag_prefetch_inflight: set[str] = set()

            async def _prefetch_rag(q: str) -> None:
                if not live_rag_context or not q or q in rag_prefetch_cache or q in rag_prefetch_inflight:
                    return
                rag_prefetch_inflight.add(q)
                try:
                    block = await asyncio.to_thread(live_rag_context, q)
                    rag_prefetch_cache[q] = (block or "").strip()
                except Exception as e:
                    logger.warning("Live RAG prefetch failed: {}", e)
                finally:
                    rag_prefetch_inflight.discard(q)

            async def _try_inject_live_rag(reason: str) -> None:
                """Inject the cached (or freshly computed) KB block for the latest STT."""
                nonlocal last_rag_inject_key
                if not live_rag_context or len(prior_16k_queue) > 0:
                    return
                q = (last_in_user or "").strip()
                if len(q) < 2 or q == last_rag_inject_key:
                    return
                block = rag_prefetch_cache.get(q)
                if block is None and len(q) < 20:
                    return  # Short query — skip blocking RAG lookup
                if block is None:
                    try:
                        block = (await asyncio.to_thread(live_rag_context, q) or "").strip()
                        rag_prefetch_cache[q] = block
                    except Exception as e:
                        logger.warning("Live RAG callback failed ({}): {}", reason, e)
                        return
                if not block:
                    return
                last_rag_inject_key = q
                try:
                    await gemini_send_live_rag(gem, block, turn_complete=True)
                    logger.info(
                        "Gemini Live: RAG context sent ({} chars) [{}]",
                        len(block),
                        reason,
                    )
                    append_artifact(
                        live_log_id,
                        "vobiz-live",
                        "rag_inject",
                        f"{len(block)} chars (after STT)",
                        base_dir=log_dir,
                        stt_query_preview=q[:500],
                    )
                except Exception as e:
                    logger.warning("Gemini Live: RAG send failed ({}): {}", reason, e)
                    last_rag_inject_key = ""

            async def pump_gemini_to_queue() -> None:
                nonlocal response_t0, last_in_user, last_out_assistant, had_model_audio_turn, last_rag_inject_key, activity_end_seq, last_meaningful_t, gemini_resample_state, model_generation_active, greeting_stt_seen
                first_byte_logged = False
                turn_model_bytes = 0
                _retry_nudge_count = 0
                _MIN_MODEL_AUDIO_BYTES = 500
                # Send the live ``setup`` as the very first message, before any
                # realtimeInput/audio. Without it Gemini rejects the session with
                # close code 1007 ("invalid argument"). pump_vobiz_to_gemini and the
                # opening nudges must wait for ``setupComplete`` before sending anything.
                try:
                    await gem.send(json.dumps(setup))
                    logger.info(
                        "Gemini Live: setup sent (model={} voice={} lang={})",
                        model, voice, language_code,
                    )
                except Exception as e:
                    logger.error("Gemini Live: setup send failed: {}", e)
                    return
                # Diagnostic: track setupComplete receipt
                _setup_sent_t = time.perf_counter()
                async for raw in gem:
                    try:
                        obj = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode("utf-8"))
                    except Exception:
                        logger.debug("Ignoring malformed JSON from Gemini Live")
                        continue
                    if obj.get("setupComplete") is not None:
                        if not gemini_setup_complete.is_set():
                            gemini_setup_complete.set()
                            _sc_elapsed = (time.perf_counter() - _setup_sent_t) * 1000
                            logger.info("Gemini Live: setupComplete received in {:.0f}ms — session active", _sc_elapsed)
                        continue
                    if obj.get("error"):
                        _err = obj["error"]
                        logger.error(
                            "Gemini Live upstream error: {} (full={})",
                            _err if isinstance(_err, str) else str(_err)[:200],
                            obj,
                        )
                        # Fatal auth / access errors — terminate immediately instead of waiting 30s
                        _err_str = str(_err).lower()
                        if any(kw in _err_str for kw in ("permission", "denied", "unauthorized", "api_key", "quota", "billing", "403", "401")):
                            logger.error(
                                "Gemini Live FATAL error (likely API key / billing issue) — terminating call {} immediately",
                                state.call_id,
                            )
                            await terminate_call(
                                ws,
                                call_uuid=state.call_id,
                                auth_id=vobiz_auth_id,
                                auth_token=vobiz_auth_token,
                                drain_seconds=0.0,
                            )
                            return
                    if obj.get("goAway"):
                        logger.warning("Gemini Live goAway: {}", obj.get("goAway"))

                    # Top-level tool/function call: Gemini Live emits these *outside*
                    # ``serverContent`` as ``toolCall.functionCalls``. The model uses
                    # the ``end_call`` tool to ask us to hang up the PSTN leg.
                    tc = obj.get("toolCall") or {}
                    fn_calls = tc.get("functionCalls") or tc.get("function_calls") or []
                    end_call_fc = next(
                        (fc for fc in fn_calls if (fc or {}).get("name") == "end_call"),
                        None,
                    )
                    if end_call_fc:
                        logger.info(
                            "Gemini Live: AI triggered end_call -> draining + REST hangup (id={}, call_uuid={})",
                            end_call_fc.get("id"),
                            state.call_id,
                        )
                        # Acknowledge the tool call so Gemini cleanly closes the turn.
                        try:
                            await gem.send(
                                json.dumps(
                                    {
                                        "toolResponse": {
                                            "functionResponses": [
                                                {
                                                    "name": "end_call",
                                                    "id": end_call_fc.get("id"),
                                                    "response": {"output": "Call terminated."},
                                                }
                                            ]
                                        }
                                    }
                                )
                            )
                        except Exception as _ack_err:
                            logger.warning("end_call ack failed: {}", _ack_err)
                        # Inline teardown: drain ~0.9 s so the goodbye TTS reaches Vobiz,
                        # send the WS hangup hint, DELETE the call via REST (authoritative
                        # — without this Vobiz reconnects the same camp_id WS), then close.
                        await terminate_call(
                            ws,
                            call_uuid=state.call_id,
                            auth_id=vobiz_auth_id,
                            auth_token=vobiz_auth_token,
                            drain_seconds=0.9,
                        )
                        return

                    sc = obj.get("serverContent") or {}

                    it = sc.get("inputTranscription") or {}
                    if it.get("text"):
                        last_in_user += str(it.get("text") or "")
                        last_meaningful_t = time.perf_counter()
                        if not greeting_stt_seen:
                            greeting_stt_seen = True
                            logger.info(
                                "DIAG greeting STT: first user speech detected at +{:.0f}ms — text={!r}",
                                (time.perf_counter() - _opening_t0) * 1000,
                                (it.get("text") or "")[:60],
                            )
                        latency.on_stt_text()
                        logger.info("Gemini Live STT: {!r}", last_in_user)
                        # Prefetch RAG while the user is still speaking so the context is
                        # ready at activityEnd — no SQLite on the critical path.
                        if live_rag_context and len(prior_16k_queue) == 0:
                            q_now = last_in_user.strip()
                            if len(q_now) >= 2:
                                _task = asyncio.create_task(_prefetch_rag(q_now))
                                _background_tasks.add(_task)
                                _task.add_done_callback(_background_tasks.discard)
                    out_tx = sc.get("outputTranscription") or obj.get("outputTranscription") or {}
                    if out_tx.get("text"):
                        last_out_assistant += str(out_tx.get("text") or "")

                    if sc.get("activityEnd") is not None or obj.get("activityEnd") is not None:
                        response_t0 = time.perf_counter()
                        first_byte_logged = False
                        turn_model_bytes = 0
                        latency.on_activity_end()
                        if live_rag_context and len(prior_16k_queue) == 0:
                            activity_end_seq += 1
                            seq_captured = activity_end_seq

                            async def _deferred_rag(delay: float, label: str) -> None:
                                await asyncio.sleep(delay)
                                if seq_captured != activity_end_seq:
                                    return
                                await _try_inject_live_rag(label)

                            # One immediate inject (usually hits the prefetch cache for 0 ms lookup)
                            # and one fallback at +180 ms in case STT text landed in a later frame.
                            # Removed the +450 ms inject — it caused extra text reads mid-reply.
                            _task = asyncio.create_task(_deferred_rag(0.18, "after activityEnd+180ms"))
                            _background_tasks.add(_task)
                            _task.add_done_callback(_background_tasks.discard)
                            await _try_inject_live_rag("activityEnd")

                    if sc.get("interrupted"):
                        # During greeting phase: ignore ALL interruptions — the greeting
                        # must play completely to prevent the deadlock where user says
                        # "Hello?" and AI keeps waiting.
                        if _in_greeting_phase():
                            logger.info(
                                "Gemini Live: interrupted during greeting phase — ignoring "
                                "(greeting must complete, {:.0f} ms elapsed)",
                                (time.perf_counter() - _gem_live_session_t0) * 1000.0,
                            )
                        elif len(prior_16k_queue) > 0 and (time.perf_counter() - _gem_live_session_t0) < 1.2:
                            logger.info(
                                "Gemini Live: interrupted during scripted opening — ignoring early start "
                                "({} bytes still queued, {:.0f} ms elapsed)",
                                len(prior_16k_queue),
                                (time.perf_counter() - _gem_live_session_t0) * 1000.0,
                            )
                        elif (
                            _prior_opening_bytes_at_connect == 0
                            and (time.perf_counter() - _gem_live_session_t0) < 1.2
                        ):
                            logger.info(
                                "Gemini Live: ignoring spurious interrupted near session start "
                                "({:.0f} ms, no scripted PCM)",
                                (time.perf_counter() - _gem_live_session_t0) * 1000.0,
                            )
                        else:
                            logger.info("Gemini Live: user barge-in (interrupted)")
                            last_rag_inject_key = ""
                            activity_end_seq += 1
                            prior_16k_queue.clear()
                            pending_audio_24k.clear()
                            gemini_16k_queue.clear()
                            # Keep gemini_resample_state across turns for smooth
                            # audio continuity (prevents clicks/pops at turn boundaries).
                            # The resampler state carries filter history that must not
                            # be abruptly reset after barge-in.
                            model_generation_active = False
                            await vobiz_send_clear_audio(ws)

                    mt = sc.get("modelTurn") or {}
                    for part in (mt.get("parts") or []):
                        inline = part.get("inlineData") or part.get("inline_data")
                        if not inline:
                            continue
                        mime = str(inline.get("mimeType") or inline.get("mime_type") or "")
                        if not mime.startswith("audio/"):
                            continue
                        b64 = inline.get("data") or ""
                        if not b64:
                            continue
                        pcm = base64.b64decode(b64)
                        turn_model_bytes += len(pcm)

                        # While scripted PCM is still physically draining to the handset,
                        # hold Gemini audio to avoid overlap. The greeting grace period
                        # (_in_greeting_phase) must NOT gate audio output — it only guards
                        # against interruption events and user mic forwarding.
                        if len(prior_16k_queue) > 0:
                            continue
                        had_model_audio_turn = True
                        model_generation_active = True
                        latency.on_first_model_audio()
                        if not first_byte_logged:
                            t0 = response_t0 if response_t0 is not None else last_user_audio_t
                            if t0 is not None:
                                dt = (time.perf_counter() - t0) * 1000.0
                                logger.info(
                                    "Gemini Live: first model-audio chunk — {:.0f} ms since trigger, {} bytes 24kHz PCM (streaming to Vobiz) [OPENING_TIMING | first_audio | +{:.0f}ms]",
                                    dt,
                                    len(pcm),
                                    (time.perf_counter() - _opening_t0) * 1000,
                                )
                            else:
                                logger.info(
                                    "Gemini Live: first model-audio chunk — {} bytes 24kHz PCM (no latency baseline yet) [OPENING_TIMING | first_audio | +{:.0f}ms]",
                                    len(pcm),
                                    (time.perf_counter() - _opening_t0) * 1000,
                                )
                            first_byte_logged = True
                        pending_audio_24k.extend(pcm)
                        
                        # Resample on 20ms boundary to match playout tick (640 bytes @ 16kHz).
                        # Processing 20ms blocks (960 bytes @ 24kHz) → 640 bytes @ 16kHz = 1 playout tick.
                        CHUNK_24K_BYTES = int(GEMINI_OUT_SR * 2 * 0.020)
                        while len(pending_audio_24k) >= CHUNK_24K_BYTES:
                            chunk = bytes(pending_audio_24k[:CHUNK_24K_BYTES])
                            del pending_audio_24k[:CHUNK_24K_BYTES]
                            pcm_16k, gemini_resample_state = resample_24k_to_16k_numpy(
                                chunk, gemini_resample_state
                            )
                            gemini_16k_queue.extend(pcm_16k)

                    if sc.get("turnComplete") or sc.get("generationComplete"):
                        last_meaningful_t = time.perf_counter()
                        if turn_model_bytes == 0:
                            logger.warning(
                                "DIAG model-turn: turnComplete but ZERO audio bytes — "
                                "model produced no speech (had_audio={}, greeting_phase={}, "
                                "prior_pcm_bytes={}, camp={}). "
                                "Check: model availability, API key, system prompt clarity.",
                                had_model_audio_turn,
                                _in_greeting_phase(),
                                len(prior_16k_queue),
                                camp_id,
                            )
                        else:
                            logger.info(
                                "DIAG model-turn: turnComplete — model sent {} bytes 24kHz this turn (had_audio={})",
                                turn_model_bytes,
                                had_model_audio_turn,
                            )

                        # Retry nudge: if the model produced too little audio on the
                        # opening turn, send a stronger nudge so it speaks its greeting.
                        if (
                            turn_model_bytes < _MIN_MODEL_AUDIO_BYTES
                            and _retry_nudge_count < 2
                            and not had_model_audio_turn
                        ):
                            _retry_nudge_count += 1
                            logger.warning(
                                "DIAG retry-nudge: model produced only {} bytes (<{} threshold) — "
                                "sending retry nudge (attempt {}/2)",
                                turn_model_bytes,
                                _MIN_MODEL_AUDIO_BYTES,
                                _retry_nudge_count,
                            )
                            try:
                                await gemini_send_retry_nudge(gem, attempt=_retry_nudge_count)
                            except Exception as _rerr:
                                logger.warning("DIAG retry-nudge send failed: {}", _rerr)
                        latency.log_turn_summary(logger)
                        model_generation_active = False
                        if len(prior_16k_queue) == 0 and pending_audio_24k:
                            chunk = bytes(pending_audio_24k)
                            pending_audio_24k.clear()
                            pcm_16k, gemini_resample_state = resample_24k_to_16k_numpy(
                                chunk, gemini_resample_state
                            )
                            gemini_16k_queue.extend(pcm_16k)
                        elif len(prior_16k_queue) > 0 and pending_audio_24k:
                            pending_audio_24k.clear()
                        # Clear the prefetch cache at turn boundary: next utterance has a new query.
                        rag_prefetch_cache.clear()
                        # Keep gemini_resample_state across turns for smooth audio continuity
                        # (prevents clicks/pops at turn boundaries)
                        u_turn = (last_in_user or "").strip()
                        if u_turn:
                            append_turn(live_log_id, "user", u_turn, "vobiz-live", base_dir=log_dir)
                            if camp_id:
                                try:
                                    from services.campaign_live import push_transcript
                                    push_transcript(camp_id, "user", u_turn)
                                except Exception as _ce:
                                    logger.warning("live transcript push (user) failed: {}", _ce)
                            last_in_user = ""
                        a_turn = (last_out_assistant or "").strip()
                        if a_turn:
                            append_turn(live_log_id, "assistant", a_turn, "vobiz-live", base_dir=log_dir)
                            if camp_id:
                                try:
                                    from services.campaign_live import push_transcript
                                    push_transcript(camp_id, "assistant", a_turn)
                                except Exception as _ce:
                                    logger.warning("live transcript push (assistant) failed: {}", _ce)
                            last_out_assistant = ""
                        elif had_model_audio_turn and len(prior_16k_queue) == 0:
                            append_turn(
                                live_log_id,
                                "assistant",
                                "[Audio reply — Gemini Live; no text transcript]",
                                "vobiz-live",
                                base_dir=log_dir,
                                synthetic="1",
                            )
                        had_model_audio_turn = False
                        response_t0 = None
                        first_byte_logged = False
                        last_rag_inject_key = ""

                _setup_ok = gemini_setup_complete.is_set()
                logger.info(
                    "Gemini Live upstream WebSocket recv loop ended (setupComplete={}, elapsed={:.0f}ms, "
                    "had_model_audio={}, camp={})",
                    _setup_ok,
                    (time.perf_counter() - _opening_t0) * 1000,
                    had_model_audio_turn,
                    camp_id,
                )
                if not _setup_ok:
                    logger.error(
                        "Gemini Live recv loop ended WITHOUT setupComplete — session was never active. "
                        "User audio was BLOCKED for the entire session. "
                        "Likely causes: invalid API key, model '{}' not available, quota exceeded, "
                        "or network issue. The fallback TTS greeting task will attempt to deliver audio.",
                        model,
                    )

            async def pump_mixed_to_vobiz() -> None:
                nonlocal model_generation_active
                # Start sending audio immediately — don't wait for the Vobiz "start" event.
                # Vobiz may timeout the call if it detects silence on the line for >5s, and
                # the start event can be delayed under some network conditions. The mixer
                # sends silence/comfort noise until real audio arrives from Gemini.
                _mixer_start_t = time.perf_counter()
                if not vobiz_stream_started.is_set():
                    logger.info("pump_mixed_to_vobiz: starting immediately (no wait for vobiz stream start)")
                logger.info(
                    "Mixer started for camp={} call={} stream={} (waited {:.0f}ms, stream_started={})",
                    camp_id,
                    state.call_id,
                    state.stream_id,
                    (time.perf_counter() - _mixer_start_t) * 1000,
                    vobiz_stream_started.is_set(),
                )
                # 20 ms mixer tick: prevents bursty packet transmission and improves playout smoothness.
                # The 24kHz -> 16kHz resampler continues to run statefully on 20ms boundaries inside pump_gemini_to_queue.
                chunk_samples = int(VOBIZ_SR * 0.02)  # 20 ms = 320 samples @16k
                chunk_bytes = chunk_samples * 2
                bg_pos = 0
                next_wakeup = time.perf_counter()
                
                # Playout Jitter Buffer Safety Margin (configurable, default 160 ms = 8 packets @ 20ms)
                PREBUFFER_BYTES = int(VOBIZ_SR * 2 * settings.vobiz_playout_prebuffer_seconds)
                is_playing_gemini = False
                _first_gemini_response = True  # Latency opt: skip prebuffer gate for first response
                # Last sample value for hold-last-sample technique (prevents clicks/pops from silence padding)
                _hold_l: int = 0  # left channel (mono = uses left)
                
                # Batched send: accumulate 8 frames (160 ms) then send as one WS message.
                # On a 2-core VPS each ws.send_text() blocks ~280 ms; batching cuts
                # 50 sends/sec down to ~6 sends/sec.
                _outbuf = bytearray()
                _BATCH_FRAMES = 8
                _BATCH_BYTES = chunk_bytes * _BATCH_FRAMES
                _flush_interval = 0.02 * _BATCH_FRAMES
                _next_flush = time.perf_counter()
                
                def _hold_pad(length: int) -> bytes:
                    """Generate padding bytes that hold the last output sample value
                    instead of going to zero abruptly — eliminates click/pop at boundaries."""
                    nonlocal _hold_l
                    if length <= 0:
                        return b""
                    pad = struct.pack("<h", _hold_l) * (length // 2)
                    return pad
                
                while True:
                    gemini_pcm = b""
                    # 1. Outbound voice: always drain scripted opening first, then LLM
                    if len(prior_16k_queue) > 0:
                        gemini_pcm = pop_l16_chunk(prior_16k_queue, chunk_bytes)
                        is_playing_gemini = False  # Reset prebuffering state when greeting is running
                    else:
                        q_len = len(gemini_16k_queue)
                        if not is_playing_gemini:
                            if _first_gemini_response and q_len > 0:
                                is_playing_gemini = True
                                _first_gemini_response = False
                                logger.info("Gemini outbound first response ({} bytes) — immediate playout", q_len)
                            elif q_len >= PREBUFFER_BYTES or (not model_generation_active and q_len > 0):
                                is_playing_gemini = True
                                if _first_gemini_response:
                                    _first_gemini_response = False
                                logger.info("Gemini outbound jitter buffer filled ({} bytes, active={}) — starting playout", q_len, model_generation_active)
                        
                        if is_playing_gemini:
                            # Playout phase
                            if q_len >= chunk_bytes:
                                gemini_pcm = bytes(gemini_16k_queue[:chunk_bytes])
                                del gemini_16k_queue[:chunk_bytes]
                                # Track last sample for hold-last-sample technique
                                _hold_l = struct.unpack("<h", gemini_pcm[-2:])[0]
                            elif q_len > 0 and not model_generation_active:
                                # End of turn drain — apply short fade-out to prevent click
                                remaining = bytes(gemini_16k_queue)
                                gemini_16k_queue.clear()
                                rlen = len(remaining)
                                if rlen >= 4:
                                    # Apply linear fade-out over the last 5ms (80 samples = 160 bytes)
                                    fade_len = min(rlen, int(VOBIZ_SR * 2 * 0.005))
                                    faded = bytearray(remaining)
                                    for i in range(fade_len // 2):
                                        frac = (fade_len // 2 - i) / (fade_len // 2)
                                        pos = rlen - fade_len + i * 2
                                        val = struct.unpack("<h", faded[pos:pos+2])[0]
                                        faded[pos:pos+2] = struct.pack("<h", int(val * frac))
                                    gemini_pcm = bytes(faded)
                                else:
                                    gemini_pcm = remaining
                                # Pad with hold-sample to fill chunk
                                if len(gemini_pcm) < chunk_bytes:
                                    gemini_pcm += _hold_pad(chunk_bytes - len(gemini_pcm))
                                is_playing_gemini = False
                            elif q_len > 0 and model_generation_active:
                                # Partial chunk during generation — use hold-last-sample instead of silence
                                remaining = bytes(gemini_16k_queue)
                                gemini_16k_queue.clear()
                                gemini_pcm = remaining + _hold_pad(chunk_bytes - len(remaining))
                            elif model_generation_active:
                                # Buffer underflow — use hold-last-sample instead of abrupt silence
                                gemini_pcm = _hold_pad(chunk_bytes)
                            else:
                                # Generation finished with empty buffer — stop playback
                                is_playing_gemini = False
                                gemini_pcm = _hold_pad(chunk_bytes)

                    # Always send to Vobiz to keep the native jitter buffer warmed up and perfectly synced!
                    first_tx = not latency._first_vobiz_send
                    latency.on_first_vobiz_send()
                    if first_tx:
                        logger.info(
                            "Mixer first packet sent to Vobiz (call={} stream={} elapsed={:.0f}ms)",
                            state.call_id,
                            state.stream_id,
                            (time.perf_counter() - _opening_t0) * 1000,
                        )
                    mixed, bg_pos = mix_voice_and_background_tick(
                        gemini_pcm or (b"\x00" * chunk_bytes),
                        bg_audio,
                        bg_volume,
                        bg_pos,
                        len(gemini_pcm) // 2 if gemini_pcm else chunk_samples,
                    )
                    # Record individual frame to call_recorder, then batch for WS send
                    if call_rec is not None:
                        call_rec.add_outbound(mixed)
                    _outbuf.extend(mixed)
                    now = time.perf_counter()
                    if len(_outbuf) >= _BATCH_BYTES or (now >= _next_flush and _outbuf):
                        try:
                            await send_play_audio_batched(
                                ws, bytes(_outbuf), VOBIZ_SR
                            )
                        except Exception as e:
                            # If WS is closing/closed, exit the loop cleanly instead of spamming warnings.
                            from starlette.websockets import WebSocketState
                            _closed_states = {WebSocketState.DISCONNECTED}
                            if hasattr(WebSocketState, 'CLOSING'):
                                _closed_states.add(WebSocketState.CLOSING)
                            if hasattr(ws, 'client_state') and ws.client_state in _closed_states:
                                logger.debug("Vobiz WS closed during playout — exiting mixer")
                                return
                            logger.warning("Vobiz playAudio send failed: {}", e)
                        _outbuf.clear()
                        _next_flush = now + _flush_interval

                    # Grid-paced timing: align each tick to a 20ms wall clock grid.
                    # NOTE: must sleep the FULL inter-tick gap (not capped at 10ms) so
                    # the mixer runs at real-time. Capping the sleep made the loop emit
                    # 20ms of audio every ~12ms (~1.7x), draining Gemini's jitter buffer
                    # and forcing hold-sample padding -> audible stutter after the greeting.
                    if now < next_wakeup:
                        await asyncio.sleep(min(0.05, next_wakeup - now))
                    next_wakeup += 0.02
                    if next_wakeup < now - 0.02:
                        next_wakeup = now + 0.02

            async def silence_watchdog() -> None:
                """Hang up the call if neither side has done anything meaningful for
                ``SILENCE_HANGUP_SEC`` seconds. Belt-and-braces fallback for cases
                where the model never invokes ``end_call`` (e.g. stuck silence).
                During the first ``SILENCE_GRACE_SEC`` after Vobiz stream starts,
                the watchdog is paused to allow greeting playback + first AI response."""
                while True:
                    await asyncio.sleep(5.0)
                    # If Vobiz stream never started, don't fire silence watchdog —
                    # the call isn't actually active (infrastructure issue).
                    if not vobiz_stream_started.is_set():
                        logger.debug(
                            "Silence watchdog: Vobiz stream not started yet — skipping (camp={})",
                            camp_id,
                        )
                        continue
                    # Grace period: don't fire during the first seconds after stream starts
                    if vobiz_stream_started_at > 0:
                        elapsed_since_stream = time.perf_counter() - vobiz_stream_started_at
                        if elapsed_since_stream < SILENCE_GRACE_SEC:
                            logger.debug(
                                "Silence watchdog: grace period ({:.0f}/{:.0f}s since stream start)",
                                elapsed_since_stream,
                                SILENCE_GRACE_SEC,
                            )
                            continue
                    idle = time.perf_counter() - last_meaningful_t
                    if idle >= SILENCE_HANGUP_SEC:
                        logger.warning(
                            "Silence watchdog: idle for {:.0f}s (>= {:.0f}s) — REST hangup (call_uuid={}) "
                            "[stream started {:.0f}s ago, grace={:.0f}s, setup_complete={}]",
                            idle,
                            SILENCE_HANGUP_SEC,
                            state.call_id,
                            (time.perf_counter() - vobiz_stream_started_at) if vobiz_stream_started_at else -1,
                            SILENCE_GRACE_SEC,
                            gemini_setup_complete.is_set(),
                        )
                        await terminate_call(
                            ws,
                            call_uuid=state.call_id,
                            auth_id=vobiz_auth_id,
                            auth_token=vobiz_auth_token,
                            drain_seconds=0.0,
                        )
                        return

            async def greeting_silence_watchdog() -> None:
                """If no user STT is detected within ~9s after greeting + setup, nudge Gemini
                to prompt the callee. Safety net for cases where:
                  - The scripted greeting's system prompt says \"wait for customer\" but VAD
                    never fires (user silent / speech too soft).
                  - The greeting phase grace period previously blocked user audio entirely.
                  - Gemini's VAD doesn't detect the callee's first utterance.
                """
                logger.info("Greeting silence watchdog: started (role={}, scripted_greeting={} bytes)", role, _prior_opening_bytes_at_connect)
                try:
                    await asyncio.wait_for(gemini_setup_complete.wait(), timeout=12.0)
                except asyncio.TimeoutError:
                    logger.warning("Greeting silence watchdog: setupComplete not received in 12s — aborting")
                    return
                # Wait for greeting grace period to expire and a brief user response window.
                # The session uses FIRST_COMPLETED so this must NOT be the shortest task.
                for _ in range(6):
                    await asyncio.sleep(1.5)
                    if greeting_stt_seen:
                        logger.info("Greeting silence watchdog: user STT arrived — no nudge needed, staying alive")
                        while True:
                            await asyncio.sleep(3600)  # keep task alive so FIRST_COMPLETED doesn't fire
                if not greeting_stt_seen:
                    logger.info(
                        "Greeting silence watchdog: no STT after {:.0f}s post-setup — sending silence prompt",
                        9.0,
                    )
                    try:
                        await gem.send(json.dumps({
                            "clientContent": {
                                "turns": [{
                                    "role": "user",
                                    "parts": [{
                                        "text": (
                                            "[The callee has not responded to the greeting yet. "
                                            "Ask them if they are on the line — e.g. \"Hello? Are you there?\" "
                                            "— then wait briefly for their reply. Do NOT repeat the opening line.]"
                                        )
                                    }],
                                }],
                                "turnComplete": True,
                            }
                        }))
                        logger.info("Greeting silence watchdog: nudge sent to Gemini")
                    except Exception as exc:
                        logger.warning("Greeting silence watchdog: nudge send failed: {}", exc)
                    # Keep task alive so FIRST_COMPLETED doesn't fire
                    while True:
                        await asyncio.sleep(3600)

            async def user_silence_watchdog() -> None:
                # Disabled — sleep forever so it does not trigger asyncio.wait FIRST_COMPLETED
                while True:
                    await asyncio.sleep(3600)

            async def max_duration_watchdog() -> None:
                """Force-close the WebSocket if the call exceeds MAX_CALL_DURATION_SEC.
                This prevents slot leaks when Vobiz keeps the WS open after a
                failed or zombie call."""
                await asyncio.sleep(MAX_CALL_DURATION_SEC)
                logger.warning(
                    "Max-duration watchdog: call exceeded {:.0f}s — closing WS (call_uuid={})",
                    MAX_CALL_DURATION_SEC,
                    state.call_id,
                )
                try:
                    await ws.close(code=1000, reason="max-duration exceeded")
                except Exception:
                    pass

            task_in = asyncio.create_task(pump_vobiz_to_gemini(), name="pump_vobiz_to_gemini")
            task_out = asyncio.create_task(pump_gemini_to_queue(), name="pump_gemini_to_queue")
            task_mix = asyncio.create_task(pump_mixed_to_vobiz(), name="pump_mixed_to_vobiz")
            task_dog = asyncio.create_task(silence_watchdog(), name="silence_watchdog")
            task_user_silence = asyncio.create_task(user_silence_watchdog(), name="user_silence_watchdog")
            task_max_dur = asyncio.create_task(max_duration_watchdog(), name="max_duration_watchdog")
            task_greet_dog = asyncio.create_task(greeting_silence_watchdog(), name="greeting_silence_watchdog")

            # Diagnostic: warn if setupComplete not received within 8s
            # Also actively triggers TTS fallback and eventually terminates the call.
            async def _setup_complete_watchdog() -> None:
                try:
                    await asyncio.wait_for(gemini_setup_complete.wait(), timeout=8.0)
                    return  # setupComplete arrived — all good
                except asyncio.TimeoutError:
                    logger.warning(
                        "DIAG: setupComplete NOT received within 8s — Gemini session may be hung. "
                        "Check API key validity and model availability (model={}). "
                        "User audio is BLOCKED until setupComplete arrives.",
                        model,
                    )
                    # Send a text nudge as a fallback to try to unblock Gemini
                    try:
                        await gem.send(json.dumps({
                            "clientContent": {
                                "turns": [{"role": "user", "parts": [{"text": "Hello, are you there?"}]}],
                                "turnComplete": True,
                            }
                        }))
                        logger.info("DIAG: fallback text nudge sent to Gemini (setupComplete was missing)")
                    except Exception as _nudge_exc:
                        logger.warning("DIAG: fallback nudge failed: {}", _nudge_exc)
                    # Wait 5 more seconds — if setupComplete still not received, trigger TTS fallback
                    try:
                        await asyncio.wait_for(gemini_setup_complete.wait(), timeout=5.0)
                        return  # setupComplete arrived — we're good
                    except asyncio.TimeoutError:
                        logger.error(
                            "DIAG: setupComplete STILL not received after 13s total — "
                            "triggering immediate TTS greeting fallback (model={}, camp={}). "
                            "Likely cause: invalid API key, model not available, or billing issue.",
                            model,
                            camp_id,
                        )
                        # Actively trigger the TTS greeting instead of waiting for the
                        # separate _fallback_greeting_tts timer (which fires at 5s and
                        # may race with this). If the TTS task already fired, the call
                        # is already handled. If it hasn't, force it now.
                        if not had_model_audio_turn and len(prior_16k_queue) == 0 and opening_line and not fallback_tts_played:
                            fallback_tts_played = True
                            try:
                                from services.gemini_tts import gemini_synthesize_pcm, get_gemini_tts_httpx
                                tts_client = await get_gemini_tts_httpx()
                                pcm_bytes, sr = await asyncio.wait_for(
                                    gemini_synthesize_pcm(
                                        tts_client,
                                        text=opening_line,
                                        voice=voice,
                                        style_mode="opening",
                                    ),
                                    timeout=15.0,
                                )
                                if pcm_bytes and len(pcm_bytes) > 100:
                                    if sr != VOBIZ_SR:
                                        pcm_bytes = pcm_resample(pcm_bytes, sr, VOBIZ_SR)
                                    logger.info(
                                        "SETUP_WATCHDOG TTS: greeting PCM generated ({} bytes) — sending to Vobiz",
                                        len(pcm_bytes),
                                    )
                                    await send_play_audio_batched(ws, pcm_bytes, VOBIZ_SR)
                                    if call_rec is not None:
                                        call_rec.add_outbound(pcm_bytes)
                                    last_meaningful_t = time.perf_counter()
                                    append_turn(
                                        live_log_id, "assistant", opening_line,
                                        "vobiz-live", base_dir=log_dir,
                                        note="setup_watchdog_tts_greeting",
                                    )
                                else:
                                    logger.warning("SETUP_WATCHDOG TTS: empty/short PCM ({} bytes)", len(pcm_bytes or b""))
                            except asyncio.TimeoutError:
                                logger.warning("SETUP_WATCHDOG TTS: Gemini TTS timed out")
                            except Exception as tts_exc:
                                logger.warning("SETUP_WATCHDOG TTS: generation failed: {}", tts_exc)
                            # Last resort: try disk greeting PCM if TTS failed
                            if not fallback_tts_played:
                                try:
                                    from core.greeting_pcm import load_recorded_greeting_pcm
                                    recorded = load_recorded_greeting_pcm(role, greeting_text=greeting_text or opening_line)
                                    if recorded:
                                        pcm_bytes, in_sr = recorded
                                        if in_sr != VOBIZ_SR:
                                            pcm_bytes = pcm_resample(pcm_bytes, in_sr, VOBIZ_SR)
                                        logger.warning(
                                            "SETUP_WATCHDOG DISK_FALLBACK: playing recorded greeting PCM ({} bytes)",
                                            len(pcm_bytes),
                                        )
                                        await send_play_audio_batched(ws, pcm_bytes, VOBIZ_SR)
                                        if call_rec is not None:
                                            call_rec.add_outbound(pcm_bytes)
                                        last_meaningful_t = time.perf_counter()
                                        append_turn(
                                            live_log_id, "assistant", opening_line or "greeting",
                                            "vobiz-live", base_dir=log_dir,
                                            note="setup_watchdog_disk_greeting",
                                        )
                                except Exception as disk_exc:
                                    logger.warning("SETUP_WATCHDOG DISK_FALLBACK: disk greeting also failed: {}", disk_exc)
                        # After TTS greeting attempt, wait 10 more seconds for Gemini to recover.
                        # If setupComplete still hasn't arrived, terminate the zombie call.
                        try:
                            await asyncio.wait_for(gemini_setup_complete.wait(), timeout=10.0)
                            return  # Gemini recovered
                        except asyncio.TimeoutError:
                            logger.error(
                                "DIAG: setupComplete NEVER received — terminating zombie call (camp={}). "
                                "ALL audio sources exhausted: no Gemini, no TTS fallback, no scripted PCM.",
                                camp_id,
                            )
                            await terminate_call(
                                ws,
                                call_uuid=state.call_id,
                                auth_id=vobiz_auth_id,
                                auth_token=vobiz_auth_token,
                                drain_seconds=0.0,
                            )
                except Exception as _watchdog_exc:
                    logger.warning("DIAG: setup watchdog error: {}", _watchdog_exc)

            _task = asyncio.create_task(_setup_complete_watchdog())
            _background_tasks.add(_task)
            _task.add_done_callback(_background_tasks.discard)

            # When no scripted PCM: prompt Gemini to speak the opening as soon as the leg is up.
            needs_live_opening_nudge = _prior_opening_bytes_at_connect == 0 and bool(
                (opening_line or "").strip()
            )
            logger.info(
                "DIAG opening nudge decision: prior_pcm_bytes={} opening_line={!r} needs_nudge={} gemini_live_first={}",
                _prior_opening_bytes_at_connect,
                (opening_line or "")[:80],
                needs_live_opening_nudge,
                gemini_live_first,
            )

            async def _send_live_opening_nudge(label: str) -> None:
                if not needs_live_opening_nudge:
                    return
                # Never send a clientContent turn before the session is configured.
                try:
                    await asyncio.wait_for(gemini_setup_complete.wait(), timeout=10.0)
                except asyncio.TimeoutError:
                    logger.warning("Gemini Live: opening nudge skipped (no setupComplete in 10s)")
                    return
                try:
                    await gemini_send_live_opening_turn_nudge(gem)
                    logger.info("Gemini Live: opening nudge sent ({})", label)
                except Exception as exc:
                    logger.warning("Gemini Live: opening nudge failed ({}): {}", label, exc)

            async def _opening_nudge_after_setup() -> None:
                await _send_live_opening_nudge("post-setup")

            async def _opening_nudge_after_stream() -> None:
                try:
                    await vobiz_stream_started.wait()
                except Exception as exc:
                    logger.debug("opening nudge: vobiz stream wait ended: {}", exc)
                    return
                await _send_live_opening_nudge("post-stream-start")

            for _nudge_runner in (_opening_nudge_after_setup, _opening_nudge_after_stream):
                _task = asyncio.create_task(_nudge_runner())
                _background_tasks.add(_task)
                _task.add_done_callback(_background_tasks.discard)

            # Fire-and-forget nudge for scripted-greeting calls: once setupComplete,
            # send a clientContent turn so Gemini speaks the follow-up immediately
            # instead of waiting for user audio (which keeps the call alive past ~4s).
            async def _continue_after_greeting_nudge() -> None:
                try:
                    await asyncio.wait_for(gemini_setup_complete.wait(), timeout=10.0)
                except asyncio.TimeoutError:
                    return
                if _prior_opening_bytes_at_connect == 0:
                    return  # Already handled by _opening_nudge_after_setup
                try:
                    await gem.send(json.dumps({
                        "clientContent": {
                            "turns": [{"role": "user", "parts": [{"text": "The recorded greeting just played. Now speak to the callee naturally — introduce yourself and start the conversation as instructed."}]}],
                            "turnComplete": True,
                        }
                    }))
                    logger.info("Greeting continuation nudge sent to Gemini")
                except Exception as exc:
                    logger.warning("Greeting continuation nudge failed: {}", exc)

            _task = asyncio.create_task(_continue_after_greeting_nudge())
            _background_tasks.add(_task)
            _task.add_done_callback(_background_tasks.discard)

            # ── CRITICAL: Fallback TTS greeting when Gemini is silent ──────
            # When gemini_live_first_opening=true and the Gemini session fails
            # to produce audio (setupComplete timeout, model error, quota),
            # generate the greeting via REST TTS and play it directly to Vobiz.
            # This prevents the "AI completely silent" scenario.
            async def _fallback_greeting_tts() -> None:
                """Generate + play TTS greeting if Gemini hasn't produced audio within 2.5s."""
                nonlocal fallback_tts_played
                try:
                    # Wait 2.5s — if Gemini hasn't spoken by then, it likely won't.
                    # Reduced from 5s (was 12s) to get audio to the caller before Vobiz silence timeout (~5-6s).
                    await asyncio.sleep(2.5)
                    # If the call already ended or greeting was delivered, bail.
                    if _call_ended():
                        return
                    if had_model_audio_turn or len(prior_16k_queue) > 0:
                        return  # Gemini already spoke or scripted greeting played
                    if fallback_tts_played:
                        return  # Watchdog already triggered TTS
                    if not opening_line:
                        return
                    fallback_tts_played = True
                    logger.warning(
                        "FALLBACK TTS: Gemini produced no audio after 2.5s — "
                        "generating TTS greeting for camp={}",
                        camp_id,
                    )
                    try:
                        from services.gemini_tts import gemini_synthesize_pcm, get_gemini_tts_httpx
                        tts_client = await get_gemini_tts_httpx()
                        pcm_bytes, sr = await asyncio.wait_for(
                            gemini_synthesize_pcm(
                                tts_client,
                                text=opening_line,
                                voice=voice,
                                style_mode="opening",
                            ),
                            timeout=15.0,
                        )
                        if pcm_bytes and len(pcm_bytes) > 100:
                            # Resample to Vobiz's 16kHz if needed
                            if sr != VOBIZ_SR:
                                pcm_bytes = pcm_resample(pcm_bytes, sr, VOBIZ_SR)
                            logger.info(
                                "FALLBACK TTS: greeting PCM generated ({} bytes @ {} Hz) — sending to Vobiz",
                                len(pcm_bytes), VOBIZ_SR,
                            )
                            # Send directly to Vobiz
                            await send_play_audio_batched(ws, pcm_bytes, VOBIZ_SR)
                            # Record to call recorder
                            if call_rec is not None:
                                call_rec.add_outbound(pcm_bytes)
                            # Update last_meaningful_t so silence watchdog doesn't fire
                            nonlocal last_meaningful_t
                            last_meaningful_t = time.perf_counter()
                            # Log the greeting
                            append_turn(
                                live_log_id, "assistant", opening_line,
                                "vobiz-live", base_dir=log_dir,
                                note="fallback_tts_greeting",
                            )
                        else:
                            logger.warning("FALLBACK TTS: generated empty/short PCM ({} bytes)", len(pcm_bytes or b""))
                    except asyncio.TimeoutError:
                        logger.warning("FALLBACK TTS: Gemini TTS timed out after 15s")
                    except Exception as tts_exc:
                        logger.warning("FALLBACK TTS: generation failed: {}", tts_exc)
                    # Last resort: try loading the disk greeting PCM if TTS failed
                    if not fallback_tts_played:
                        try:
                            from core.greeting_pcm import load_recorded_greeting_pcm
                            recorded = load_recorded_greeting_pcm(role, greeting_text=greeting_text or opening_line)
                            if recorded:
                                pcm_bytes, in_sr = recorded
                                if in_sr != VOBIZ_SR:
                                    pcm_bytes = pcm_resample(pcm_bytes, in_sr, VOBIZ_SR)
                                logger.warning(
                                    "DISK_FALLBACK: playing recorded greeting PCM ({} bytes) after TTS failure",
                                    len(pcm_bytes),
                                )
                                await send_play_audio_batched(ws, pcm_bytes, VOBIZ_SR)
                                if call_rec is not None:
                                    call_rec.add_outbound(pcm_bytes)
                                last_meaningful_t = time.perf_counter()
                                append_turn(
                                    live_log_id, "assistant", opening_line or "greeting",
                                    "vobiz-live", base_dir=log_dir,
                                    note="disk_fallback_greeting",
                                )
                        except Exception as disk_exc:
                            logger.warning("DISK_FALLBACK: failed to load greeting PCM: {}", disk_exc)
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    logger.warning("FALLBACK TTS: unexpected error: {}", exc)

            def _call_ended() -> bool:
                """Check if the call has already ended (for early-exit in fallback tasks)."""
                return bool(
                    (camp_id and camp_id in _CAMPAIGN_DATA and _CAMPAIGN_DATA[camp_id].get("_call_ended_at"))
                    or not vobiz_stream_started.is_set()
                )

            if not defer_gemini_until_scripted:
                _task = asyncio.create_task(_fallback_greeting_tts())
                _background_tasks.add(_task)
                _task.add_done_callback(_background_tasks.discard)
                logger.info(
                    "DIAG fallback TTS: enabled (no scripted PCM) — will fire at +{:.0f}s if Gemini silent",
                    2.5,
                )

            try:
                done, pending = await asyncio.wait(
                    {task_in, task_out, task_mix, task_dog, task_user_silence, task_max_dur, task_greet_dog}, return_when=asyncio.FIRST_COMPLETED
                )
                for ft in done:
                    exc = ft.exception()
                    if exc is not None:
                        if isinstance(exc, ws_client.ConnectionClosed):
                            logger.error(
                                "Gemini Live WebSocket closed — native audio stopped (close code={}, reason={!r}). "
                                "Recorded/scripted greeting may already have played; this is unrelated to prior_16k_queue. "
                                "If Google says denied access / 1008, fix API key billing, project Live entitlement, "
                                "or Gemini Live preview access for your account.",
                                exc.code,
                                exc.reason,
                            )
                        else:
                            logger.exception("A background task in live_session crashed fatally! {}", exc)
                # Diagnostic summary
                _total_session_sec = time.perf_counter() - _opening_t0
                _had_greeting = _prior_opening_bytes_at_connect > 0
                _setup_ok = gemini_setup_complete.is_set()
                _had_model_audio = had_model_audio_turn
                _done_info = []
                for ft in done:
                    name = ft.get_name()
                    exc = ft.exception()
                    if exc is not None:
                        _done_info.append(f"{name}:exc={type(exc).__name__}:{str(exc)[:80]}")
                    elif ft.cancelled():
                        _done_info.append(f"{name}:cancelled")
                    else:
                        _done_info.append(f"{name}:ok={type(ft.result()).__name__}")
                # Key diagnostic: log why the call was silent (if it was)
                _silence_reason = ""
                if not _setup_ok:
                    _silence_reason = "SILENCE_CAUSE: Gemini setupComplete never received (bad API key? model unavailable? quota?)"
                elif not _had_model_audio and not _had_greeting:
                    _silence_reason = "SILENCE_CAUSE: No greeting PCM AND no model audio produced"
                elif not _had_model_audio:
                    _silence_reason = "SILENCE_CAUSE: Model produced no audio (check system prompt / opening nudge)"
                if _silence_reason:
                    logger.error("DIAG {}", _silence_reason)
                logger.info(
                    "DIAG SESSION END: role={} camp={} duration={:.1f}s had_greeting_pcm={} "
                    "setup_complete={} had_model_audio={} stream_started_at={:.0f}s gemini_connected={} tasks_done=[{}]",
                    role,
                    camp_id,
                    _total_session_sec,
                    _had_greeting,
                    _setup_ok,
                    _had_model_audio,
                    vobiz_stream_started_at - _opening_t0 if vobiz_stream_started_at else -1,
                    _gem_live_session_t0 > 0 if '_gem_live_session_t0' in dir() else False,
                    "; ".join(_done_info),
                )
            finally:
                for t in {task_in, task_out, task_mix, task_dog, task_user_silence, task_max_dur, task_greet_dog}:
                    if not t.done():
                        t.cancel()
                        try: await t
                        except asyncio.CancelledError: pass
    except Exception as exc:
        logger.exception("Vobiz live WS error: {}", exc)
    finally:
        # Cleanup early Gemini WS connect if it was never consumed (e.g. scripted path abort)
        if _gemini_ws_connect_task is not None and not _gemini_ws_connect_task.done():
            _gemini_ws_connect_task.cancel()
            try: await _gemini_ws_connect_task
            except (asyncio.CancelledError, Exception): pass
        # Safety-net: release the telephony slot so the campaign can resume.
        # This runs for ALL calls (manual + campaign) when WebSocket closes.
        if camp_id:
            from core.state import release_vobiz_call_slot as _rel_slot
            _rel_slot(role)
            logger.info("Released vobiz call slot for camp_id={} role={}", camp_id, role)
        if call_rec is not None:
            try:
                call_rec.close()
            except Exception as exc:
                logger.warning("Call recorder close failed: {}", exc)
        # Track call duration in campaign data
        dur_sec: Optional[float] = None
        if camp_id and camp_id in _CAMPAIGN_DATA:
            connected_at = _CAMPAIGN_DATA[camp_id].get("_call_connected_at")
            if connected_at:
                duration = time.time() - connected_at
                _CAMPAIGN_DATA[camp_id]["call_duration_sec"] = round(duration, 1)
                _CAMPAIGN_DATA[camp_id]["_call_ended_at"] = time.time()
                # Clear _call_connected_at so try_recover_stale_vobiz_slot
                # doesn't incorrectly treat a closed call as still active.
                del _CAMPAIGN_DATA[camp_id]["_call_connected_at"]
                dur_sec = float(_CAMPAIGN_DATA[camp_id]["call_duration_sec"])
                logger.info(f"Call {camp_id} ended — duration: {duration:.0f}s")
                
                # Auto-trigger analysis
                lead_id = _CAMPAIGN_DATA[camp_id].get("_lead_id")
                if lead_id:
                    from core.worker import _analyze_and_update_lead
                    _task = asyncio.create_task(_analyze_and_update_lead(role, lead_id, live_log_id, duration_sec=dur_sec))
                    _background_tasks.add(_task)
                    _task.add_done_callback(_background_tasks.discard)
            else:
                # WS closed before ever connecting (e.g. Vobiz error) — log it
                logger.warning(
                    "Call {} WS closed without ever connecting (_call_connected_at not set)",
                    camp_id,
                )

        if camp_id and live_log_id:
            mem = _CAMPAIGN_DATA.get(camp_id) if camp_id in _CAMPAIGN_DATA else None
            is_manual = bool(isinstance(mem, dict) and mem.get("_manual_leg"))
            if not is_manual and str(camp_id).startswith("manual_"):
                from core.storage import manual_call_exists_for_camp

                is_manual = await manual_call_exists_for_camp(camp_id)
            if is_manual:
                from core.worker import _finalize_manual_call_leg

                _task = asyncio.create_task(_finalize_manual_call_leg(role, camp_id, live_log_id, dur_sec))
                _background_tasks.add(_task)
                _task.add_done_callback(_background_tasks.discard)
        try:
            await ws.close()
        except Exception as exc:
            logger.debug("WebSocket close failed: {}", exc)
        logger.info("Vobiz WS (live): closed")

