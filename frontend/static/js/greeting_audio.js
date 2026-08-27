/**
 * Greeting TTS: preview (WAV) + record PCM for live PSTN.
 * Depends on globals from api_utils.js: token, authHeaders, apiUrl, showToast
 * Optional: parseApiErrorMessage (restored.js), loadTuning (restored.js), tuningRoleForApi (app.js)
 */
function _greetingField() {
    return document.getElementById('tuning-greeting');
}

/** Role used for tuning/capture API — matches Configuration tab, not a stale toggle. */
function _tuningRoleQ() {
    if (typeof tuningRoleForApi === 'function') {
        return encodeURIComponent(tuningRoleForApi());
    }
    return typeof apiRoleQ === 'function' ? apiRoleQ() : encodeURIComponent('data_edge');
}

async function _tuningApiError(res) {
    if (typeof parseApiErrorMessage === 'function') {
        try {
            return await parseApiErrorMessage(res);
        } catch (_) {}
    }
    const t = await res.text();
    try {
        const j = JSON.parse(t);
        const d = j.detail;
        if (typeof d === 'string') return d;
        if (Array.isArray(d)) return d.map((x) => x.msg || x).join('; ');
        return JSON.stringify(j.detail ?? j);
    } catch {
        return t || 'HTTP ' + res.status;
    }
}

/** True if blob starts with RIFF….WAVE (standard WAV). */
async function _blobLooksLikeWav(blob) {
    if (!blob || blob.size < 44) return false;
    const head = new Uint8Array(await blob.slice(0, 12).arrayBuffer());
    const riff =
        head[0] === 0x52 &&
        head[1] === 0x49 &&
        head[2] === 0x46 &&
        head[3] === 0x46;
    const wave =
        head[8] === 0x57 &&
        head[9] === 0x41 &&
        head[10] === 0x56 &&
        head[11] === 0x45;
    return riff && wave;
}

async function previewGreeting() {
    if (typeof window !== 'undefined' && window.location.protocol === 'file:') {
        showToast('Open the console from the server (e.g. http://127.0.0.1:8000/console), not as a local file.', 'error');
        return;
    }
    const ta = _greetingField();
    const text = (ta && ta.value ? ta.value : '').trim();
    if (!text) {
        showToast('Enter a greeting line to preview.', 'error');
        return;
    }
    const btn = document.getElementById('preview-greeting-btn');
    const modalPreview = document.getElementById('modal-greeting-preview-btn');
    if (btn) {
        btn.disabled = true;
        btn.dataset._label = btn.textContent;
        btn.textContent = '…';
    }
    if (modalPreview) {
        modalPreview.disabled = true;
        modalPreview.textContent = '…';
    }
    try {
        const res = await fetch(
            apiUrl('/api/tuning/preview-greeting?role=' + _tuningRoleQ()),
            {
                method: 'POST',
                headers: authHeaders(),
                credentials: 'same-origin',
                body: JSON.stringify({ greeting_text: text }),
            }
        );
        if (!res.ok) {
            let msg = await _tuningApiError(res);
            if (res.status === 404) {
                msg +=
                    ' Open /docs and confirm POST /api/tuning/preview-greeting exists.';
            }
            showToast(msg, 'error');
            return;
        }
        const blob = await res.blob();
        const wavOk = await _blobLooksLikeWav(blob);
        if (!wavOk) {
            showToast('Preview response was not WAV — check server logs.', 'error');
            return;
        }
        const url = URL.createObjectURL(new Blob([blob], { type: 'audio/wav' }));
        const audio = new Audio(url);
        audio.onended = function () {
            URL.revokeObjectURL(url);
        };
        try {
            await audio.play();
        } catch (playErr) {
            URL.revokeObjectURL(url);
            showToast(
                (playErr && playErr.message)
                    ? playErr.message
                    : 'Preview saved but browser blocked autoplay — check popup/blockers.',
                'warning',
                6000
            );
            return;
        }
        showToast('Playing TTS preview (REST — may differ from Live on calls).', 'success');
    } catch (e) {
        showToast((e && e.message) ? e.message : 'Preview failed', 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = btn.dataset._label || 'Preview';
        }
        if (modalPreview) {
            modalPreview.disabled = false;
            modalPreview.textContent = 'Preview';
        }
    }
}

function recordGreeting() {
    if (typeof openModal === 'function') {
        openModal('modal-record');
        return;
    }
    const el = document.getElementById('modal-record');
    if (el) el.classList.add('open', 'active');
}

async function captureLiveGreeting() {
    if (typeof window !== 'undefined' && window.location.protocol === 'file:') {
        showToast('Open the console from the server (e.g. http://127.0.0.1:8000/console), not as a local file.', 'error');
        return;
    }
    const ta = _greetingField();
    const text = (ta && ta.value ? ta.value : '').trim();
    if (!text) {
        showToast('Enter greeting text before capturing Live audio.', 'error');
        return;
    }
    const roleQ = _tuningRoleQ();
    const btn = document.getElementById('capture-live-greeting-btn');
    const modalBtn = document.getElementById('modal-capture-live-btn');
    const setBusy = function (on) {
        [btn, modalBtn].forEach(function (el) {
            if (!el) return;
            el.disabled = on;
            if (on) {
                el.dataset._label = el.textContent;
                el.textContent = 'Capturing… (up to ~60s)';
            } else {
                el.textContent = el.dataset._label || 'Capture Live';
            }
        });
    };
    setBusy(true);
    try {
        const res = await fetch(
            apiUrl('/api/tuning/capture-greeting-live?role=' + roleQ),
            {
                method: 'POST',
                headers: authHeaders(),
                credentials: 'same-origin',
                body: JSON.stringify({ greeting_text: text }),
            }
        );
        const ct = (res.headers.get('Content-Type') || '').toLowerCase();
        if (!res.ok) {
            let msg = await _tuningApiError(res);
            if (res.status === 404) {
                msg += ' Deploy latest backend (POST /api/tuning/capture-greeting-live).';
            } else if (res.status === 503) {
                msg += ' Try again in a few seconds, or use the mic icon (same Live engine).';
            }
            showToast(msg, 'error', 8000);
            return;
        }
        if (ct.includes('json')) {
            const errBody = await _tuningApiError(res);
            showToast(errBody || 'Capture failed', 'error', 8000);
            return;
        }
        const bytesHdr = res.headers.get('X-Greeting-Bytes');
        const pathHdr = res.headers.get('X-Greeting-Path') || '';
        const roleHdr = res.headers.get('X-Role') || '';
        const blob = await res.blob();
        if (!blob || blob.size < 44) {
            showToast('Empty capture response — check server logs.', 'error');
            return;
        }
        const wavOk = await _blobLooksLikeWav(blob);
        if (!wavOk) {
            showToast(
                'Capture response was not WAV (got ' + blob.size + ' bytes). Hard refresh and retry.',
                'error',
                8000
            );
            return;
        }
        const url = URL.createObjectURL(new Blob([blob], { type: 'audio/wav' }));
        const audio = new Audio(url);
        let played = false;
        audio.onended = function () {
            URL.revokeObjectURL(url);
        };
        try {
            await audio.play();
            played = true;
        } catch (playErr) {
            // Autoplay policy — PCM is still saved on the server.
            console.warn('Capture WAV autoplay blocked:', playErr);
        }
        const roleLabel = roleHdr || decodeURIComponent(roleQ);
        const detail = bytesHdr
            ? bytesHdr + ' bytes' + (pathHdr ? ' → ' + pathHdr.split('/').pop() : '')
            : 'saved';
        showToast(
            (played ? 'Live capture OK — playing. ' : 'Live capture saved (browser blocked autoplay). ')
                + detail
                + (roleLabel ? ' [' + roleLabel + ']' : ''),
            'success',
            7000
        );
        if (typeof closeModal === 'function') closeModal('modal-record');
        else {
            const m = document.getElementById('modal-record');
            if (m) {
                m.classList.remove('open');
                m.classList.remove('active');
            }
        }
        if (typeof loadTuning === 'function') await loadTuning();
    } catch (e) {
        showToast((e && e.message) ? e.message : 'Live capture failed — network or timeout', 'error', 8000);
    } finally {
        setBusy(false);
    }
}

async function confirmRecordGreeting() {
    if (typeof window !== 'undefined' && window.location.protocol === 'file:') {
        showToast('Open the console from the server (e.g. http://127.0.0.1:8000/console), not as a local file.', 'error');
        return;
    }
    const ta = _greetingField();
    const text = (ta && ta.value ? ta.value : '').trim();
    if (!text) {
        showToast('Enter greeting text before generating audio.', 'error');
        return;
    }
    const btn = document.getElementById('record-greeting-btn');
    const modalSave = document.getElementById('modal-record-generate-btn');
    if (btn) btn.style.background = '#FF9500';
    if (modalSave) {
        modalSave.disabled = true;
        modalSave.textContent = 'Generating…';
    }
    try {
        const res = await fetch(apiUrl('/api/tuning/record-greeting?role=' + _tuningRoleQ()), {
            method: 'POST',
            headers: authHeaders(),
            credentials: 'same-origin',
            body: JSON.stringify({ greeting_text: text }),
        });
        if (!res.ok) {
            let msg = await _tuningApiError(res);
            if (res.status === 404) {
                msg +=
                    ' Open /docs and confirm POST /api/tuning/record-greeting exists, or set vernika-api-root if using a path prefix.';
            }
            showToast(msg, 'error');
            return;
        }
        let data = {};
        try {
            data = await res.json();
        } catch (_) {}
        if (btn) btn.style.background = '#34C759';
        const eng = data.engine === 'live' ? ' (Live voice)' : '';
        showToast(
            data.bytes
                ? 'Greeting audio saved (' + data.bytes + ' bytes)' + eng + '.'
                : 'Greeting audio saved.',
            'success'
        );
        if (typeof closeModal === 'function') closeModal('modal-record');
        else {
            const m = document.getElementById('modal-record');
            if (m) {
                m.classList.remove('open');
                m.classList.remove('active');
            }
        }
        if (typeof loadTuning === 'function') await loadTuning();
        setTimeout(function () {
            if (btn) btn.style.background = 'var(--primary)';
        }, 2000);
    } catch (e) {
        showToast((e && e.message) ? e.message : 'Recording failed', 'error');
        if (btn) btn.style.background = 'var(--primary)';
    } finally {
        if (modalSave) {
            modalSave.disabled = false;
            modalSave.textContent = 'Generate & Save';
        }
    }
}
