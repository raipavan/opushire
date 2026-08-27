"""Diagnose why calls are not connecting — run on the VPS server.

Usage:  python diagnose_calls.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path

# Load config
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import settings


def section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def check(label: str, ok: bool, detail: str = "") -> None:
    icon = "✓" if ok else "✗"
    print(f"  [{icon}] {label}")
    if detail:
        print(f"       {detail}")


async def diagnose():
    section("1. GEMINI API KEY")
    key = settings.gemini_api_key
    check("GEMINI_API_KEY set", bool(key), repr(key[:12] + "..." if key else "(empty)"))
    check("Key format (AIza...)", key.startswith("AIza") if key else False)
    # Quick API check
    if key:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(
                    f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
                )
                if r.status_code == 200:
                    models = r.json().get("models", [])
                    live_models = [m["name"] for m in models if "live" in m.get("name", "").lower()]
                    check("Gemini API reachable", True, f"{len(models)} models found, {len(live_models)} Live models")
                    if live_models:
                        print(f"       Live models: {live_models[:5]}")
                    else:
                        check("Gemini Live model available", False, "No Live models found — check API key billing/access")
                else:
                    check("Gemini API reachable", False, f"HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            check("Gemini API reachable", False, str(e))

    section("2. VOBIZ CREDENTIALS")
    auth_id = settings.vobiz_data_edge_auth_id
    auth_token = settings.vobiz_data_edge_auth_token
    from_num = settings.vobiz_data_edge_from_number
    base_url = settings.vobiz_public_base_url

    check("VOBIZ_DATA_EDGE_AUTH_ID set", bool(auth_id), repr(auth_id or "(empty)"))
    check("VOBIZ_DATA_EDGE_AUTH_TOKEN set", bool(auth_token), repr((auth_token[:8] + "...") if auth_token else "(empty)"))
    check("VOBIZ_DATA_EDGE_FROM_NUMBER set", bool(from_num), repr(from_num or "(empty)"))
    check("VOBIZ_PUBLIC_BASE_URL set", bool(base_url), repr(base_url or "(empty)"))

    section("3. VOBIZ API CONNECTIVITY")
    if auth_id and auth_token:
        import httpx
        url = f"https://api.vobiz.ai/api/v1/Account/{auth_id}/"
        headers = {"X-Auth-ID": auth_id, "X-Auth-Token": auth_token}
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(url, headers=headers)
                check("Vobiz API reachable", r.status_code < 400, f"HTTP {r.status_code}")
                if r.status_code < 400:
                    try:
                        data = r.json()
                        balance = data.get("balance") or data.get("credits") or data.get("account", {}).get("balance")
                        check("Account balance", balance is not None, f"Balance: {balance}" if balance else "Could not read balance")
                        print(f"       Response keys: {list(data.keys())[:10]}")
                    except Exception:
                        print(f"       Response: {r.text[:300]}")
                else:
                    print(f"       Response: {r.text[:300]}")
        except Exception as e:
            check("Vobiz API reachable", False, str(e))
    else:
        check("Vobiz API test", False, "Skipped — credentials not set")

    section("4. PUBLIC URL / WEBSOCKET REACHABILITY")
    stream_url = settings.vobiz_stream_public_base_url or base_url
    check("VOBIZ_STREAM_PUBLIC_BASE_URL set", bool(settings.vobiz_stream_public_base_url),
          repr(settings.vobiz_stream_public_base_url or "(empty — falls back to PUBLIC_BASE_URL)"))

    if base_url:
        import httpx
        # Check if answer URL is reachable
        answer_test = f"{base_url.rstrip('/')}/vobiz/answer"
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
                r = await c.get(answer_test)
                check("Answer URL reachable", r.status_code < 500,
                      f"HTTP {r.status_code} from {answer_test}")
                if r.status_code < 500:
                    ct = r.headers.get("content-type", "")
                    check("Answer URL returns XML", "xml" in ct.lower() or "text" in ct.lower(),
                          f"Content-Type: {ct}")
        except Exception as e:
            check("Answer URL reachable", False, str(e))

        # Check WebSocket upgrade
        ws_test_url = stream_url.rstrip("/").replace("https://", "wss://").replace("http://", "ws://") + "/ws/vobiz"
        check("WSS URL constructed", True, ws_test_url)

        # Test WebSocket upgrade via HTTP
        import httpx
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
                # Send an Upgrade: websocket header to see if the server supports it
                r = await c.get(
                    ws_test_url,
                    headers={
                        "Upgrade": "websocket",
                        "Connection": "Upgrade",
                        "Sec-WebSocket-Version": "13",
                        "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
                    },
                )
                # If proxy blocks WS, it'll return 200/400/403 instead of 101
                check("WebSocket upgrade supported", r.status_code == 101,
                      f"HTTP {r.status_code} (expected 101 Switching Protocols). "
                      f"If not 101, your hosting proxy blocks WebSocket upgrades!")
                if r.status_code != 101:
                    print(f"       *** THIS IS LIKELY THE ROOT CAUSE ***")
                    print(f"       Hostinger shared hosting proxies typically do NOT support WebSocket.")
                    print(f"       The call answer URL works (HTTP), but media streaming (WebSocket) fails.")
                    print(f"       FIX: Deploy behind a reverse proxy that supports WebSocket (nginx, Caddy, Cloudflare Tunnel)")
        except Exception as e:
            check("WebSocket upgrade", False, str(e))

    section("5. HOSTING ENVIRONMENT")
    hostname = base_url.split("//")[-1].split("/")[0] if base_url else ""
    is_hstgr = "hstgr" in hostname.lower()
    is_hostinger_shared = is_hstgr and ".srv" in hostname.lower()
    check("Hostinger shared hosting detected", not is_hostinger_shared,
          f"Domain: {hostname}" + (" — SHARED HOSTING DOES NOT SUPPORT WEBSOCKET!" if is_hostinger_shared else ""))

    section("6. CONFIG WARNINGS")
    problems = settings.__class__.__call__.__qualname__  # just for reference
    from config import validate_critical_config
    probs = validate_critical_config()
    if probs:
        for p in probs:
            check("Config", False, p)
    else:
        check("Config validation", True, "No issues found")

    section("7. RECOMMENDATIONS")
    print("""
  If calls don't connect (ring then drop, or no ring at all):

  A) WebSocket proxy issue (MOST LIKELY):
     Your domain (dataedge.srv1003582.hstgr.cloud) is on Hostinger shared hosting.
     Hostinger's nginx proxy does NOT forward WebSocket upgrade requests.
     → Calls will be initiated (HTTP answer URL works) but media streaming fails.

     RECOMMENDED FIX FOR YOUR VPS (31.97.186.20):
     Set both URLs in .env to use the direct VPS IP and port 8001:
       VOBIZ_PUBLIC_BASE_URL=http://31.97.186.20:8001
       VOBIZ_STREAM_PUBLIC_BASE_URL=http://31.97.186.20:8001

     Alternative Options:
     1. Use Cloudflare Tunnel: cloudflared tunnel --url http://localhost:8001
        Set VOBIZ_STREAM_PUBLIC_BASE_URL to the tunnel URL
     2. Use Nginx with WebSocket proxy_set_header Upgrade $http_upgrade on the VPS

  B) If the server isn't running:
     cd /root/vernika/backend
     source ../venv/bin/activate
     uvicorn main:app --host 0.0.0.0 --port 8001

  C) Check server logs for errors:
     tail -100 /root/vernika/backend/server.log
""")

    section("8. QUICK TEST — Try a manual call via API")
    if auth_id and auth_token and base_url:
        import httpx
        try:
            test_body = {
                "from": from_num.lstrip("+") if from_num else "",
                "to": "+918065481138",  # test number
                "answer_url": f"{base_url}/vobiz/answer?camp_id=diagnostic_test",
                "answer_method": "POST",
                "time_limit": 60,
            }
            url = f"https://api.vobiz.ai/api/v1/Account/{auth_id}/Call/"
            headers = {
                "X-Auth-ID": auth_id,
                "X-Auth-Token": auth_token,
                "Content-Type": "application/json",
            }
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(url, json=test_body, headers=headers)
                data = r.json() if r.status_code < 500 else {"raw": r.text}
                check("Test call API call", r.status_code < 400,
                      f"HTTP {r.status_code}: {json.dumps(data)[:300]}")
                if r.status_code >= 400:
                    print(f"       *** Vobiz API rejected the call — check balance/auth ***")
        except Exception as e:
            check("Test call API call", False, str(e))
    else:
        check("Test call", False, "Skipped — missing credentials")


if __name__ == "__main__":
    asyncio.run(diagnose())
