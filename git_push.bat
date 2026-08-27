@echo off
echo ========================================================
echo Staging and committing all Vobiz Live & VAD fixes...
echo ========================================================
git add backend/config.py backend/diagnose_calls.py backend/api/routes/vobiz.py backend/services/vobiz_bridge/live_session.py backend/services/vobiz_bridge/gemini_protocol.py backend/services/vobiz_bridge/turn_taking_addon.py backend/prompts/priya.py backend/scripts/call_number.py .env.example
git commit -m "Fix Vobiz live call silence, WebSocket upgrade handling, Gemini model ID, and VAD sensitivity tuning"
echo.
echo Pushing to remote git repository...
git push
echo.
echo ========================================================
echo SUCCESS: Changes pushed to Git!
echo ========================================================
pause
