// --- Restored Missing Functions ---

function downloadFilteredCSV() {
    const search = (document.getElementById('search-input')?.value || '').toLowerCase().trim();
    const dateFromVal = document.getElementById('filter-date-from')?.value;
    const dateToVal = document.getElementById('filter-date-to')?.value;
    const dateFrom = dateFromVal ? new Date(dateFromVal + 'T00:00:00') : null;
    const dateTo = dateToVal ? new Date(dateToVal + 'T23:59:59') : null;

    // Re-run same filter logic as renderCalls/renderManifest
    let rows = allLeads;
    if (currentFilter !== 'all') {
        rows = rows.filter(isCalled);
        if (currentFilter === 'failed') {
            rows = rows.filter(isFailed);
        } else if (currentFilter === 'star4') {
            rows = rows.filter(l => (l.rating || 0) >= 4);
        } else if (currentFilter === 'Call Later') {
            rows = rows.filter(l => {
                const d = effectiveDispo(l);
                return d === 'Call Later' || d === 'Callback' || d === 'Busy';
            });
        } else {
            const isRealEstate = typeof currentRole !== 'undefined' && currentRole === 'real_estate';
            if (isRealEstate && currentFilter === 'Not Interested') {
                rows = rows.filter(l => effectiveDispo(l) === 'Interested');
            } else {
                rows = rows.filter(l => effectiveDispo(l) === currentFilter);
            }
        }
    }
    if (dateFrom || dateTo) {
        rows = rows.filter(l => {
            const ts = l.start_time || (l.called_at_iso ? new Date(l.called_at_iso).getTime()/1000 : null);
            if (!ts) return false;
            const dt = new Date(ts * 1000);
            if (dateFrom && dt < dateFrom) return false;
            if (dateTo && dt > dateTo) return false;
            return true;
        });
    }
    if (search) {
        rows = rows.filter(l => {
            const p = typeof leadContactPrimary === 'function' ? leadContactPrimary(l) : (l.name || '');
            const s2 = typeof leadContactSecondary === 'function' ? leadContactSecondary(l) : (l.company || '');
            return (l.name || '').toLowerCase().includes(search)
                || (p || '').toLowerCase().includes(search)
                || (s2 || '').toLowerCase().includes(search)
                || (l.phone || '').toLowerCase().includes(search)
                || (l.company || '').toLowerCase().includes(search)
                || (l.summary || '').toLowerCase().includes(search);
        });
    }
    rows.sort((a, b) => (b.start_time || 0) - (a.start_time || 0));

    if (!rows.length) {
        showToast('No records to export for current filters.', 'info');
        return;
    }

    // Build CSV
    const IST = { label: 'IST', offset: 5.5 * 60 * 60 * 1000 };
    const headers = ['Name', 'Phone', 'Email', 'Company', 'Status', 'Disposition', 'Rating', 'Called At (IST)', 'Failure Reason', 'Summary'];
    const lines = rows.map(r => {
        const failure = r.failure_title || r.failure_reason || '';
        const calledAt = r.start_time
            ? new Date(r.start_time * 1000 + IST.offset).toISOString().slice(0,19).replace('T',' ') + ' IST'
            : (r.called_at_iso ? new Date(new Date(r.called_at_iso).getTime() + IST.offset).toISOString().slice(0,19).replace('T',' ') + ' IST' : '');
        const cells = [
            r.name || '',
            r.phone || '',
            r.email || '',
            r.company || '',
            r.status || '',
            r.disposition || '',
            r.rating || '',
            calledAt,
            failure,
            (r.summary || '').replace(/\n/g, ' ')
        ];
        return cells.map(c => '"' + String(c).replace(/"/g, '""') + '"').join(',');
    });

    const csv = [headers.join(','), ...lines].join('\n');
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    const today = new Date().toISOString().slice(0,10);
    const filt = currentFilter !== 'all' ? `-${currentFilter}` : '';
    a.download = `vernika-${currentRole}${filt}-${today}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    showToast(`Exported ${rows.length} rows.`, 'success');
}

// ─── Call Detail Modal ───

let _cdRecordingBlobUrl = null;

function revokeCdRecordingBlobUrl() {
    if (_cdRecordingBlobUrl) {
        URL.revokeObjectURL(_cdRecordingBlobUrl);
        _cdRecordingBlobUrl = null;
    }
}

async function prepCallDetailRecording(lead) {
    revokeCdRecordingBlobUrl();
    const block = document.getElementById('cd-recording-block');
    const audio = document.getElementById('cd-audio');
    if (!block || !audio) return;

    const media = typeof resolveLeadMediaContext === 'function'
        ? resolveLeadMediaContext(lead)
        : { leadId: lead && lead.id, logId: String((lead && (lead._log_id || lead.log_id)) || ''), hasMedia: !!(lead && (lead.recording_available || lead._log_id || lead.log_id)) };
    if (!media.hasMedia || media.leadId == null) {
        block.style.display = 'none';
        audio.removeAttribute('src');
        audio.style.display = 'none';
        return;
    }

    block.style.display = 'block';
    const msgId = 'cd-recording-msg';
    let msgEl = document.getElementById(msgId);
    if (!msgEl) {
        msgEl = document.createElement('p');
        msgEl.id = msgId;
        msgEl.style.cssText = 'margin:8px 0 0;font-size:12px;color:var(--text-secondary);';
        block.appendChild(msgEl);
    }
    msgEl.textContent = 'Loading recording…';
    audio.style.display = 'none';
    audio.removeAttribute('src');

    function showRecordingError(msg) {
        revokeCdRecordingBlobUrl();
        audio.removeAttribute('src');
        audio.style.display = 'none';
        block.style.display = 'block';
        msgEl.textContent = msg || 'Recording not available for this call.';
    }

    function attachStreamSrc() {
        if (typeof campaignRecordingStreamUrl !== 'function') return false;
        const streamUrl = campaignRecordingStreamUrl(media.leadId);
        return new Promise(function (resolve, reject) {
            let settled = false;
            const onReady = function () {
                if (settled) return;
                settled = true;
                audio.removeEventListener('loadedmetadata', onReady);
                audio.removeEventListener('canplay', onReady);
                audio.removeEventListener('error', onErr);
                if (audio.duration && Number.isFinite(audio.duration) && audio.duration > 0) {
                    resolve(true);
                } else {
                    reject(new Error('Recording loaded but duration is zero.'));
                }
            };
            const onErr = function () {
                if (settled) return;
                settled = true;
                audio.removeEventListener('loadedmetadata', onReady);
                audio.removeEventListener('canplay', onReady);
                audio.removeEventListener('error', onErr);
                reject(new Error('Browser could not play this recording.'));
            };
            audio.addEventListener('loadedmetadata', onReady);
            audio.addEventListener('canplay', onReady);
            audio.addEventListener('error', onErr);
            audio.preload = 'auto';
            audio.src = streamUrl;
            audio.style.display = 'block';
            audio.load();
            setTimeout(function () {
                if (!settled && audio.readyState >= 1 && audio.duration > 0) onReady();
            }, 12000);
        });
    }

    try {
        await attachStreamSrc();
        msgEl.textContent = '';
        return;
    } catch (_streamErr) {
        /* fall through to blob fetch */
    }

    try {
        const res = await fetch(apiUrl(`/api/campaign/lead/${media.leadId}/recording?role=${apiRoleQ()}`), {
            headers: { 'Authorization': `Bearer ${token()}` },
            credentials: 'same-origin',
        });
        if (!res.ok) {
            const errText = await res.text().catch(function () { return ''; });
            throw new Error(errText || ('Recording not available (HTTP ' + res.status + ')'));
        }
        let blob = await res.blob();
        if (!blob || !blob.size) throw new Error('Recording file is empty.');
        if (!blob.type || blob.type === 'application/octet-stream') {
            blob = new Blob([blob], { type: 'audio/wav' });
        }
        _cdRecordingBlobUrl = URL.createObjectURL(blob);
        audio.src = _cdRecordingBlobUrl;
        audio.style.display = 'block';
        audio.load();
        msgEl.textContent = '';
    } catch (err) {
        showRecordingError((err && err.message) ? String(err.message) : 'Recording not available for this call.');
    }
}

async function loadCallDetailTranscript(lead, transEl) {
    const legacyUrl = lead.transcript_url;
    const media = typeof resolveLeadMediaContext === 'function'
        ? resolveLeadMediaContext(lead)
        : { leadId: lead && lead.id, hasMedia: !!(lead && (lead.log_id || lead._log_id)) };
    const securedUrl =
        media.leadId != null && media.hasMedia
            ? apiUrl(`/api/campaign/lead/${media.leadId}/transcript?role=${apiRoleQ()}`)
            : null;

    if (!securedUrl && !legacyUrl) {
        transEl.innerHTML = `<p style="font-size:12px;color:var(--text-secondary);text-align:center;margin:20px 0;">No transcript recorded for this call.</p>`;
        return;
    }
    try {
        const url = securedUrl || legacyUrl;
        const res = await fetch(url, {
            headers: { 'Authorization': `Bearer ${token()}` },
            credentials: 'same-origin',
        });
        if (!res.ok) throw new Error('Transcript not available (HTTP ' + res.status + ')');
        const text = await res.text();
        renderTranscript(text);
    } catch (err) {
        const msg = (err && err.message) ? String(err.message) : 'Transcript not available.';
        transEl.innerHTML = `<p style="font-size:12px;color:var(--text-secondary);text-align:center;margin:20px 0;">${escapeHtml(msg)}</p>`;
    }
}

let currentCallLead = null;
async function openCallDetail(leadId) {
    const lead = typeof findLeadById === 'function' ? findLeadById(leadId) : allLeads.find(function (l) { return Number(l.id) === Number(leadId); });
    if (!lead) return;
    currentCallLead = lead;

    document.getElementById('cd-title').textContent = lead.name || 'Unknown contact';
    const subParts = [];
    if (lead.phone) subParts.push(lead.phone);
    if (lead.company) subParts.push(lead.company);
    if (lead.email) subParts.push(lead.email);
    document.getElementById('cd-subtitle').textContent = subParts.join(' • ') || '';

    const dispo = effectiveDispo(lead) || '—';
    const fromTrans = !!lead.outcome_from_transcript;
    document.getElementById('cd-outcome-heading').textContent = fromTrans ? 'Outcome (transcript QA)' : 'Outcome';
    const hintEl = document.getElementById('cd-outcome-hint');
    hintEl.textContent = fromTrans
        ? 'Label is inferred only from what the caller said in the transcript below.'
        : 'When a transcript is saved and analyzed, the outcome is set from that text. Use Re-analyze if this call already has a log.';
    document.getElementById('cd-outcome').innerHTML = `<span class="badge-tag ${escapeHtml(dispoTagClass(dispo))}">${escapeHtml(dispo)}</span>`;
    document.getElementById('cd-rating').innerHTML = lead.rating
        ? starsHtml(lead.rating) + ` <span style="font-size:12px;color:var(--text-secondary);font-weight:600;">${escapeHtml(lead.rating)}/5</span>`
        : '<span style="color:var(--text-secondary);font-size:13px;">—</span>';
    fillCallFailureModal(lead);

    revokeCdRecordingBlobUrl();
    document.getElementById('cd-recording-block').style.display = 'none';
    const cdAud = document.getElementById('cd-audio');
    if (cdAud) cdAud.removeAttribute('src');

    document.getElementById('cd-summary').textContent = lead.summary || 'No summary generated for this call yet.';
    const emoBlock = document.getElementById('cd-emotion-block');
    const emoEl = document.getElementById('cd-emotion');
    const emoLabel = (lead.emotion_label || (lead.analysis && lead.analysis.emotion_label) || '').trim();
    const emoRat = (lead.emotion_rationale || (lead.analysis && lead.analysis.emotion_rationale) || '').trim();
    if (emoBlock && emoEl && (emoLabel || emoRat)) {
        emoBlock.style.display = 'block';
        let emoTxt = emoLabel || 'Unknown';
        if (lead.emotion_confidence != null && !isNaN(Number(lead.emotion_confidence))) {
            emoTxt += ' (' + Math.round(Number(lead.emotion_confidence) * 100) + '% confidence)';
        }
        if (emoRat) emoTxt += ' — ' + emoRat;
        emoEl.textContent = emoTxt;
    } else if (emoBlock) {
        emoBlock.style.display = 'none';
    }
    if (lead.next_steps) {
        document.getElementById('cd-next-block').style.display = 'block';
        document.getElementById('cd-next').textContent = typeof lead.next_steps === 'string' ? lead.next_steps : String(lead.next_steps || '');
    } else {
        document.getElementById('cd-next-block').style.display = 'none';
    }

    const transEl = document.getElementById('cd-transcript');
    transEl.innerHTML = '<p style="font-size:12px;color:var(--text-secondary);text-align:center;margin:20px 0;">Loading transcript…</p>';
    openModal('modal-call');

    await Promise.all([
        prepCallDetailRecording(lead),
        loadCallDetailTranscript(lead, transEl),
    ]);
}

function renderTranscript(rawText) {
    const transEl = document.getElementById('cd-transcript');
    const lines = (rawText || '').split('\n').filter(l => l.trim());
    const turns = [];
    for (const line of lines) {
        try {
            const obj = JSON.parse(line);
            const role = obj.role || obj.type || '';
            const content = obj.content || obj.text || obj.message || '';
            if ((role === 'user' || role === 'assistant') && content) {
                turns.push({ role, content: String(content).trim() });
            }
        } catch {}
    }
    if (!turns.length) {
        const plain = lines.map(function (l) { return l.trim(); }).filter(Boolean);
        if (plain.length) {
            transEl.innerHTML = '<pre style="margin:0;font-size:12px;line-height:1.45;white-space:pre-wrap;word-break:break-word;color:var(--text);">' +
                escapeHtml(plain.join('\n')) + '</pre>';
            transEl.scrollTop = 0;
            return;
        }
        transEl.innerHTML = `<p style="font-size:12px;color:var(--text-secondary);text-align:center;margin:20px 0;">Transcript is empty.</p>`;
        return;
    }
    transEl.innerHTML = turns.map(t => `
        <div style="display:flex;flex-direction:column;align-items:${t.role === 'user' ? 'flex-end' : 'flex-start'};">
            <span class="bubble-meta">${t.role === 'user' ? 'Caller' : 'Devika'}</span>
            <div class="bubble ${escapeHtml(t.role)}">${escapeHtml(t.content)}</div>
        </div>
    `).join('');
    transEl.scrollTop = 0;
}

async function reanalyzeCall() {
    if (!currentCallLead) return;
    const btn = document.getElementById('cd-reanalyze');
    btn.disabled = true;
    const oldText = btn.textContent;
    btn.textContent = 'Analyzing…';
    try {
        const res = await fetch(apiUrl(`/api/campaign/lead/${currentCallLead.id}/analyze?role=${apiRoleQ()}`), {
            method: 'POST',
            headers: authHeaders(),
            credentials: 'same-origin',
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            const d = data.detail;
            let msg = 'Analysis failed';
            if (typeof d === 'string') msg = d;
            else if (Array.isArray(d)) {
                msg = d.map(function (x) { return x && x.msg ? x.msg : JSON.stringify(x); }).join('; ') || msg;
            } else if (d) msg = String(d);
            throw new Error(msg);
        }
        if (data.lead && data.lead.id != null) {
            const idx = allLeads.findIndex(l => Number(l.id) === Number(data.lead.id));
            if (idx >= 0) {
                allLeads[idx] = Object.assign({}, allLeads[idx], data.lead);
                currentCallLead = allLeads[idx];
            } else if (currentCallLead && Number(currentCallLead.id) === Number(data.lead.id)) {
                currentCallLead = Object.assign({}, currentCallLead, data.lead);
            }
        }
        await syncState();
        // Refresh modal with newest lead data (recording + transcript + Gemma QA fields)
        openCallDetail(currentCallLead.id);
        btn.textContent = '✓ Done';
        setTimeout(() => { btn.textContent = oldText; btn.disabled = false; }, 1500);
    } catch (e) {
        alert('Re-analyze failed: ' + (e.message || e));
        btn.textContent = oldText;
        btn.disabled = false;
    }
}

// ─── Render Lead Manifest ───
const MANIFEST_TABLE_ROW_CAP = 10000;

function renderManifest() {
    const tbody = document.getElementById('manifest-tbody');
    if (!tbody) return;
    if (!allLeads.length) {
        tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;padding:40px;color:var(--text-secondary);">No leads uploaded</td></tr>`;
        const countEl = document.getElementById('manifest-count');
        if (countEl) countEl.textContent = '0 leads';
        return;
    }
    const search = (document.getElementById('search-input')?.value || '').toLowerCase().trim();
    let filtered = allLeads.filter(function (l) { return l && l.id != null && Number.isFinite(Number(l.id)); });

    if (search) {
        filtered = filtered.filter(function (l) {
            const p = typeof leadContactPrimary === 'function' ? leadContactPrimary(l) : (l.name || '');
            const s2 = typeof leadContactSecondary === 'function' ? leadContactSecondary(l) : (l.company || '');
            return (l.name || '').toLowerCase().includes(search)
                || (p || '').toLowerCase().includes(search)
                || (s2 || '').toLowerCase().includes(search)
                || (l.phone || '').toLowerCase().includes(search)
                || (l.company || '').toLowerCase().includes(search);
        });
    }

    const countEl = document.getElementById('manifest-count');
    if (countEl) {
        if (search) {
            countEl.textContent = `${filtered.length} of ${allLeads.length} leads`;
        } else {
            countEl.textContent = `${allLeads.length} leads`;
        }
    }

    const cap = typeof window.__VERN_MANIFEST_CAP === 'number' ? window.__VERN_MANIFEST_CAP : MANIFEST_TABLE_ROW_CAP;
    const slice = filtered
        .slice()
        .sort(function (a, b) {
            var ta = Number(a.start_time) || 0;
            var tb = Number(b.start_time) || 0;
            if (tb !== ta) return tb - ta;
            return Number(b.id || 0) - Number(a.id || 0);
        })
        .slice(0, cap);
    if (!slice.length && filtered.length) {
        tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;padding:40px;color:var(--text-secondary);">Leads loaded but missing row IDs — try a hard refresh (Ctrl+Shift+R).</td></tr>`;
        return;
    }
    let rowsHtml = slice.map(function (l) {
        const id = l.id;
        const meta = manifestStatusMeta(l);
        const failureHtml = formatFailureCell(l);
        const pname = escapeHtml(typeof leadContactPrimary === 'function' ? leadContactPrimary(l) : (l.name || '—'));
        const pitched = buildClientOpeningLine(l);
        const canView = l.status && ['completed','failed','not_interested','interested','callback','callback_scheduled'].includes((l.status||'').toLowerCase());
        const rowCursor = canView ? 'cursor:pointer;' : '';
        const rowClick = canView ? `onclick="openCallDetail(${Number(id)})"` : '';
        return `<tr ${rowClick} style="${rowCursor}">
            <td style="padding-left:20px;font-weight:600;">
                ${pname}
                <div style="font-size:10px;color:var(--text-secondary);font-weight:400;margin-top:2px;max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(pitched)}">
                    ${escapeHtml(pitched)}
                </div>
            </td>
            <td style="font-family:var(--font-mono);font-size:12px;">${escapeHtml(l.phone || '—')}</td>
            <td style="color:var(--text-secondary);">${escapeHtml(String(l.company || '—'))}</td>
            <td><span class="badge-tag ${escapeHtml(meta.cls)}">${escapeHtml(meta.label)}</span></td>
            <td style="max-width:220px;">${failureHtml}</td>
            <td style="text-align:right;padding-right:20px;" onclick="event.stopPropagation();">
                <button class="btn btn-ghost btn-sm" style="font-size:11px;" onclick="markStatus(${escapeHtml(id)},'completed')">✓</button>
                <button class="btn btn-ghost btn-sm" style="font-size:11px;margin-left:4px;" onclick="markStatus(${escapeHtml(id)},'not_interested')">✗</button>
                <select class="btn btn-ghost btn-sm" style="font-size:10px;margin-left:6px;padding:2px 4px;max-width:95px;" onclick="event.stopPropagation();" onchange="changeDisposition(${escapeHtml(id)}, this.value); this.blur();">
                    <option value="" disabled selected style="color:var(--text-secondary);">Dispo</option>
                    <option value="Answered">Answered</option>
                    <option value="Interested">Interested</option>
                    <option value="Not Interested">Not Interested</option>
                    <option value="Call Later">Call Later</option>
                    <option value="Busy">Busy</option>
                    <option value="Wrong Number">Wrong Number</option>
                </select>
            </td>
        </tr>`;
    }).join('');
    if (allLeads.length > cap) {
        rowsHtml += `<tr><td colspan="6" style="padding:14px;color:var(--text-secondary);font-size:12px;text-align:center;">Showing first <strong>${cap.toLocaleString()}</strong> of <strong>${allLeads.length.toLocaleString()}</strong> in this browser view. Totals and campaign still use full list.</td></tr>`;
    }
    const sig = slice.map(l => l.id + ':' + l.status + ':' + l.disposition).join('|') + '|' + slice.length;
    if (tbody.dataset.sig !== sig) {
        const temp = document.createElement('tbody');
        temp.innerHTML = rowsHtml;
        if (tbody.children.length === temp.children.length) {
            const oldChildren = Array.from(tbody.children);
            const newChildren = Array.from(temp.children);
            for (let i = 0; i < oldChildren.length; i++) {
                if (oldChildren[i].outerHTML !== newChildren[i].outerHTML) {
                    oldChildren[i].outerHTML = newChildren[i].outerHTML;
                }
            }
        } else {
            tbody.innerHTML = rowsHtml;
        }
        tbody.dataset.sig = sig;
    }
}

/** Map raw lead.status + disposition to a friendly badge for the manifest. */
function manifestStatusMeta(lead) {
    const raw = (lead.status || '').trim();
    const s = raw.toLowerCase();
    if (s === 'callback_scheduled') {
        const iso = (lead.callback_reminder_at_iso || '').trim();
        const lbl = iso ? ('Callback scheduled · ' + formatTimeIST(iso)) : 'Callback scheduled';
        return { label: lbl, cls: 'tag-cbk' };
    }
    const dispo = (typeof effectiveDispo === 'function' ? effectiveDispo(lead) : (lead.disposition || '')).trim();
    // Prefer the call disposition when present (it's the post-call outcome).
    if (dispo && dispo !== 'Completed' && dispo !== 'Pending' && dispo !== 'Dialing…') {
        const ds = dispo.toLowerCase();
        if (ds.includes('not interested')) return { label: 'Not Interested', cls: 'tag-noint' };
        if (ds.includes('interested'))     return { label: 'Interested',     cls: 'tag-int' };
        if (ds.includes('wrong'))          return { label: 'Wrong Number',   cls: 'tag-fail' };
        if (ds.includes('call later'))
            return { label: dispo, cls: 'tag-call-later' };
        if (ds.includes('callback') || ds.includes('busy'))
            return { label: dispo, cls: 'tag-cbk' };
        return { label: dispo, cls: 'tag-cbk' };
    }
    // Fall back to the machine status the backend stores.
    if (s === 'completed')       return { label: 'Completed',      cls: 'tag-int' };
    if (s === 'not_interested')  return { label: 'Not Interested', cls: 'tag-noint' };
    if (s === 'interested')      return { label: 'Interested',     cls: 'tag-int' };
    if (s === 'callback')        return { label: 'Callback',       cls: 'tag-cbk' };
    if (s === 'dialing')         return { label: 'Dialing…',       cls: 'tag-cbk' };
    if (s === 'pending' || !s)   return { label: 'Pending',        cls: '' };
    if (s === 'failed' || s === 'error' || s === 'no answer')
        return { label: 'Failed', cls: 'tag-fail' };
    return { label: raw || 'pending', cls: '' };
}

async function parseApiErrorMessage(res) {
    const raw = await res.text().catch(() => '');
    if (!raw) return `Request failed (${res.status})`;
    try {
        const j = JSON.parse(raw);
        const d = j.detail;
        if (typeof d === 'string') return d;
        if (Array.isArray(d) && d.length && typeof d[0] === 'object' && d[0].msg) {
            return d.map((x) => x.msg || '').filter(Boolean).join(' ') || `Request failed (${res.status})`;
        }
        if (j.message) return String(j.message);
    } catch (_) {}
    return raw.slice(0, 500);
}

// ─── Campaign Controls ───
let _campaignSubmitting = false;
let _stopCampaignSubmitting = false;
let _uploadLeadsSubmitting = false;
let _saveTuningSubmitting = false;

async function startCampaign() {
    if (_campaignSubmitting) return;
    _campaignSubmitting = true;
    const btn = document.getElementById('btn-start');
    if (btn && btn.disabled && btn.getAttribute('title')) {
        showToast(btn.getAttribute('title') || 'Campaign cannot start outside calling hours.', 'error');
        _campaignSubmitting = false;
        return;
    }
    if (btn) btn.disabled = true;
    try {
        if (campaignWorkerActive) {
            showToast('Campaign is already running for this role.', 'info');
            return;
        }
        // Use /start (idempotent) instead of /toggle so a stale "alive" task
        // never silently stops the campaign when the user clicks Start.
        const res = await fetch(apiUrl(`/api/campaign/start?role=${apiRoleQ()}`), {
            method: 'POST',
            headers: authHeaders(),
            credentials: 'same-origin',
        });
        if (!res.ok) {
            showToast(await parseApiErrorMessage(res), 'error');
            return;
        }
        const data = await res.json().catch(() => ({}));
        if ((data.status === 'started' || data.status === 'already_running') && data.active) {
            campaignWorkerActive = true;
            const p = data.pending;
            const prefix = data.status === 'already_running' ? 'Campaign already running' : 'Campaign started';
            showToast(
                typeof p === 'number' ? `${prefix} — ${p} lead(s) in the queue.` : `${prefix}.`,
                'success'
            );
        } else {
            showToast('Campaign could not be started. Try again or check pending leads.', 'error');
        }
    } catch (e) {
        showToast((e && e.message) ? e.message : 'Network error', 'error');
    } finally {
        if (btn) btn.disabled = false;
        await syncState();
        _campaignSubmitting = false;
    }
}
async function stopCampaign() {
    if (_stopCampaignSubmitting) return;
    _stopCampaignSubmitting = true;
    const btn = document.getElementById('btn-stop');
    if (btn) btn.disabled = true;
    try {
        const res = await fetch(apiUrl(`/api/campaign/stop?role=${apiRoleQ()}`), {
            method: 'POST',
            headers: authHeaders(),
            credentials: 'same-origin',
        });
        if (!res.ok) {
            showToast(await parseApiErrorMessage(res), 'error');
            return;
        }
        const data = await res.json().catch(() => ({}));
        campaignWorkerActive = false;
        if (data.status === 'stopped' || data.active === false) {
            showToast('Campaign stopped.', 'success');
        } else {
            showToast('Stop acknowledged.', 'info');
        }
    } catch (e) {
        showToast((e && e.message) ? e.message : 'Network error', 'error');
    } finally {
        if (btn) btn.disabled = false;
        await syncState();
        _stopCampaignSubmitting = false;
    }
}
async function executeWipe() {
    if (!confirm('Clear all leads? This cannot be undone.')) return;
    await fetch(apiUrl(`/api/campaign/wipe?role=${apiRoleQ()}`), {
        method: 'POST',
        headers: authHeaders(),
        credentials: 'same-origin',
    });
    syncState();
}
async function markStatus(idx, status) {
    await fetch(apiUrl(`/api/campaign/lead/${idx}/status?role=${apiRoleQ()}`), {
        method: 'POST',
        headers: authHeaders(),
        credentials: 'same-origin',
        body: JSON.stringify({ status }),
    });
    syncState();
}
async function changeDisposition(idx, disposition) {
    if (!disposition) return;
    try {
        const res = await fetch(apiUrl(`/api/campaign/lead/${idx}/disposition?role=${apiRoleQ()}`), {
            method: 'POST',
            headers: { ...authHeaders(), 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({ disposition }),
        });
        if (res.ok) {
            showToast(`Disposition set to ${disposition}`, 'success');
        } else {
            const msg = await parseApiErrorMessage(res);
            showToast(msg || 'Failed to update disposition', 'error');
        }
    } catch (e) {
        showToast((e && e.message) ? e.message : 'Network error', 'error');
    }
    syncState();
}
async function uploadLeads(input) {
    if (_uploadLeadsSubmitting) return;
    _uploadLeadsSubmitting = true;
    const file = input.files && input.files[0];
    if (!file) {
        _uploadLeadsSubmitting = false;
        return;
    }
    showToast(`Uploading "${file.name}"…`, 'info');
    const fd = new FormData(); fd.append('file', file);
    try {
        const res = await fetch(apiUrl(`/api/campaign/upload?role=${apiRoleQ()}`), {
            method: 'POST',
            headers: { Authorization: `Bearer ${token()}` },
            credentials: 'same-origin',
            body: fd,
        });
        if (res.status === 401 && typeof logout === 'function') logout();
        if (!res.ok) {
            showToast(await parseApiErrorMessage(res), 'error');
            return;
        }
        const data = await res.json().catch(() => ({}));
        const count = Number(data.count || 0);
        if (Array.isArray(data.recent) && data.recent.length > 0) {
            allLeads = data.recent;
            renderManifest();
            renderCalls();
            if (typeof persistLeadTablesToSession === 'function') persistLeadTablesToSession();
        }
        const pv = document.getElementById('upload-preview-summary');
        if (pv) {
            if (count > 0) {
                pv.style.display = 'block';
                pv.textContent =
                    `${count.toLocaleString()} row(s) saved. Preview below (Lead Manifest); full list refreshes from server…`;
            } else {
                pv.style.display = 'none';
                pv.textContent = '';
            }
        }
        if (count > 0) {
            showToast(`${count} lead${count === 1 ? '' : 's'} uploaded successfully.`, 'success');
            showCampaignStartModal();
        } else if (data.error) {
            showToast(`Upload finished, but: ${data.error}`, 'info');
        } else {
            showToast('Upload finished — no new valid leads found.', 'info');
        }
        await syncState();
        requestAnimationFrame(function () {
            const a = document.getElementById('lead-manifest-anchor');
            if (a && count > 0) {
                try {
                    a.scrollIntoView({ behavior: 'smooth', block: 'start' });
                } catch (_) {
                    a.scrollIntoView(true);
                }
            }
        });
    } catch (e) {
        showToast((e && e.message) ? e.message : 'Network error during upload', 'error');
    } finally {
        input.value = '';
        _uploadLeadsSubmitting = false;
    }
}
function downloadCSV() {
    downloadFilteredCSV();
}

// ─── Manual Call ───

let _manualCallModalBlobUrl = null;

function revokeManualCallModalBlobUrl() {
    if (_manualCallModalBlobUrl) {
        URL.revokeObjectURL(_manualCallModalBlobUrl);
        _manualCallModalBlobUrl = null;
    }
}

function closeManualCallModal() {
    const m = document.getElementById('manual-call-modal');
    const audio = document.getElementById('manual-call-modal-audio');
    if (audio) {
        audio.pause();
        audio.removeAttribute('src');
        audio.style.display = 'none';
    }
    revokeManualCallModalBlobUrl();
    if (!m) return;
    m.style.display = 'none';
    m.setAttribute('aria-hidden', 'true');
}

async function prepManualCallModalRecording(callId, recordingAvailable) {
    const audio = document.getElementById('manual-call-modal-audio');
    const msg = document.getElementById('manual-call-recording-msg');
    if (!audio || !msg) return;

    revokeManualCallModalBlobUrl();
    audio.pause();
    audio.removeAttribute('src');
    audio.style.display = 'none';
    msg.textContent = '';

    if (!callId) {
        msg.textContent = '';
        return;
    }
    if (!recordingAvailable) {
        msg.textContent = 'No recording saved for this call (recording may be off, still processing, or files rotated).';
        return;
    }
    msg.textContent = 'Loading audio…';

    // Try streaming via <audio src> with access_token first (supports range requests, progressive playback)
    if (typeof manualCallRecordingStreamUrl === 'function') {
        const streamUrl = manualCallRecordingStreamUrl(callId);
        try {
            await new Promise(function (resolve, reject) {
                var settled = false;
                function onReady() { if (!settled) { settled = true; resolve(); } }
                function onErr() { if (!settled) { settled = true; reject(new Error('Stream failed')); } }
                audio.addEventListener('loadedmetadata', onReady);
                audio.addEventListener('canplay', onReady);
                audio.addEventListener('error', onErr);
                audio.preload = 'auto';
                audio.src = streamUrl;
                audio.style.display = 'block';
                audio.load();
                setTimeout(function () {
                    if (!settled && audio.readyState >= 1 && audio.duration > 0) onReady();
                }, 12000);
            });
            msg.textContent = '';
            return;
        } catch (_e) {
            /* fall through to blob fetch */
            audio.pause();
            audio.removeAttribute('src');
            audio.style.display = 'none';
        }
    }

    // Fallback: fetch entire blob (works with Bearer token)
    try {
        const res = await fetch(apiUrl(`/api/manual/calls/${callId}/recording?role=${apiRoleQ()}`), {
            headers: { 'Authorization': `Bearer ${token()}` },
            credentials: 'same-origin',
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            msg.textContent = (typeof err.detail === 'string' && err.detail) || 'Recording could not be loaded.';
            return;
        }
        const blob = await res.blob();
        _manualCallModalBlobUrl = URL.createObjectURL(blob);
        audio.src = _manualCallModalBlobUrl;
        audio.style.display = 'block';
        msg.textContent = '';
    } catch (e) {
        msg.textContent = (e && e.message) ? e.message : 'Could not load recording.';
    }
}

async function manualCallSetDisposition(disposition) {
    const id = window.__manualModalCallId;
    if (id == null || id === '') return;
    const statusEl = document.getElementById('manual-call-disposition-status');
    const btnInterested = document.getElementById('manual-call-btn-interested');
    const btnNotInterested = document.getElementById('manual-call-btn-not-interested');
    if (statusEl) {
        statusEl.style.display = 'block';
        statusEl.textContent = 'Saving…';
        statusEl.style.color = '#64748b';
    }
    if (btnInterested) btnInterested.disabled = true;
    if (btnNotInterested) btnNotInterested.disabled = true;
    try {
        const res = await fetch(apiUrl(`/api/manual/calls/${id}/disposition?role=${apiRoleQ()}`), {
            method: 'POST',
            headers: { ...authHeaders(), 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({ disposition: disposition }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            if (res.status === 401 && typeof logout === 'function') logout();
            throw new Error(
                typeof data.detail === 'string'
                    ? data.detail
                    : (data.detail?.[0]?.msg || res.statusText || 'Failed to save disposition')
            );
        }
        openManualCallModal(data);
        if (statusEl) {
            statusEl.textContent = disposition === 'Interested'
                ? 'Saved: WhatsApp details will be sent.'
                : 'Saved: Not Interested.';
            statusEl.style.color = disposition === 'Interested' ? '#16a34a' : '#64748b';
        }
        showToast(`Marked as ${disposition}.`, 'success');
        loadRecentManualCalls();
    } catch (e) {
        if (statusEl) {
            statusEl.textContent = (e && e.message) ? e.message : 'Failed to save';
            statusEl.style.color = '#dc2626';
        }
        showToast((e && e.message) ? e.message : 'Failed to save disposition', 'error');
    } finally {
        if (btnInterested) btnInterested.disabled = false;
        if (btnNotInterested) btnNotInterested.disabled = false;
    }
}

async function manualCallModalReanalyze() {
    const id = window.__manualModalCallId;
    if (id == null || id === '') return;
    const btn = document.getElementById('manual-call-reanalyze-btn');
    const oldLabel = btn ? btn.textContent : '';
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Analyzing…';
    }
    try {
        const res = await fetch(apiUrl(`/api/manual/calls/${id}/reanalyze?role=${apiRoleQ()}`), {
            method: 'POST',
            headers: authHeaders(),
            credentials: 'same-origin',
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            if (res.status === 401 && typeof logout === 'function') logout();
            throw new Error(
                typeof data.detail === 'string'
                    ? data.detail
                    : (data.detail?.[0]?.msg || res.statusText || 'Re-analyze failed')
            );
        }
        openManualCallModal(data);
        showToast('Summary updated.', 'success');
        loadRecentManualCalls();
    } catch (e) {
        showToast((e && e.message) ? e.message : 'Re-analyze failed', 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = oldLabel || 'Re-analyze';
        }
    }
}

function renderShareOfVoiceGraph(lines) {
    const container = document.getElementById('manual-call-modal-graph-container');
    const barAssistant = document.getElementById('manual-graph-bar-assistant');
    const barUser = document.getElementById('manual-graph-bar-user');
    const pctAssistant = document.getElementById('manual-graph-pct-assistant');
    const pctUser = document.getElementById('manual-graph-pct-user');
    
    if (!lines || lines.length === 0) {
        container.style.display = 'none';
        return;
    }
    
    let userTokens = 0;
    let assistantTokens = 0;
    
    for (let line of lines) {
        if (line.toLowerCase().startsWith('user:')) {
            userTokens += line.length;
        } else if (line.toLowerCase().startsWith('assistant:')) {
            assistantTokens += line.length;
        }
    }
    
    const total = userTokens + assistantTokens;
    if (total === 0) {
        container.style.display = 'none';
        return;
    }
    
    container.style.display = 'block';
    
    const asstPct = Math.round((assistantTokens / total) * 100);
    const userPct = 100 - asstPct;
    
    barAssistant.style.width = '0%';
    barUser.style.width = '0%';
    
    setTimeout(() => {
        barAssistant.style.width = asstPct + '%';
        barUser.style.width = userPct + '%';
        pctAssistant.textContent = asstPct + '%';
        pctUser.textContent = userPct + '%';
    }, 50);
}

function openManualCallModal(payload) {
    const m = document.getElementById('manual-call-modal');
    if (!m || !payload) return;
    const sub = document.getElementById('manual-call-modal-sub');
    const sum = document.getElementById('manual-call-modal-summary');
    const pre = document.getElementById('manual-call-modal-transcript');
    
    if (sub) sub.textContent = `${escapeHtml(payload.callee_name || '—')} · ${escapeHtml(payload.to_phone || '')} · ${escapeHtml(payload.status || '')}`;
    if (sum) sum.textContent = payload.summary || '—';
    
    // Graph
    renderShareOfVoiceGraph(payload.transcript_lines || []);
    
    // Recording - we rely on prepManualCallModalRecording for this
    var _recRow = document.getElementById('manual-call-recording-row');
    if (_recRow) _recRow.style.display = 'block';

    // Next Action
    const naContainer = document.getElementById('manual-call-next-action-container');
    const naBadge = document.getElementById('manual-call-next-action-badge');
    const naTime = document.getElementById('manual-call-next-action-time');
    const naDetails = document.getElementById('manual-call-next-action-details');
    
    let analysisObj = payload.analysis || {};
    let next_action = analysisObj.next_action;
    let next_steps = payload.next_steps;
    
    if (next_action && next_action.type && next_action.type !== 'None') {
        if (naContainer) naContainer.style.display = 'block';
        const type = next_action.type;
        let badgeHtml = '';
        if (type === 'WhatsApp') {
            badgeHtml = `<span style="display:inline-flex;align-items:center;padding:4px 10px;background:rgba(34,197,94,0.1);color:#15803d;border:1px solid rgba(34,197,94,0.2);border-radius:20px;font-size:11px;font-weight:700;letter-spacing:0.05em;text-transform:uppercase;"><svg style="margin-right:4px;" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"></path></svg> WhatsApp</span>`;
        } else if (type === 'Email') {
            badgeHtml = `<span style="display:inline-flex;align-items:center;padding:4px 10px;background:rgba(59,130,246,0.1);color:#1d4ed8;border:1px solid rgba(59,130,246,0.2);border-radius:20px;font-size:11px;font-weight:700;letter-spacing:0.05em;text-transform:uppercase;"><svg style="margin-right:4px;" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"></path><polyline points="22,6 12,13 2,6"></polyline></svg> Email</span>`;
        } else if (type === 'Call Again') {
            badgeHtml = `<span style="display:inline-flex;align-items:center;padding:4px 10px;background:rgba(249,115,22,0.1);color:#c2410c;border:1px solid rgba(249,115,22,0.2);border-radius:20px;font-size:11px;font-weight:700;letter-spacing:0.05em;text-transform:uppercase;"><svg style="margin-right:4px;" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z"></path></svg> Call Again</span>`;
        } else {
            badgeHtml = `<span style="display:inline-flex;align-items:center;padding:4px 10px;background:rgba(100,116,139,0.1);color:#475569;border:1px solid rgba(100,116,139,0.2);border-radius:20px;font-size:11px;font-weight:700;letter-spacing:0.05em;text-transform:uppercase;">${escapeHtml(type)}</span>`;
        }
        if (naBadge) naBadge.innerHTML = badgeHtml;
        if (naTime) {
            naTime.textContent = next_action.datetime_iso
                ? 'Scheduled: ' + (window.formatTime ? formatTime(next_action.datetime_iso) : next_action.datetime_iso)
                : '';
        }
        if (naDetails) naDetails.textContent = next_action.details || '';

    } else if (next_steps && next_steps !== 'N/A') {
        if (naContainer) naContainer.style.display = 'block';
        if (naBadge) naBadge.innerHTML = `<span style="display:inline-flex;align-items:center;padding:4px 10px;background:rgba(100,116,139,0.1);color:#475569;border:1px solid rgba(100,116,139,0.2);border-radius:20px;font-size:11px;font-weight:700;letter-spacing:0.05em;text-transform:uppercase;">Action Needed</span>`;
        if (naTime) naTime.textContent = '';
        if (naDetails) naDetails.textContent = next_steps;
    } else {
        if (naContainer) naContainer.style.display = 'none';
    }

    if (pre) pre.textContent = (payload.transcript_readable || payload.transcript_raw || '').trim() || '—';
    window.__manualModalCallId = payload.id ?? null;
    const rbtn = document.getElementById('manual-call-reanalyze-btn');
    if (rbtn) {
        rbtn.disabled = !((payload.log_id || '').trim());
        rbtn.title = rbtn.disabled ? 'No transcript session id — nothing to analyze yet.' : '';
    }

    // Disposition block for manual calls
    const dispBlock = document.getElementById('manual-call-disposition-block');
    const dispStatus = document.getElementById('manual-call-disposition-status');
    const btnInterested = document.getElementById('manual-call-btn-interested');
    const btnNotInterested = document.getElementById('manual-call-btn-not-interested');
    if (dispBlock) {
        const currentDisp = (payload.disposition || '').trim();
        if (currentDisp === 'Interested' || currentDisp === 'Not Interested') {
            dispBlock.style.display = 'block';
            if (dispStatus) {
                dispStatus.style.display = 'block';
                dispStatus.textContent = `Outcome: ${currentDisp}`;
                dispStatus.style.color = currentDisp === 'Interested' ? '#16a34a' : '#64748b';
            }
            if (btnInterested) {
                btnInterested.style.display = currentDisp === 'Interested' ? 'none' : 'inline-flex';
                btnInterested.textContent = 'Interested — Send WhatsApp';
            }
            if (btnNotInterested) {
                btnNotInterested.style.display = currentDisp === 'Not Interested' ? 'none' : 'inline-flex';
            }
        } else {
            dispBlock.style.display = 'block';
            if (dispStatus) dispStatus.style.display = 'none';
            if (btnInterested) {
                btnInterested.style.display = 'inline-flex';
                btnInterested.textContent = 'Interested — Send WhatsApp';
            }
            if (btnNotInterested) btnNotInterested.style.display = 'inline-flex';
        }
    }

    // Emotion block
    const emoBlock = document.getElementById('manual-call-modal-emotion-block');
    const emoLabel = document.getElementById('manual-call-modal-emotion');
    const emoRat = document.getElementById('manual-call-modal-emotion-rationale');
    const emoConf = document.getElementById('manual-call-modal-emotion-confidence');
    const emotionLabel = (payload.emotion_label || '').trim();
    const emotionRat = (payload.emotion_rationale || '').trim();
    if (emoBlock && emotionLabel) {
        emoBlock.style.display = 'block';
        if (emoLabel) emoLabel.textContent = emotionLabel;
        if (emoRat) emoRat.textContent = emotionRat;
        if (emoConf) {
            const c = Number(payload.emotion_confidence || 0);
            emoConf.textContent = c > 0 ? `${Math.round(c * 100)}% confidence` : '';
        }
    } else if (emoBlock) {
        emoBlock.style.display = 'none';
    }

    // Rating block
    const ratingBlock = document.getElementById('manual-call-modal-rating-block');
    const ratingEl = document.getElementById('manual-call-modal-rating');
    const rating = payload.rating;
    if (ratingBlock && rating != null && rating !== '') {
        ratingBlock.style.display = 'block';
        if (ratingEl) ratingEl.textContent = String(rating);
    } else if (ratingBlock) {
        ratingBlock.style.display = 'none';
    }

    // Recommended actions list
    const actionsBlock = document.getElementById('manual-call-modal-actions-block');
    const actionsEl = document.getElementById('manual-call-modal-actions');
    const actions = Array.isArray(payload.recommended_actions) ? payload.recommended_actions : [];
    if (actionsBlock && actionsEl && actions.length) {
        actionsEl.innerHTML = actions.map(a => `<li>${escapeHtml(String(a))}</li>`).join('');
        actionsBlock.style.display = 'block';
    } else if (actionsBlock) {
        actionsBlock.style.display = 'none';
    }

    void prepManualCallModalRecording(payload.id, !!payload.recording_available);
    m.style.display = 'flex';
    m.setAttribute('aria-hidden', 'false');
}

async function fetchManualCallDetail(id) {
    const res = await fetch(apiUrl(`/api/manual/calls/${id}?role=${apiRoleQ()}`), {
        headers: authHeaders(),
        credentials: 'same-origin',
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    return res.json();
}

async function pollManualCallComplete(manualCallId, opts) {
    const maxMs = (opts && opts.maxMs) || 120000;
    const intervalMs = (opts && opts.intervalMs) || 2000;
    const t0 = Date.now();
    while (Date.now() - t0 < maxMs) {
        try {
            const row = await fetchManualCallDetail(manualCallId);
            if (row.status === 'completed' || row.status === 'failed') {
                return row;
            }
        } catch (_) { /* network blip */ }
        await new Promise(r => setTimeout(r, intervalMs));
    }
    return null;
}

async function loadRecentManualCalls() {
    const listEl = document.getElementById('manual-recent-list');
    const emptyEl = document.getElementById('manual-recent-empty');
    if (!listEl) return;
    try {
        const res = await fetch(apiUrl(`/api/manual/calls/recent?role=${apiRoleQ()}&limit=12`), {
            headers: authHeaders(),
            credentials: 'same-origin',
        });
        const data = await res.json();
        const items = data.items || [];
        if (emptyEl) {
            emptyEl.style.display = items.length ? 'none' : 'block';
        }
        listEl.innerHTML = items.map(r => {
            const st = escapeHtml(r.status || '');
            const sum = escapeHtml((r.summary || '').slice(0, 80) + ((r.summary || '').length > 80 ? '…' : ''));
            const btn = (r.status === 'completed' || r.status === 'failed')
                ? `<button type="button" class="btn btn-ghost btn-sm" onclick="viewManualCallOutcome(${escapeHtml(r.id)})">View result</button>`
                : `<span style="color:var(--text-secondary);font-size:11px;">${st}</span>`;
            return `<div class="manual-recent-row">
                <div><span style="font-weight:600;">${escapeHtml(r.callee_name || '—')}</span>
                <span style="color:var(--text-secondary);margin-left:8px;">${escapeHtml(r.to_phone || '')}</span>
                <div style="color:var(--text-secondary);margin-top:2px;">${sum || st}</div></div>
                <div>${btn}</div>
            </div>`;
        }).join('');
    } catch (e) {
        listEl.innerHTML = '<div style="font-size:12px;color:var(--text-secondary);">Could not load recent calls.</div>';
    }
}

async function viewManualCallOutcome(id) {
    try {
        const row = await fetchManualCallDetail(id);
        openManualCallModal(row);
    } catch (e) {
        showToast((e && e.message) ? e.message : 'Failed to load call', 'error');
    }
}

/** Value sent to `/api/manual/call`: digits-only for India-first server norm, or full `+country…`. */
function composeManualDialPayload(raw) {
    const v = String(raw || '').trim();
    if (!v) return '';
    const tight = v.replace(/[^\d+]/g, '');
    if (!tight) return '';
    if (tight.startsWith('+')) return tight;
    return tight.replace(/\+/g, '');
}

/** Human-readable label for the result card (best-effort; server is canonical). */
function manualDialPreviewLabel(payloadTo) {
    if (!payloadTo) return '';
    if (String(payloadTo).startsWith('+')) return String(payloadTo);
    const d = String(payloadTo).replace(/\D/g, '');
    if (!d) return '';
    return '+91 ' + d;
}

function refreshManualPhoneCcBadge() {
    const label = document.getElementById('manual-phone-cc');
    const inp = document.getElementById('manual-phone-local');
    if (!label || !inp) return;
    const intl = inp.value.trimStart().startsWith('+');
    label.style.opacity = intl ? '0.35' : '1';
    label.textContent = '+91';
}

function manualCallRoleQ() {
    if (typeof tuningRoleForApi === 'function') {
        return encodeURIComponent(tuningRoleForApi());
    }
    return typeof apiRoleQ === 'function' ? apiRoleQ() : encodeURIComponent('buyers');
}

async function triggerManualTest() {
    const name = document.getElementById('manual-name').value.trim();
    const localEl = document.getElementById('manual-phone-local');
    const rawLocal = localEl ? localEl.value.trim() : '';
    const phone = composeManualDialPayload(rawLocal);
    if (!phone) { alert('Enter a phone number.'); return; }
    const btn = document.querySelector('#page-manual button.btn-primary');
    btn.disabled = true; btn.textContent = 'Calling...';
    try {
        const res = await fetch(apiUrl(`/api/manual/call?role=${manualCallRoleQ()}`), {
            method: 'POST',
            headers: authHeaders(),
            credentials: 'same-origin',
            body: JSON.stringify({ to: phone, callee_name: name }),
        });
        let data = {};
        try { data = await res.json(); } catch (_) {}
        if (!res.ok) {
            if (res.status === 401 && typeof logout === 'function') {
                alert('Your session expired or the server was restarted. Signing you in again…');
                logout();
                return;
            }
            throw new Error(
                typeof data.detail === 'string'
                    ? data.detail
                    : (Array.isArray(data.detail)
                        ? (data.detail[0]?.msg || res.statusText)
                        : (data.detail || data.message || res.statusText))
            );
        }
        const card = document.getElementById('call-result-card');
        card.style.borderStyle = 'solid';
        const mid = data.manual_call_id;
        card.innerHTML = `<div style="text-align:left;padding:16px;width:100%;">
            <p style="font-size:13px;font-weight:700;color:var(--success);margin:0 0 8px;">Call initiated</p>
            <p style="font-size:12px;color:var(--text-secondary);margin:0 0 12px;">Dialing ${escapeHtml(manualDialPreviewLabel(phone))}… Outcome appears when the call ends (typically 30–90s after hangup).</p>
            <p id="manual-poll-status" style="font-size:12px;color:var(--text-secondary);margin:0;">Analyzing transcript…</p>
            <button type="button" class="btn btn-ghost btn-sm" style="margin-top:12px;" onclick="loadRecentManualCalls()">Refresh recent list</button>
        </div>`;
        loadRecentManualCalls();
        if (mid) {
            pollManualCallComplete(mid).then(row => {
                const st = document.getElementById('manual-poll-status');
                if (!row) {
                    if (st) st.textContent = 'Still processing — open Recent manual calls → View result when ready.';
                    return;
                }
                if (st) st.textContent = row.status === 'failed' ? 'Ended with errors — see summary.' : 'Ready — opening summary…';
                openManualCallModal(row);
                if (row.status === 'failed') {
                    showToast((row.error || 'Manual call failed') + '', 'error');
                }
                loadRecentManualCalls();
            });
        }
    } catch (e) {
        alert('Error: ' + (e && e.message ? e.message : e));
    } finally {
        btn.disabled = false; btn.textContent = 'Initiate Call';
        syncState();
    }
}

// ─── Tuning (Configuration) ───
// Critical invariant: the textareas must reflect ``currentRole`` at all times.
// If we ever leave stale content from another role visible, the user could click
// Save and POST it under the wrong role — corrupting that role's prompt file.
// So we (1) blank the fields first, (2) lock Save during the fetch, and
// (3) always assign the API response (even if empty).
async function loadTuning() {
    const promptEl   = document.getElementById('tuning-prompt');
    const ragEl      = document.getElementById('tuning-rag');
    const greetingEl = document.getElementById('tuning-greeting');
    const saveBtn    = document.querySelector('#page-tuning button.btn-primary');

    // 1) Blank everything so a stale (cross-role) value can never be re-saved.
    promptEl.value = '';
    ragEl.value = '';
    greetingEl.value = '';

    // 2) Lock Save while the role's real content is in-flight.
    let originalLabel = '';
    if (saveBtn) {
        originalLabel = saveBtn.textContent;
        saveBtn.disabled = true;
        saveBtn.textContent = 'Loading…';
    }

    try {
        // Same-role SSR seed (only relevant for sellers — `/console`).
        const seedEl = document.getElementById('__vernika_tuning_seed');
        let seedData = {};
        if (seedEl) { try { seedData = JSON.parse(seedEl.textContent); } catch {} }
        if (currentRole === 'sellers' && seedData.sellers) {
            promptEl.value   = seedData.sellers.prompt   || '';
            ragEl.value      = seedData.sellers.rag      || '';
            greetingEl.value = seedData.sellers.greeting_text != null
                ? String(seedData.sellers.greeting_text) : '';
        }

        const roleQ =
            typeof tuningRoleForApi === 'function'
                ? encodeURIComponent(tuningRoleForApi())
                : apiRoleQ();
        const res = await fetch(apiUrl(`/api/tuning?role=${roleQ}`), {
            headers: { 'Authorization': `Bearer ${token()}` },
            credentials: 'same-origin',
        });
        if (!res.ok) return;
        const d = await res.json();
        // Always assign — even if empty — so the UI is the source of truth
        // for ``currentRole`` and Save can never mix roles.
        promptEl.value   = d.prompt != null ? String(d.prompt) : '';
        ragEl.value      = d.rag    != null ? String(d.rag)    : '';
        greetingEl.value = d.greeting_text != null ? String(d.greeting_text) : '';
    } catch {}
    finally {
        if (saveBtn) {
            saveBtn.disabled = false;
            saveBtn.textContent = originalLabel || 'Save Changes';
        }
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

async function addSchedule() {
    const btn = document.getElementById('btn-schedule');
    const whenEl = document.getElementById('schedule-when');
    const stopEl = document.getElementById('schedule-stop');
    const nameEl = document.getElementById('schedule-name');
    const rawStart = (whenEl && whenEl.value) ? whenEl.value.trim() : '';
    const rawStop  = (stopEl && stopEl.value) ? stopEl.value.trim() : '';
    if (!rawStart) {
        showToast('Pick a start date and time first.', 'error');
        return;
    }
    // ``datetime-local`` returns ``YYYY-MM-DDTHH:MM`` without timezone, which
    // ``new Date(...)`` parses as **local time** — exactly what the operator
    // sees in the picker. We then convert to ISO with offset so the backend
    // gets an unambiguous instant.
    const startLocal = new Date(rawStart);
    if (isNaN(startLocal.getTime())) {
        showToast('That doesn\'t look like a valid start date / time.', 'error');
        return;
    }
    if (startLocal.getTime() < Date.now() - 15000) {
        showToast('Pick a future start time — that moment has already passed.', 'error');
        return;
    }

    let stopLocal = null;
    if (rawStop) {
        stopLocal = new Date(rawStop);
        if (isNaN(stopLocal.getTime())) {
            showToast('That doesn\'t look like a valid stop date / time.', 'error');
            return;
        }
        if (stopLocal.getTime() <= startLocal.getTime()) {
            showToast('Stop time must be after the start time.', 'error');
            return;
        }
    }

    if (btn) { btn.disabled = true; btn.textContent = 'Scheduling…'; }
    try {
        const body = {
            run_at_iso: startLocal.toISOString(),
            name: nameEl ? nameEl.value.trim() : '',
        };
        if (stopLocal) body.stop_at_iso = stopLocal.toISOString();

        const res = await fetch(apiUrl(`/api/schedules?role=${apiRoleQ()}`), {
            method: 'POST',
            headers: authHeaders(),
            credentials: 'same-origin',
            body: JSON.stringify(body),
        });
        if (!res.ok) {
            showToast(await parseApiErrorMessage(res), 'error');
            return;
        }
        const startTxt = _formatScheduleWhen(startLocal.getTime() / 1000);
        const msg = stopLocal
            ? `Scheduled ${startTxt} → ${_formatScheduleWhen(stopLocal.getTime() / 1000)}.`
            : `Scheduled for ${startTxt}.`;
        showToast(msg, 'success');
        if (nameEl) nameEl.value = '';
        if (stopEl) stopEl.value = '';
        await loadSchedules();
    } catch (e) {
        showToast((e && e.message) ? e.message : 'Network error', 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Schedule'; }
    }
}

function clearScheduleStop() {
    const el = document.getElementById('schedule-stop');
    if (el) el.value = '';
}

async function saveTuning() {
    if (_saveTuningSubmitting) return;
    _saveTuningSubmitting = true;
    const btn = document.querySelector('#page-tuning button.btn-primary');
    const targetRole = currentRole;
    const prompt = document.getElementById('tuning-prompt').value;
    const rag = document.getElementById('tuning-rag').value;
    const greeting_text = document.getElementById('tuning-greeting').value;

    btn.disabled = true; btn.textContent = 'Saving...';
    try {
        const res = await fetch(apiUrl(`/api/tuning?role=${encodeURIComponent(targetRole)}`), {
            method: 'POST',
            headers: authHeaders(),
            credentials: 'same-origin',
            body: JSON.stringify({ prompt, rag, greeting_text }),
        });
        if (res.ok) {
            btn.textContent = '✓ Saved';
            showToast('Configuration saved.', 'success');
            if (currentRole === targetRole) await loadTuning();
            setTimeout(() => { btn.textContent = 'Save Changes'; btn.disabled = false; }, 2000);
        } else {
            const err = await parseApiErrorMessage(res);
            showToast(err, 'error');
            btn.disabled = false; btn.textContent = 'Save Changes';
        }
    } catch (e) {
        showToast(e.message || 'Save failed', 'error');
        btn.disabled = false; btn.textContent = 'Save Changes';
    } finally {
        _saveTuningSubmitting = false;
    }
}

// ─── Campaign Schedules UI Integration ───
function _initScheduleDefaults() {
    const tzEl = document.getElementById('schedule-tz-pill');
    if (tzEl) tzEl.textContent = 'IST';
    const whenEl = document.getElementById('schedule-when');
    if (whenEl && !whenEl.value) {
        const now = new Date();
        const istOffset = 5.5 * 60 * 60 * 1000;
        const future = new Date(Date.now() + istOffset + 10 * 60 * 1000);
        future.setSeconds(0);
        future.setMilliseconds(0);
        const pad = (n) => String(n).padStart(2, '0');
        const formatted = `${future.getUTCFullYear()}-${pad(future.getUTCMonth() + 1)}-${pad(future.getUTCDate())}T${pad(future.getUTCHours())}:${pad(future.getUTCMinutes())}`;
        whenEl.value = formatted;
    }
}

function _formatScheduleWhen(epoch) {
    if (!epoch) return '—';
    try {
        const d = new Date(epoch * 1000);
        return formatTimeIST(d.toISOString());
    } catch (_) {
        return '—';
    }
}

async function loadSchedules() {
    const listEl = document.getElementById('schedules-list');
    if (!listEl) return;
    try {
        const res = await fetch(apiUrl(`/api/schedules?role=${apiRoleQ()}`), {
            headers: authHeaders(),
            credentials: 'same-origin',
        });
        if (res.status === 401 && typeof logout === 'function') { logout(); return; }
        if (!res.ok) {
            listEl.innerHTML = `<div style="text-align:center;padding:18px;color:var(--text-secondary);">Could not load schedules.</div>`;
            return;
        }
        const data = await res.json().catch(() => ({}));
        const list = Array.isArray(data.schedules) ? data.schedules : [];
        if (!list.length) {
            listEl.innerHTML = `<div style="text-align:center;padding:18px;color:var(--text-secondary);">No schedules yet.</div>`;
            return;
        }
        
        listEl.innerHTML = list.map(s => {
            const id = s.id;
            const status = s.status || 'scheduled';
            const name = escapeHtml(s.name || 'Unnamed Schedule');
            
            let whenText = s.run_at ? formatTimeIST(new Date(s.run_at * 1000).toISOString()) : '—';
            if (s.stop_at) {
                const stopText = formatTimeIST(new Date(s.stop_at * 1000).toISOString());
                whenText += ` → ${stopText}`;
            }
            
            let badgeClass = 'badge-tag-neutral';
            if (status === 'scheduled') badgeClass = 'badge-tag-warning';
            else if (status === 'running') badgeClass = 'badge-tag-success';
            else if (status === 'completed') badgeClass = 'badge-tag-info';
            else if (status === 'cancelled') badgeClass = 'badge-tag-neutral';
            else if (status === 'failed') badgeClass = 'badge-tag-danger';
            
            const cancelBtn = (status === 'scheduled') 
                ? `<button type="button" class="btn btn-ghost btn-sm" style="color:var(--danger);font-size:11px;padding:2px 8px;border:1px solid var(--danger);" onclick="cancelSchedule(${escapeHtml(id)})">Cancel</button>`
                : '';
                
            return `
                <div style="display:flex;justify-content:space-between;align-items:center;padding:10px 14px;border-bottom:1px solid var(--border);gap:12px;">
                    <div style="flex:1;">
                        <div style="font-weight:600;display:flex;align-items:center;gap:8px;">
                            <span>${name}</span>
                            <span class="badge-tag ${badgeClass}" style="font-size:10px;padding:2px 6px;text-transform:uppercase;">${escapeHtml(status)}</span>
                        </div>
                        <div style="font-size:11px;color:var(--text-secondary);margin-top:4px;">${escapeHtml(whenText)}</div>
                    </div>
                    <div>${cancelBtn}</div>
                </div>
            `;
        }).join('');
    } catch (e) {
        listEl.innerHTML = `<div style="text-align:center;padding:18px;color:var(--text-secondary);">Could not load schedules.</div>`;
    }
}

async function cancelSchedule(id) {
    if (!confirm('Are you sure you want to cancel this scheduled campaign?')) return;
    try {
        const res = await fetch(apiUrl(`/api/schedules/${id}`), {
            method: 'DELETE',
            headers: authHeaders(),
            credentials: 'same-origin',
        });
        if (res.status === 401 && typeof logout === 'function') logout();
        if (!res.ok) {
            showToast(await parseApiErrorMessage(res), 'error');
            return;
        }
        showToast('Schedule cancelled successfully.', 'success');
        await loadSchedules();
    } catch (e) {
        showToast((e && e.message) ? e.message : 'Failed to cancel schedule', 'error');
    }
}

// ─── Campaign Start / Schedule Modal ───
function showCampaignStartModal() {
    var now = new Date();
    var pad = function (n) { return String(n).padStart(2, '0'); };
    var localStr = now.getFullYear() + '-' + pad(now.getMonth() + 1) + '-' + pad(now.getDate()) + 'T' + pad(now.getHours()) + ':' + pad(now.getMinutes());
    document.getElementById('campaign-start-datetime').value = localStr;
    document.getElementById('campaign-stop-datetime').value = '';
    document.getElementById('campaign-start-label').value = '';
    openModal('modal-campaign-start');
}

function startCampaignFromModal() {
    var label = document.getElementById('campaign-start-label').value.trim();
    closeModal('modal-campaign-start');
    if (label) {
        var nameEl = document.getElementById('schedule-name');
        if (nameEl) nameEl.value = label;
    }
    if (typeof startCampaign === 'function') startCampaign();
}

function scheduleCampaignFromModal() {
    var label = document.getElementById('campaign-start-label').value.trim();
    var startVal = document.getElementById('campaign-start-datetime').value;
    var stopVal = document.getElementById('campaign-stop-datetime').value;
    if (!startVal) { showToast('Please select a start time.', 'error'); return; }
    closeModal('modal-campaign-start');
    var nameEl = document.getElementById('schedule-name');
    var whenEl = document.getElementById('schedule-when');
    var stopEl = document.getElementById('schedule-stop');
    if (nameEl && label) nameEl.value = label;
    if (whenEl) whenEl.value = startVal;
    if (stopEl) stopEl.value = stopVal;
    if (typeof addSchedule === 'function') addSchedule();
    if (typeof showPageNav === 'function') showPageNav('campaigns', document.getElementById('nav-campaigns'));
}

function buildClientOpeningLine(lead) {
    const defaultGreetings = {
        "data_edge": "Hi, this is Priya from Data Edge. I'm a career counselor — got a quick minute?",
        "sellers": "Hi, this is Devika from Procucev, Bangalore. Got a quick minute?",
        "buyers": "Hi, this is Adithi from Procucev Enterprise Solutions, Bangalore. I'm a procurement specialist — do you have a quick minute?",
        "rfqs": "Hi, this is from Procucev. I'm calling about an RFQ opportunity — got a quick minute?"
    };

    let text = document.getElementById('tuning-greeting')?.value || '';
    if (!text.trim()) {
        text = defaultGreetings[currentRole] || defaultGreetings["data_edge"];
    }

    const rawNm = (lead.name || '').trim();
    const rawCo = (lead.company || '').trim();

    const cities = ["jamnagar", "bhavnagar", "rajkot", "vadodara", "surat", "ahmedabad", "gandhinagar", "morbi", "vapi", "valsad", "anand", "nadiad", "mehsana", "bhuj", "porbandar", "junagadh", "bharuch", "navsari", "mumbai", "delhi", "bangalore", "pune", "hyderabad", "chennai", "kolkata", "jaipur", "lucknow"];
    const isCity = cities.includes(rawNm.toLowerCase());
    
    let firstName = "";
    if (rawNm && rawNm.toLowerCase() !== 'unknown' && !isCity) {
        firstName = rawNm.split(' ')[0];
    }

    let company = "";
    if (rawCo && rawCo.toLowerCase() !== 'unknown') {
        company = rawCo;
    }

    if (firstName) {
        if (text.includes("{name}")) {
            text = text.replace("{name}", firstName);
        } else {
            const prefixes = ["Hi,", "Hello,", "Hey,"];
            let interpolated = false;
            for (let prefix of prefixes) {
                if (text.startsWith(prefix)) {
                    text = prefix.slice(0, -1) + " " + firstName + "," + text.slice(prefix.length);
                    interpolated = true;
                    break;
                }
            }
            if (!interpolated) {
                text = "Hi " + firstName + ", " + text;
            }
        }
    }

    if (company) {
        if (!text.toLowerCase().includes(company.toLowerCase())) {
            const insertPhrase = ", calling for " + company;
            const match = text.match(/([.!?])(\s|$)/);
            if (match) {
                const idx = match.index;
                text = text.slice(0, idx) + insertPhrase + text.slice(idx);
            } else {
                text = text.trim() + " " + insertPhrase.replace(/^, /, '') + ".";
            }
        }
    }

    return text;
}
