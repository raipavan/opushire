@echo off
echo ========================================================
echo Pushing updated backend & call scripts to VPS 31.97.186.20
echo ========================================================
scp backend/config.py backend/diagnose_calls.py root@31.97.186.20:/root/vernika/backend/
scp backend/api/routes/vobiz.py root@31.97.186.20:/root/vernika/backend/api/routes/
scp backend/services/vobiz_bridge/live_session.py backend/services/vobiz_bridge/gemini_protocol.py backend/services/vobiz_bridge/turn_taking_addon.py root@31.97.186.20:/root/vernika/backend/services/vobiz_bridge/
scp backend/prompts/priya.py root@31.97.186.20:/root/vernika/backend/prompts/
scp backend/scripts/call_number.py root@31.97.186.20:/root/vernika/backend/scripts/
echo.
echo Restarting dataedge.service on VPS...
ssh root@31.97.186.20 "systemctl restart dataedge.service || systemctl restart vernika.service"
echo.
echo ========================================================
echo SUCCESS: VPS 31.97.186.20 updated and restarted!
echo ========================================================
pause
