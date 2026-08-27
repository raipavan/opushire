// ─── API & Core Helpers ───
const token = () => localStorage.getItem('vernika_token') || '';
const authHeaders = () => ({ 'Authorization': `Bearer ${token()}`, 'Content-Type': 'application/json' });

/**
 * Optional URL prefix when the API is mounted under a subpath (reverse proxy).
 * Set from console HTML: <meta name="vernika-api-root" content="/vernika">
 * or before load: window.__VERN_API_ROOT__ = '/vernika';
 */
function apiRoot() {
    if (typeof window !== 'undefined' && window.__VERN_API_ROOT__) {
        return String(window.__VERN_API_ROOT__).replace(/\/$/, '');
    }
    const meta =
        typeof document !== 'undefined' ? document.querySelector('meta[name="vernika-api-root"]') : null;
    if (meta && meta.content && meta.content.trim()) {
        return meta.content.trim().replace(/\/$/, '');
    }
    return '';
}

/** Absolute path for API calls, e.g. apiUrl('/api/tuning?role=data_edge') */
function apiUrl(pathWithQuery) {
    const p = pathWithQuery.startsWith('/') ? pathWithQuery : '/' + pathWithQuery;
    const root = apiRoot();
    return root ? root + p : p;
}

/** Roles tied to the login account — sidebar toggle must not override these. */
const LOCKED_CONSOLE_ROLES = ['data_edge', 'admin'];

function jwtPayload() {
    try {
        const t = token();
        if (!t) return null;
        const parts = t.split('.');
        if (parts.length < 2) return null;
        let b64 = parts[1].replace(/-/g, '+').replace(/_/g, '/');
        const pad = (4 - (b64.length % 4)) % 4;
        if (pad) b64 += '='.repeat(pad);
        return JSON.parse(atob(b64));
    } catch (_) {
        return null;
    }
}

/** Role from JWT (authoritative after login). */
function loginRoleFromToken() {
    const p = jwtPayload();
    if (!p || !p.role) return null;
    return normalizeRole(p.role);
}

function isLockedConsoleLogin() {
    if (window.__VERN_SESSION__ && window.__VERN_SESSION__.locked) return true;
    const r = loginRoleFromToken();
    return !!(r && LOCKED_CONSOLE_ROLES.includes(r));
}

/** Server truth for dashboard role (set during console bootstrap). */
function dashboardRole() {
    if (window.__VERN_SESSION__ && window.__VERN_SESSION__.dashboard_role) {
        return normalizeRole(window.__VERN_SESSION__.dashboard_role);
    }
    const locked = loginRoleFromToken();
    if (locked && LOCKED_CONSOLE_ROLES.includes(locked)) return locked;
    return null;
}

function apiRoleQ() {
    if (window.__VERN_SESSION__ && window.__VERN_SESSION__.can_switch_roles) {
        let role = typeof currentRole !== 'undefined' && currentRole
            ? currentRole
            : localStorage.getItem('vernika_role') || 'data_edge';
        return encodeURIComponent(normalizeRole(role));
    }
    const fromServer = dashboardRole();
    if (fromServer) return encodeURIComponent(fromServer);
    const locked = loginRoleFromToken();
    if (locked && LOCKED_CONSOLE_ROLES.includes(locked)) {
        return encodeURIComponent(locked);
    }
    let role =
        typeof currentRole !== 'undefined' && currentRole
            ? currentRole
            : localStorage.getItem('vernika_role') || 'data_edge';
    return encodeURIComponent(normalizeRole(role));
}

function isDataEdgeCounselorHost() {
    return false;
}

async function bootstrapConsoleSession() {
    const res = await fetch(apiUrl('/api/me'), {
        headers: authHeaders(),
        credentials: 'same-origin',
    });
    if (res.status === 401) {
        if (typeof logout === 'function') logout();
        else window.location.href = '/login';
        return null;
    }
    if (!res.ok) {
        throw new Error('Could not load session (' + res.status + ')');
    }
    const data = await res.json();
    if (isDataEdgeCounselorHost() && data.dashboard_role !== 'data_edge') {
        const who = (data.email || data.dashboard_role || 'another account');
        try {
            alert(
                'This site is for the Data Edge counselor console (Priya).\n\n' +
                    'You are signed in as ' + who + ' (' + (data.dashboard_role || 'data_edge') + ' data).\n\n' +
                    'Please sign out and log in with dataedge@pitchxai.com.'
            );
        } catch (_) {}
        localStorage.removeItem('vernika_token');
        localStorage.removeItem('vernika_role');
        localStorage.removeItem('vernika_email');
        window.location.href = '/login?reason=wrong_account';
        return null;
    }
    window.__VERN_SESSION__ = data;
    const dr = normalizeRole(data.dashboard_role || data.role || 'data_edge');
    if (typeof currentRole !== 'undefined') currentRole = dr;
    localStorage.setItem('vernika_role', dr);
    if (data.email) localStorage.setItem('vernika_email', data.email);
    if (dr === 'data_edge') {
        try {
            sessionStorage.removeItem('vernika_dash_snap_v2_data_edge');
            sessionStorage.removeItem('vernika_leads_snap_v2_data_edge');
        } catch (_) {}
    }
    return data;
}

function normalizeRole(r) {
    const valid = ['data_edge', 'admin', 'factory', 'demo'];
    return valid.includes(r) ? r : 'data_edge';
}

function escapeHtml(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function formatTime(iso) {
    if (!iso) return '—';
    try {
        let s = String(iso);
        const hasTZ = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(s);
        if (!hasTZ && /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(s)) s = s + 'Z';
        const d = new Date(s);
        if (isNaN(d.getTime())) return iso;
        const now = new Date();
        const sameDay = d.toLocaleDateString('en-IN', { timeZone: 'Asia/Kolkata' }) === now.toLocaleDateString('en-IN', { timeZone: 'Asia/Kolkata' });
        if (sameDay) return d.toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit' });
        return d.toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    } catch { return iso; }
}

/** Format instant in Indian Standard Time (for deferred recall badges, etc.). */
function formatTimeIST(iso) {
    if (!iso) return '—';
    try {
        let s = String(iso);
        const hasTZ = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(s);
        if (!hasTZ && /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(s)) s = s + 'Z';
        const d = new Date(s);
        if (isNaN(d.getTime())) return iso;
        return d.toLocaleString('en-IN', {
            timeZone: 'Asia/Kolkata',
            month: 'short',
            day: 'numeric',
            hour: '2-digit',
            minute: '2-digit',
            hour12: true,
        }) + ' IST';
    } catch (_) {
        return iso;
    }
}

function starsHtml(n) {
    n = Math.max(0, Math.min(5, parseInt(n || 0)));
    let html = '';
    for (let i = 1; i <= 5; i++) {
        html += `<span class="star ${i <= n ? 'on' : ''}">★</span>`;
    }
    return html;
}

function dispoTagClass(d) {
    const s = (d || '').toLowerCase();
    if (s.includes('not interested') || s === 'not_interested') return 'tag-noint';
    if (s.includes('interested')) return 'tag-int';
    if (s.includes('call later')) return 'tag-call-later';
    if (s.includes('callback') || s.includes('busy')) return 'tag-cbk';
    if (s.includes('wrong')) return 'tag-fail';
    if (s === 'failed' || s === 'error' || s === 'no answer') return 'tag-fail';
    if (s === 'completed') return 'tag-int';
    return 'tag-cbk';
}

function prettyStatus(s) {
    const x = (s || '').toString().trim();
    if (!x) return '';
    if (x === 'not_interested') return 'Not Interested';
    if (x === 'completed') return 'Completed';
    if (x === 'failed') return 'Failed';
    if (x === 'callback_scheduled') return 'Callback scheduled';
    if (x === 'dialing') return 'Dialing…';
    if (x === 'in_progress') return 'In Progress';
    return x.charAt(0).toUpperCase() + x.slice(1);
}

function _parseAnalysisBlobClient(raw) {
    if (raw != null && typeof raw === 'object' && !Array.isArray(raw)) return raw;
    if (typeof raw === 'string' && raw.trim()) {
        try {
            const o = JSON.parse(raw);
            return o && typeof o === 'object' && !Array.isArray(o) ? o : {};
        } catch (_) {}
    }
    return {};
}

/** Soft interest in summary/next_steps (send email, will check, etc.) — mirrors ``transcript_interest.py``. */
function softInterestInLeadText(lead) {
    const aj = _parseAnalysisBlobClient(lead && lead.analysis);
    if (lead && lead.outcome_from_transcript) return true;
    if (aj.outcome_from_transcript) return true;
    const blob = [
        lead && lead.summary,
        aj.summary,
        lead && lead.next_steps,
        aj.next_steps,
    ].filter(Boolean).join(' ');
    if (!blob || blob.length < 8) return false;
    const neg = /not\s+interested|don'?t\s+call|stop\s+calling|wrong\s+number|take\s+me\s+off/i;
    if (neg.test(blob)) return false;
    const pos = /send\s+(me|us|the|details|information|a\s+note|write.?up)|(?:email|whatsapp).{0,40}(?:send|share|bhej)|(?:send|share).{0,30}(?:email|whatsapp|mail)|whatsapp\s+(?:me\s+)?(?:the\s+)?(?:detail|info|information|brochure|course|pricing|quote)|(?:message|text|ping|contact)\s+(?:me\s+)?(?:on|via|through)\s+whatsapp|(?:send|share).{0,30}(?:through|via)\s+whatsapp|(?:provide|give).{0,20}email|information\s+via\s+(?:email|whatsapp)|will\s+check|i'?ll\s+check|let\s+me\s+check|decide\s+on\s+that|expressed\s+interest|mail\s+kar|bhej\s+dijiye|@[\w.-]+\.(?:com|in)\b/i;
    return pos.test(blob);
}

/** Match ``enrich_lead_for_console`` / ``effective_disposition_console`` (disposition may live in ``analysis`` only). */
function effectiveDispo(lead) {
    const aj = _parseAnalysisBlobClient(lead && lead.analysis);
    if (aj.disposition_overridden) {
        return String(aj.disposition || 'Answered').trim();
    }
    const d = String((lead && lead.disposition) || aj.disposition || '').trim();
    if (d && d !== 'Answered') return d;
    if (softInterestInLeadText(lead)) return 'Interested';
    if (d) return d;
    return prettyStatus((lead && lead.status) || '');
}

/** Resolve a lead row from ``allLeads`` (onclick may pass string ids). */
function findLeadById(leadId) {
    const nid = Number(leadId);
    if (!Number.isFinite(nid)) return null;
    return allLeads.find(function (l) { return Number(l.id) === nid; }) || null;
}

function _normPhoneDigits(phone) {
    return String(phone || '').replace(/\D/g, '').slice(-10);
}

/**
 * Duplicate DB rows (same phone, different ids): one may lack ``_log_id`` while a sibling has
 * transcript + recording. Returns the best row id + log for media APIs.
 */
function resolveLeadMediaContext(lead) {
    if (!lead || lead.id == null) {
        return { leadId: null, logId: '', hasMedia: false };
    }
    let logId = String(lead._log_id || lead.log_id || '').trim();
    let leadId = Number(lead.id);
    const phone = _normPhoneDigits(lead.phone);
    const role = lead.role || (typeof currentRole !== 'undefined' ? currentRole : '');
    if (!logId && phone && Array.isArray(allLeads)) {
        for (let i = 0; i < allLeads.length; i++) {
            const s = allLeads[i];
            if (!s || s.id == null) continue;
            if (role && s.role && s.role !== role) continue;
            if (_normPhoneDigits(s.phone) !== phone) continue;
            const sid = String(s._log_id || s.log_id || '').trim();
            if (sid) {
                logId = sid;
                if (!String(lead._log_id || lead.log_id || '').trim()) {
                    leadId = Number(s.id);
                }
                break;
            }
        }
    }
    return {
        leadId: leadId,
        logId: logId,
        hasMedia: !!logId,
    };
}

/** Stream URL for ``<audio src>`` (cannot send Authorization header on element src). */
function campaignRecordingStreamUrl(leadId) {
    const t = token();
    let url = apiUrl('/api/campaign/lead/' + leadId + '/recording?role=' + apiRoleQ());
    if (t) url += (url.indexOf('?') >= 0 ? '&' : '?') + 'access_token=' + encodeURIComponent(t);
    return url;
}

/** Stream URL for manual call recording (same pattern). */
function manualCallRecordingStreamUrl(callId) {
    const t = token();
    let url = apiUrl('/api/manual/calls/' + callId + '/recording?role=' + apiRoleQ());
    if (t) url += (url.indexOf('?') >= 0 ? '&' : '?') + 'access_token=' + encodeURIComponent(t);
    return url;
}

/** Count QA dispositions from loaded manifest rows (fallback when state JSON is stale). */
function countDispositionFromLeads(leads) {
    const keys = ['Interested', 'Not Interested', 'Call Later', 'Busy', 'Callback', 'Answered', 'Failed'];
    const buckets = {};
    keys.forEach(function (k) { buckets[k] = 0; });
    (Array.isArray(leads) ? leads : []).forEach(function (lead) {
        if (!isCalled(lead)) return;
        const st = String(lead.status || '').toLowerCase();
        // Skip leads still in progress — they are active calls, not final outcomes
        if (st === 'in_progress' || st === 'dialing') return;
        if (st === 'failed' || st === 'error') {
            buckets.Failed += 1;
            return;
        }
        if (st === 'not_interested') {
            buckets['Not Interested'] += 1;
            return;
        }
        const ed = effectiveDispo(lead);
        const el = ed.toLowerCase();
        if (ed === 'Interested' || (el.includes('interested') && !el.includes('not interested'))) {
            buckets.Interested += 1;
        } else if (ed === 'Not Interested' || el.includes('not interested')) {
            buckets['Not Interested'] += 1;
        } else if (ed === 'Call Later' || el.includes('call later')) {
            buckets['Call Later'] += 1;
        } else if (ed === 'Busy' || el.includes('busy')) {
            buckets.Busy += 1;
        } else if (ed === 'Callback' || el.includes('callback')) {
            buckets.Callback += 1;
        } else if (ed === 'Wrong Number' || ed === 'Not Available' || ed === 'Voicemail') {
            buckets.Failed += 1;
        } else if (st === 'completed' || ed === 'Answered') {
            buckets.Answered += 1;
        } else if (isFailed(lead)) {
            buckets.Failed += 1;
        }
    });
    return buckets;
}

/** Prefer server aggregates; fall back to manifest-derived counts. */
function resolveDashboardCounts(data, leads) {
    data = data || {};
    const dc = data.disposition_counts || {};
    const fromApi = Number(dc.Interested);
    const fromApiNi = Number(dc['Not Interested']);
    const hasApi =
        Number.isFinite(fromApi) && fromApi >= 0 &&
        Number.isFinite(fromApiNi) && fromApiNi >= 0 &&
        (fromApi > 0 || fromApiNi > 0 || Number(data.called_count) > 0);
    if (hasApi) {
        return {
            interested: fromApi,
            notInterested: fromApiNi,
            failed: Number(dc.Failed) || 0,
            dispositionCounts: dc,
        };
    }
    const chartTotal = Number(data.chart_interested_total);
    if (Number.isFinite(chartTotal) && chartTotal > 0) {
        return {
            interested: chartTotal,
            notInterested: Number(dc['Not Interested']) || 0,
            failed: Number(dc.Failed) || 0,
            dispositionCounts: dc,
        };
    }
    const computed = countDispositionFromLeads(leads);
    return {
        interested: Number(computed.Interested) || 0,
        notInterested: Number(computed['Not Interested']) || 0,
        failed: Number(computed.Failed) || 0,
        dispositionCounts: computed,
    };
}

function isCalled(lead) { return !!lead.start_time || !!lead._log_id || !!lead.called_at_iso; }

/** Prefer API ``contact_display_*`` when ``name`` was a sheet row counter (``11.0``) wrongly mapped as contact. */
function leadContactPrimary(lead) {
    if (!lead) return '';
    const co = (lead.company != null ? String(lead.company) : '').trim();
    if (co) return co;
    const p = (lead.contact_display_primary != null ? String(lead.contact_display_primary) : '').trim();
    if (p) return p;
    const n = (lead.name != null ? String(lead.name) : '').trim();
    return n || '';
}

function leadContactSecondary(lead) {
    if (!lead) return '';
    const co = (lead.company != null ? String(lead.company) : '').trim();
    if (co) {
        const p = (lead.contact_display_primary != null ? String(lead.contact_display_primary) : '').trim();
        if (p && p !== co) return p;
        const n = (lead.name != null ? String(lead.name) : '').trim();
        if (n && n !== co) return n;
        return '';
    }
    const s = (lead.contact_display_secondary != null ? String(lead.contact_display_secondary) : '').trim();
    if (s) return s;
    return '';
}

function isFailed(lead) {
    const s = (lead.status || '').toLowerCase();
    return (s === 'failed' || s === 'error' || s === 'no answer') && s !== 'in_progress';
}

function failureSeverityClass(sev) {
    const s = (String(sev || '').toLowerCase());
    if (s === 'info') return 'fail-sev-info';
    if (s === 'warning') return 'fail-sev-warning';
    if (s === 'muted') return 'fail-sev-muted';
    return 'fail-sev-error';
}

/** Table / manifest cell: labeled failure from API, or raw failure_reason. */
function formatFailureCell(r) {
    const title = (r.failure_title || '').trim();
    const detail = (r.failure_detail || '').trim();
    const raw = (r.failure_reason || '').trim();
    const status = (r.status || '').toLowerCase();
    const isFailedStatus = status === 'failed' || status === 'error' || status === 'no answer';
    if ((!title && !raw) || !isFailedStatus) {
        return '<span style="color:var(--text-secondary);font-size:12px;">—</span>';
    }
    const label = title || raw;
    const sevCls = title ? failureSeverityClass(r.failure_severity) : 'fail-sev-error';
    const secondaryBits = [];
    if (detail && detail !== label) secondaryBits.push(detail);
    if (raw && raw !== label && raw !== detail) secondaryBits.push(raw);
    const secondary = secondaryBits.join(' · ');
    const secondaryHtml = secondary
        ? `<div style="font-size:11px;color:var(--text-secondary);margin-top:4px;line-height:1.35;max-width:260px;white-space:normal;word-break:break-word;" title="${escapeHtml(secondary)}">${escapeHtml(secondary.length > 90 ? secondary.substring(0, 90) + '…' : secondary)}</div>`
        : '';
    const tip = [label, secondary].filter(Boolean).join(' — ');
    return `<div style="display:flex;flex-direction:column;align-items:flex-start;gap:0;">
        <span class="failure-chip ${sevCls}" title="${escapeHtml(tip)}">${escapeHtml(label)}</span>
        ${secondaryHtml}
    </div>`;
}

/** For failed leads, replace the empty Summary cell with a clear reason line. */
function failureSummaryHtml(r) {
    const title = (r.failure_title || '').trim();
    const raw = (r.failure_reason || '').trim();
    const detail = (r.failure_detail || '').trim();
    const label = title || raw || 'Call did not connect';
    const sev = (r.failure_severity || 'error').toLowerCase();
    const color = sev === 'info' ? '#007AFF'
        : sev === 'warning' ? '#CC7700'
        : sev === 'muted' ? 'var(--text-secondary)'
        : 'var(--danger)';
    const secondary = (detail && detail !== label) ? detail : (raw && raw !== label ? raw : '');
    return `<div style="display:flex;flex-direction:column;gap:4px;">
        <span style="font-size:12px;font-weight:700;color:${color};">Why this failed: ${escapeHtml(label)}</span>
        ${secondary ? `<span style="font-size:11px;color:var(--text-secondary);line-height:1.4;">${escapeHtml(secondary.length > 130 ? secondary.substring(0, 130) + '…' : secondary)}</span>` : ''}
    </div>`;
}

function fillCallFailureModal(lead) {
    const fb = document.getElementById('cd-failure-block');
    const catEl = document.getElementById('cd-failure-category');
    const titleEl = document.getElementById('cd-failure-title-line');
    const detEl = document.getElementById('cd-failure-detail-line');
    if (!fb || !catEl || !titleEl || !detEl) return;

    const title = (lead.failure_title || '').trim();
    const detail = (lead.failure_detail || '').trim();
    const raw = (lead.failure_reason || '').trim();
    const cat = (lead.failure_category || '').trim();

    if (!isFailed(lead) || (!title && !raw)) {
        fb.style.display = 'none';
        fb.className = 'cd-failure-block fail-sev-error';
        catEl.style.display = 'none';
        catEl.textContent = '';
        titleEl.textContent = '';
        detEl.style.display = 'none';
        detEl.textContent = '';
        return;
    }
    const primary = title || raw;
    const sev = title ? failureSeverityClass(lead.failure_severity) : 'fail-sev-error';
    fb.style.display = 'block';
    fb.className = 'cd-failure-block ' + sev;
    if (cat) {
        catEl.style.display = 'block';
        catEl.textContent = cat;
    } else {
        catEl.style.display = 'none';
        catEl.textContent = '';
    }
    titleEl.textContent = primary;
    const secondaryBits = [];
    if (detail && detail !== primary) secondaryBits.push(detail);
    if (raw && raw !== primary && (!detail || raw !== detail)) secondaryBits.push(raw);
    const secondary = secondaryBits.join('\n\n');
    if (secondary) {
        detEl.style.display = 'block';
        detEl.textContent = secondary;
    } else {
        detEl.style.display = 'none';
        detEl.textContent = '';
    }
}

function showToast(msg, type = 'info', ms = 4000) {
    const host = document.getElementById('toast-host');
    if (!host) return;
    const t = document.createElement('div');
    t.className = `vernika-toast ${type}`;
    t.textContent = msg;
    host.appendChild(t);
    const hideMs = typeof ms === 'number' && ms > 0 ? ms : 4000;
    setTimeout(() => {
        t.style.animation = 'toastIn 0.32s ease reverse forwards';
        setTimeout(() => t.remove(), 400);
    }, hideMs);
}
