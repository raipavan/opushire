"""Script to initiate a test call to a phone number and monitor the live session."""

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from services.vobiz_bridge.vobiz_client import make_vobiz_call
from core.state import _CAMPAIGN_DATA, _prime_opening_audio

def norm_phone(p: str) -> str:
    p = str(p or "").strip()
    if not p: return ""
    if p.startswith("+"): return p
    digits = "".join(c for c in p if c.isdigit())
    if len(digits) == 10: return "+91" + digits
    if len(digits) == 12 and digits.startswith("91"): return "+" + digits
    return "+" + digits

async def trigger_call(phone_number: str, callee_name: str = "Mayur"):
    phone_number = norm_phone(phone_number)
    print("=" * 60)
    print(f"  INITIATING TEST CALL TO: {phone_number}")
    print("=" * 60)
    
    auth_id = settings.vobiz_data_edge_auth_id or settings.vobiz_auth_id
    auth_token = settings.vobiz_data_edge_auth_token or settings.vobiz_auth_token
    from_num = settings.vobiz_data_edge_from_number or settings.vobiz_from_number
    base_url = (settings.vobiz_stream_public_base_url or settings.vobiz_public_base_url or "http://31.97.186.20:8001").rstrip("/")
    
    print(f"  Auth ID: {auth_id[:6]}... ({auth_id[-4:] if len(auth_id) > 10 else ''})")
    print(f"  From Number: {from_num}")
    print(f"  Base URL: {base_url}")
    
    if not auth_id or not auth_token:
        print("❌ Error: Missing Vobiz Auth ID or Token in environment!")
        return

    camp_id = f"manual_data_edge_test_{int(asyncio.get_event_loop().time())}"
    answer_url = f"{base_url}/vobiz/answer?camp_id={camp_id}"
    ring_url = f"{base_url}/vobiz/ring?camp_id={camp_id}"
    hangup_url = f"{base_url}/vobiz/hangup?camp_id={camp_id}"
    
    _CAMPAIGN_DATA[camp_id] = {
        "_role": "data_edge",
        "_manual_leg": True,
        "phone": phone_number,
        "name": callee_name,
    }
    
    print(f"  Answer URL: {answer_url}")
    print(f"  Dialing now via Vobiz API...")
    
    extra = {
        "ring_url": ring_url,
        "ring_method": "POST",
        "hangup_url": hangup_url,
        "hangup_method": "POST",
        "hangup_on_ring": "60",
    }
    
    try:
        res = await make_vobiz_call(
            to=phone_number,
            from_=from_num,
            answer_url=answer_url,
            auth_id=auth_id,
            auth_token=auth_token,
            extra=extra,
        )
        print("\n✅ VOBIZ API RESPONSE:")
        print(json.dumps(res, indent=2))
        print("\nCall placed successfully! Keep your phone nearby to answer.")
        print("To observe real-time logs on the VPS, run:")
        print("  tail -f /root/vernika/backend/server.log")
    except Exception as e:
        print(f"\n❌ Call Failed: {e}")

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "+917204955388"
    asyncio.run(trigger_call(target))
