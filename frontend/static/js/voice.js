let liveWs = null;
let liveAudioContext = null;
let liveProcessor = null;
let liveMicrophone = null;
let liveNextPlayTime = 0;
let liveActiveSources = [];
const JITTER_BUFFER_MS = 100; // 100ms client-side jitter buffer

function resampleTo16k(inputData, fromSampleRate) {
    if (fromSampleRate === 16000) {
        return inputData;
    }
    const ratio = fromSampleRate / 16000;
    const newLength = Math.round(inputData.length / ratio);
    const result = new Float32Array(newLength);
    for (let i = 0; i < newLength; i++) {
        const pos = i * ratio;
        const left = Math.floor(pos);
        const right = Math.min(inputData.length - 1, left + 1);
        const w = pos - left;
        result[i] = inputData[left] * (1 - w) + inputData[right] * w;
    }
    return result;
}

function teardownLiveAudio() {
    liveActiveSources.forEach((src) => {
        try { src.stop(); } catch (_) {}
    });
    liveActiveSources = [];
    liveNextPlayTime = 0;
    if (liveProcessor) {
        try { liveProcessor.disconnect(); } catch (_) {}
        liveProcessor = null;
    }
    if (liveMicrophone) {
        try {
            liveMicrophone.mediaStream.getTracks().forEach((t) => t.stop());
        } catch (_) {}
        try { liveMicrophone.disconnect(); } catch (_) {}
        liveMicrophone = null;
    }
    if (liveAudioContext) {
        try { liveAudioContext.close(); } catch (_) {}
        liveAudioContext = null;
    }
}

function playLiveResponse(base64) {
    if (!liveAudioContext) return;
    if (liveAudioContext.state === 'suspended') {
        liveAudioContext.resume().catch(() => {});
    }
    const binary = atob(base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    const pcm = new Int16Array(bytes.buffer);
    const floatData = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i++) floatData[i] = pcm[i] / 0x7FFF;
    const buffer = liveAudioContext.createBuffer(1, floatData.length, 16000);
    buffer.getChannelData(0).set(floatData);

    const now = liveAudioContext.currentTime;
    if (liveNextPlayTime < now) {
        liveNextPlayTime = now + JITTER_BUFFER_MS / 1000;
    }

    const source = liveAudioContext.createBufferSource();
    source.buffer = buffer;
    source.connect(liveAudioContext.destination);
    
    liveActiveSources.push(source);
    source.onended = () => {
        const idx = liveActiveSources.indexOf(source);
        if (idx > -1) liveActiveSources.splice(idx, 1);
    };

    source.start(liveNextPlayTime);
    liveNextPlayTime += buffer.duration;
}

function updateLiveWave(data) {
    const bars = document.querySelectorAll('#test-wave .wave-bar');
    if (!bars.length) return;
    const step = Math.max(1, Math.floor(data.length / bars.length));
    for (let i = 0; i < bars.length; i++) {
        const val = Math.abs(data[i * step]) * 40;
        bars[i].style.height = `${Math.max(4, val)}px`;
    }
}

async function initLiveAudio() {
    liveAudioContext = new (window.AudioContext || window.webkitAudioContext)();
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    liveMicrophone = liveAudioContext.createMediaStreamSource(stream);
    liveProcessor = liveAudioContext.createScriptProcessor(2048, 1, 1);
    liveProcessor.onaudioprocess = (e) => {
        const inputData = e.inputBuffer.getChannelData(0);
        const resampled = resampleTo16k(inputData, liveAudioContext.sampleRate);
        const pcmData = new Int16Array(resampled.length);
        for (let i = 0; i < resampled.length; i++) {
            pcmData[i] = Math.max(-1, Math.min(1, resampled[i])) * 0x7FFF;
        }
        if (liveWs && liveWs.readyState === WebSocket.OPEN) {
            const b64 = btoa(String.fromCharCode(...new Uint8Array(pcmData.buffer)));
            liveWs.send(JSON.stringify({ type: 'audio', data: b64 }));
        }
        updateLiveWave(inputData);
    };
    liveMicrophone.connect(liveProcessor);
    liveProcessor.connect(liveAudioContext.destination);
}

// Helper to determine the active role (fallback if apiRoleQ or similar not defined)
function getVoiceTestRole() {
    if (typeof apiRoleQ === 'function') {
        return apiRoleQ();
    }
    // Fallback logic
    const path = window.location.pathname;
    return 'data_edge';
}

async function startLiveTest() {
    if (liveWs && (liveWs.readyState === WebSocket.CONNECTING || liveWs.readyState === WebSocket.OPEN)) {
        return;
    }
    const recCard = document.getElementById('test-recording-card');
    if (recCard) recCard.style.display = 'none';

    const wsProtocol = location.protocol === 'https:' ? 'wss' : 'ws';
    const wsUrl = `${wsProtocol}://${location.host}/ws/voice-test?role=${getVoiceTestRole()}`;
    try {
        teardownLiveAudio();
        liveWs = new WebSocket(wsUrl);
        liveWs.onopen = async () => {
            const statusDisp = document.getElementById('test-display-status');
            const statusLbl = document.getElementById('test-status');
            const startBtn = document.getElementById('test-start-btn');
            const stopBtn = document.getElementById('test-stop-btn');
            
            if (statusDisp) statusDisp.textContent = 'Connected — speak now';
            if (statusLbl) {
                statusLbl.textContent = 'Live';
                statusLbl.style.color = 'var(--success)';
            }
            if (startBtn) startBtn.disabled = true;
            if (stopBtn) stopBtn.disabled = false;
            try {
                await initLiveAudio();
            } catch (err) {
                console.error(err);
                alert('Microphone access failed. Check browser permissions.');
                stopLiveTest();
            }
        };
        liveWs.onmessage = (e) => {
            let msg;
            try {
                msg = JSON.parse(e.data);
            } catch (_) {
                return;
            }
            if (msg.type === 'interrupted') {
                const statusDisp = document.getElementById('test-display-status');
                if (statusDisp) statusDisp.textContent = 'Interrupted — listening…';
                liveActiveSources.forEach((src) => {
                    try { src.stop(); } catch (_) {}
                });
                liveActiveSources = [];
                liveNextPlayTime = 0;
                return;
            }
            if (msg.type === 'audio' && msg.data) {
                playLiveResponse(msg.data);
            }
            if (msg.type === 'recording' && msg.data) {
                try {
                    const binary = atob(msg.data);
                    const bytes = new Uint8Array(binary.length);
                    for (let i = 0; i < binary.length; i++) {
                        bytes[i] = binary.charCodeAt(i);
                    }
                    const blob = new Blob([bytes], { type: 'audio/wav' });
                    const url = URL.createObjectURL(blob);
                    
                    const card = document.getElementById('test-recording-card');
                    const player = document.getElementById('test-audio-player');
                    const link = document.getElementById('test-download-link');
                    if (card && player && link) {
                        player.src = url;
                        link.href = url;
                        card.style.display = 'block';
                    }
                } catch (err) {
                    console.error('Failed to parse call recording', err);
                }
            }
        };
        liveWs.onclose = () => resetVoiceTest();
        liveWs.onerror = () => {
            alert('Voice connection failed.');
            stopLiveTest();
        };
    } catch (err) {
        console.error(err);
        alert('Could not start voice test.');
        resetVoiceTest();
    }
}

function stopLiveTest() {
    if (liveWs) {
        liveWs.close();
        liveWs = null;
    }
    teardownLiveAudio();
    resetVoiceTest();
}

function resetVoiceTest() {
    teardownLiveAudio();
    const statusDisp = document.getElementById('test-display-status');
    const statusLbl = document.getElementById('test-status');
    const startBtn = document.getElementById('test-start-btn');
    const stopBtn = document.getElementById('test-stop-btn');
    
    if (statusDisp) statusDisp.textContent = 'Voice Link Inactive';
    if (statusLbl) {
        statusLbl.textContent = 'Ready';
        statusLbl.style.color = 'var(--primary)';
    }
    if (startBtn) startBtn.disabled = false;
    if (stopBtn) stopBtn.disabled = true;
    liveWs = null;
}
