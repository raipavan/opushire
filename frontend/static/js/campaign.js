// ─── Campaign State Sync ───
window.__CAMPAIGN_JS_LOADED = true;
let campaignStateFetchWarned = false;
let _campaignSyncGen = 0;
/** Last successful dashboard snapshot for replay when a refresh fails (avoids flashing all zeros). */
let lastCampaignSnapshot = null;
/** Server-side total lead count from /api/campaign/state (authoritative). */
let _serverTotalLeads = 0;

const DEFAULT_MANIFEST_FETCH_LIMIT = 0;

/** Server-side chart aggregates (full outbound cohort, not the chart row sample). */
function buildChartExtrasFromState(data) {
    data = data || {};
    return {
        calledCount: typeof data.called_count === 'number' ? data.called_count : null,
        progressCounts: data.progress_counts || null,
        weekdayCounts: data.weekday_counts || null,
        hourlyCounts: Array.isArray(data.hourly_counts) && data.hourly_counts.length === 24 ? data.hourly_counts : null,
        dispositionCounts: data.disposition_counts || {},
    };
}

/** Reflect ``campaign_hours`` from ``/api/campaign/state`` (quiet-hours hard block). */
function applyCampaignHoursUI(hours) {
    const banner = document.getElementById('campaign-quiet-hours-banner');
    const btnStart = document.getElementById('btn-start');
    if (!hours || !hours.enabled) {
        if (banner) banner.style.display = 'none';
        if (btnStart) {
            btnStart.disabled = false;
            btnStart.removeAttribute('title');
        }
        return;
    }
    const blocked = !!hours.in_quiet_hours;
    if (banner) {
        if (blocked) {
            const msg = hours.block_message
                || ('Calling allowed ' + (hours.allowed_start || '') + '–' + (hours.allowed_end || '') + ' ' + (hours.tz || ''));
            banner.textContent = msg + (hours.local_time ? ' (now ' + hours.local_time + ').' : '.');
            banner.style.display = 'block';
        } else {
            banner.style.display = 'none';
        }
    }
    if (btnStart) {
        btnStart.disabled = blocked;
        if (blocked) {
            btnStart.setAttribute('title', hours.block_message || 'Outside calling hours');
        } else {
            btnStart.removeAttribute('title');
        }
    }
}

function applyCampaignPausedUI(paused) {
    const banner = document.getElementById('campaign-quiet-hours-banner');
    if (paused) {
        if (banner) {
            banner.style.display = 'block';
            banner.textContent =
                'Campaign is paused — outbound dialing is off. Re-analyze and the lead list still work; no new calls will be placed until you Start during calling hours (9:30 AM – 8:30 PM IST).';
        }
    }
}

    /* When !paused, do nothing — applyCampaignHoursUI re-manages the banner/button on the next poll. */

const LEAD_SESSION_KEY_PREFIX = 'vernika_leads_snap_v2_';
const DASH_SNAP_KEY_PREFIX = 'vernika_dash_snap_v2_';
const LEAD_SESSION_MAX_ROWS = 3500;
/** Stay under typical ~5 MB ``sessionStorage`` limits. */
const LEAD_SESSION_MAX_CHARS = 4_200_000;

function slimLeadForSession(l) {
    if (!l || l.id == null) return null;
    return {
        id: l.id,
        role: l.role,
        name: l.name,
        phone: l.phone,
        company: l.company,
        email: l.email,
        status: l.status,
        disposition: l.disposition,
        summary: l.summary,
        rating: l.rating,
        start_time: l.start_time,
        called_at_iso: l.called_at_iso,
        _log_id: l._log_id,
        log_id: l.log_id,
        recording_available: l.recording_available,
        recording_url: l.recording_url,
        transcript_url: l.transcript_url,
        outcome_from_transcript: l.outcome_from_transcript,
        contact_display_primary: l.contact_display_primary,
        contact_display_secondary: l.contact_display_secondary,
        failure_title: l.failure_title,
        failure_detail: l.failure_detail,
        failure_reason: l.failure_reason,
        failure_severity: l.failure_severity,
        next_steps: l.next_steps,
        emotion_label: l.emotion_label,
        emotion_rationale: l.emotion_rationale,
        emotion_confidence: l.emotion_confidence,
        callback_reminder_at_iso: l.callback_reminder_at_iso,
    };
}

function persistLeadTablesToSession() {
    try {
        const role = typeof currentRole !== 'undefined' ? currentRole : 'data_edge';
        if (!Array.isArray(allLeads) || !allLeads.length) {
            sessionStorage.removeItem(LEAD_SESSION_KEY_PREFIX + role);
            return;
        }
        let slice = allLeads.map(slimLeadForSession).filter(Boolean).slice(0, LEAD_SESSION_MAX_ROWS);
        let json = null;
        while (slice.length > 0) {
            json = JSON.stringify({ v: 2, ts: Date.now(), leads: slice });
            if (json.length <= LEAD_SESSION_MAX_CHARS || slice.length <= 1) break;
            slice = slice.slice(0, Math.max(1, Math.floor(slice.length * 0.7)));
        }
        if (!json || json.length > LEAD_SESSION_MAX_CHARS) return;
        sessionStorage.setItem(LEAD_SESSION_KEY_PREFIX + role, json);
    } catch (_) {}
}

/** Hydrate Lead Manifest + Recent Calls from cache so a hard refresh does not wipe tables. */
function restoreLeadTablesFromSession() {
    try {
        const role = typeof currentRole !== 'undefined' ? currentRole : 'data_edge';
        const raw = sessionStorage.getItem(LEAD_SESSION_KEY_PREFIX + role);
        if (!raw) return false;
        const o = JSON.parse(raw);
        if (!o || !Array.isArray(o.leads) || !o.leads.length) return false;
        allLeads = o.leads;
        if (typeof renderManifest === 'function') renderManifest();
        if (typeof renderCalls === 'function') renderCalls();
        return true;
    } catch (_) {
        return false;
    }
}

function setCampaignTotalsIndeterminate(busy) {
    const dash = '\u2013'; // en dash — reads as "waiting", not zero
    if (busy) {
        updateStat('stat-total', dash);
        updateStat('stat-called', dash);
        updateStat('stat-interested-count', dash);
        updateStat('stat-not-interested', dash);
        updateStat('stat-inbound-callbacks', dash);
        updateStat('stat-conversion-rate', dash);
        updateStat('stat-attempts', dash);
        updateStat('stat-failed', dash);
        updateStat('perf-avg-rating', dash);
        updateStat('perf-total-called', dash);
        updateStat('perf-callback-rate', dash);
        updateStat('perf-fail-rate', dash);
        updateStat('camp-total', 'Loading…');
        const bar = document.getElementById('progress-bar');
        if (bar) {
            bar.classList.add('vern-progress-indeterminate');
            bar.parentElement?.classList.add('vern-loading-pulse');
        }
        return;
    }
    const bar = document.getElementById('progress-bar');
    if (bar) {
        bar.classList.remove('vern-progress-indeterminate');
        bar.parentElement?.classList.remove('vern-loading-pulse');
    }
}

/** Lead Manifest skeleton (spinner row). Caller must eventually call ``renderManifest`` or overwrite tbody. */
function showLeadManifestSkeleton(message, opts) {
    const tb = document.getElementById('manifest-tbody');
    if (!tb) return;
    const spinner = !(opts && opts.spinner === false);
    const m = escapeHtml(message || 'Loading leads…');
    const spinHtml = spinner
        ? '<span class="vern-campaign-spinner"></span>'
        : '';
    tb.innerHTML = '<tr><td colspan="6" style="padding:42px;">'
        + '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;color:var(--text-secondary);">'
        + spinHtml
        + '<span style="font-size:13px;font-weight:600;text-align:center;max-width:320px;line-height:1.4;">' + m + '</span>'
        + '</div></td></tr>';
}

function stashCampaignSnapshot(patch) {
    lastCampaignSnapshot = Object.assign({}, lastCampaignSnapshot || {}, patch);
    try {
        const role = typeof currentRole !== 'undefined' ? currentRole : 'data_edge';
        const s = lastCampaignSnapshot;
        if (!s || !s.texts) return;
        const slim = {
            texts: s.texts,
            progressPct: s.progressPct,
            disposition_counts: s.disposition_counts || {},
            callback_dates: s.callback_dates || {},
            timeline_week_labels: s.timeline_week_labels,
            timeline_total_calls: s.timeline_total_calls,
            timeline_interested: s.timeline_interested,
            timeline_dates_iso: s.timeline_dates_iso,
            timeline_inbound_per_day: s.timeline_inbound_per_day,
            workerActive: s.workerActive,
            activeCalls: s.activeCalls,
        };
        sessionStorage.setItem(DASH_SNAP_KEY_PREFIX + role, JSON.stringify(slim));
    } catch (_) {}
}

/** After hard refresh, paint last known stats/charts from sessionStorage until /state returns. */
function restoreDashboardSnapshotFromSession() {
    try {
        const role = typeof currentRole !== 'undefined' ? currentRole : 'data_edge';
        const raw = sessionStorage.getItem(DASH_SNAP_KEY_PREFIX + role);
        if (!raw) return;
        const slim = JSON.parse(raw);
        if (!slim || !slim.texts) return;
        lastCampaignSnapshot = Object.assign({}, slim, { chartSample: [] });
        replayLastCampaignSnapshot();
    } catch (_) {}
}

function replayLastCampaignSnapshot() {
    if (!lastCampaignSnapshot || !lastCampaignSnapshot.texts) return false;
    const s = lastCampaignSnapshot;
    campaignWorkerActive = !!s.workerActive;
    Object.keys(s.texts).forEach(function (id) {
        updateStat(id, s.texts[id]);
    });
    const bar = document.getElementById('progress-bar');
    if (bar && typeof s.progressPct === 'number') {
        bar.style.width = s.progressPct + '%';
    }
    const dot = document.getElementById('active-dot');
    const sample = Array.isArray(s.chartSample) ? s.chartSample : [];
    updateCharts(sample, s.disposition_counts || {}, s.callback_dates || {}, {
        timeline_week_labels: s.timeline_week_labels,
        timeline_total_calls: s.timeline_total_calls,
        timeline_interested: s.timeline_interested,
        timeline_dates_iso: s.timeline_dates_iso,
        timeline_inbound_per_day: s.timeline_inbound_per_day,
    }, {
        calledCount: s.texts && s.texts['stat-called'] ? parseInt(String(s.texts['stat-called']).replace(/,/g, ''), 10) : null,
        progressCounts: s.progress_counts,
        weekdayCounts: s.weekday_counts,
        dispositionCounts: s.disposition_counts || {},
    });
    updateCampaignRunnerChrome();
    return true;
}

/** Full rows for Lead Manifest + call list live here (small JSON). ``/state`` only carries counts + chart sample. */
async function refreshCampaignManifest(opts) {
    opts = opts || {};
    const staleVisibleKeep = !!(opts.keepStaleVisible && Array.isArray(allLeads) && allLeads.length > 0);
    try {
        if (!staleVisibleKeep) {
            showLeadManifestSkeleton('Loading lead preview…');
        }

        const raw = typeof window.__VERN_MANIFEST_FETCH_LIMIT !== 'undefined' && window.__VERN_MANIFEST_FETCH_LIMIT != null
            ? Number(window.__VERN_MANIFEST_FETCH_LIMIT)
            : DEFAULT_MANIFEST_FETCH_LIMIT;
        const ml = Number.isFinite(raw) ? (raw <= 0 ? 0 : Math.min(20000, Math.max(50, Math.floor(raw)))) : DEFAULT_MANIFEST_FETCH_LIMIT;

        const res = await fetch(apiUrl(`/api/campaign/manifest?role=${apiRoleQ()}&limit=${ml}`), {
            headers: { 'Authorization': `Bearer ${token()}` },
            credentials: 'same-origin',
        });
        if (res.status === 401) {
            logout();
            return;
        }
        if (!res.ok) {
            console.warn('Campaign manifest fetch failed', res.status);
            if (typeof showToast === 'function' && !campaignStateFetchWarned) {
                campaignStateFetchWarned = true;
                showToast(
                    'Could not load lead preview (HTTP ' + res.status + '). Stats may load; try reload.',
                    'error',
                    7000,
                );
            }
            if (!allLeads.length) {
                showLeadManifestSkeleton('Preview could not be loaded.', { spinner: false });
            } else {
                renderManifest();
                renderCalls();
            }
            return;
        }
        const m = await res.json().catch(() => ({}));
        allLeads = Array.isArray(m.leads) ? m.leads : [];
        campaignStateFetchWarned = false;
        renderManifest();
        renderCalls();
        persistLeadTablesToSession();
        if (typeof applyManifestDispositionStats === 'function') {
            applyManifestDispositionStats();
        }
    } catch (e) {
        console.error('Campaign manifest failed', e);
        if (!allLeads.length) {
            showLeadManifestSkeleton('Something went wrong while loading leads.', { spinner: false });
        } else {
            renderManifest();
            renderCalls();
        }
    }
}

async function syncState() {
    _campaignSyncGen += 1;
    const myGen = _campaignSyncGen;

    // Only show the skeleton / dash placeholders on the very FIRST load
    // (no prior snapshot). On repeat 4-second polls, keep showing stale
    // values so the UI never blinks to "—".
    const isFirstLoad = !lastCampaignSnapshot && !(Array.isArray(allLeads) && allLeads.length > 0);
    if (isFirstLoad) {
        setCampaignTotalsIndeterminate(true);
        showLeadManifestSkeleton('Fetching campaign summary…');
    }

    try {
        const res = await fetch(apiUrl(`/api/campaign/state?role=${apiRoleQ()}`), {
            headers: { 'Authorization': `Bearer ${token()}` },
            credentials: 'same-origin',
        });
        if (res.status === 401) {
            setCampaignTotalsIndeterminate(false);
            logout();
            return;
        }
        if (!res.ok) {
            console.warn('Campaign state fetch failed', res.status);
            if (typeof showToast === 'function' && !campaignStateFetchWarned) {
                campaignStateFetchWarned = true;
                showToast(
                    'Campaign data could not load (HTTP ' + res.status +
                    '). The lead list stays empty until this succeeds - check login or network.',
                    'error',
                    8000,
                );
            }
            setCampaignTotalsIndeterminate(false);
            if (!replayLastCampaignSnapshot()) {
                updateStat('stat-total', '0');
                updateStat('stat-called', '0');
                updateStat('stat-interested-count', '0');
                updateStat('stat-not-interested', '0');
                updateStat('stat-failed', '0');
                updateStat('stat-conversion-rate', '0%');
                updateStat('stat-inbound-callbacks', '0');
                updateStat('stat-attempts', '0');
                updateStat('perf-avg-rating', '—');
                updateStat('perf-total-called', '0');
                updateStat('perf-callback-rate', '0%');
                updateStat('perf-fail-rate', '0%');
                updateStat('camp-total', '0 leads');
            }
            renderManifest();
            if (typeof renderCalls === 'function') renderCalls();
            return;
        }
        let data;
        try {
            data = await res.json();
        } catch (je) {
            console.error('Campaign state JSON parse failed', je);
            if (typeof showToast === 'function') {
                showToast('Campaign response was truncated or invalid JSON. Trying lead preview separately…', 'error', 8000);
            }
            setCampaignTotalsIndeterminate(false);
            if (!replayLastCampaignSnapshot()) {
                updateStat('camp-total', '—');
            }
            await refreshCampaignManifest({ keepStaleVisible: !!allLeads.length });
            return;
        }

        campaignWorkerActive = !!data.active;
        applyCampaignHoursUI(data.campaign_hours);
        applyCampaignPausedUI(data.campaign_paused);
        if (data.campaign_paused) {
            campaignWorkerActive = false;
        }
        let chartSample = Array.isArray(data.chart_sample) ? data.chart_sample : [];
        if (!chartSample.length && Array.isArray(data.leads) && data.leads.length) {
            chartSample = data.leads.slice(0, 900);
        }
        if (!allLeads || !allLeads.length) {
            allLeads = chartSample;
        }

        const totalInDb = Number(data.total);
        const nTotal = Number.isFinite(totalInDb) && totalInDb >= 0
            ? Math.floor(totalInDb)
            : chartSample.length;
        _serverTotalLeads = nTotal;
        const pendingN = Number.isFinite(Number(data.pending)) ? Math.floor(Number(data.pending)) : null;

        const called = chartSample.filter(isCalled);
        const resolved = typeof resolveDashboardCounts === 'function'
            ? resolveDashboardCounts(data, allLeads.length ? allLeads : chartSample)
            : null;
        const dc = (resolved && resolved.dispositionCounts) || data.disposition_counts || {};
        const interested = resolved ? resolved.interested : Number(dc['Interested'] ?? data.chart_interested_total ?? 0);
        const notInterested = resolved ? resolved.notInterested : Number(dc['Not Interested'] ?? 0);
        const failed = resolved ? resolved.failed : Number(dc['Failed'] ?? 0);
        const chartExtras = buildChartExtrasFromState(data);
        const active = data.active_calls ?? 0;
        const callbacks = data.inbound_callbacks ?? 0;
        const calledCount = typeof data.called_count === 'number' ? data.called_count : called.length;
        const conversionRate = calledCount > 0 ? Math.round((interested / calledCount) * 100) : 0;

        setCampaignTotalsIndeterminate(false);

        updateStat('stat-total', nTotal.toLocaleString());
        updateStat('stat-called', Number.isFinite(calledCount) ? calledCount.toLocaleString() : String(calledCount));
        updateStat('stat-interested-count', interested);
        updateStat('stat-not-interested', notInterested);
        updateStat('stat-failed', failed);
        updateStat('stat-conversion-rate', conversionRate + '%');
        updateStat('stat-inbound-callbacks', callbacks);
        updateStat('stat-attempts', Number.isFinite(calledCount) ? calledCount.toLocaleString() : String(calledCount));

        const pctCalled = nTotal > 0 ? Math.round((calledCount / nTotal) * 100) : 0;
        updatePct('pct-called', pctCalled);
        const pctInterested = calledCount > 0 ? Math.round((interested / calledCount) * 100) : 0;
        updatePct('pct-interested', pctInterested);
        const pctNotInterested = calledCount > 0 ? Math.round((notInterested / calledCount) * 100) : 0;
        updatePct('pct-not-interested', pctNotInterested);
        const pctInbound = nTotal > 0 ? Math.round((callbacks / nTotal) * 100) : 0;
        updatePct('pct-inbound', pctInbound);
        const attempts = Number.isFinite(calledCount) ? calledCount : 0;
        const pctAttempts = nTotal > 0 ? Math.round((attempts / nTotal) * 100) : 0;
        updatePct('pct-attempts', pctAttempts);
        const pctFailed = calledCount > 0 ? Math.round((failed / calledCount) * 100) : 0;
        updatePct('pct-failed', pctFailed);

        // Performance metrics for chart cards
        const perfAvgEl = document.getElementById('perf-avg-rating');
        const perfCalledEl = document.getElementById('perf-total-called');
        const perfCallbackEl = document.getElementById('perf-callback-rate');
        const perfFailEl = document.getElementById('perf-fail-rate');
        if (perfCalledEl) perfCalledEl.textContent = calledCount;
        const callbackLeads = called.filter(l => {
            const d = effectiveDispo(l);
            return d === 'Callback' || d === 'Busy';
        }).length;
        if (perfCallbackEl) {
            const cbRate = calledCount > 0 ? Math.round((callbackLeads / calledCount) * 100) : 0;
            perfCallbackEl.textContent = cbRate + '%';
        }
        if (perfFailEl) {
            const fRate = calledCount > 0 ? Math.round((failed / calledCount) * 100) : 0;
            perfFailEl.textContent = fRate + '%';
        }
        if (perfAvgEl) {
            let sum = 0, count = 0;
            called.forEach(function (l) {
                const r = l && l.analysis ? parseInt(l.analysis.rating, 10) : null;
                if (Number.isFinite(r) && r >= 1 && r <= 5) { sum += r; count++; }
            });
            perfAvgEl.textContent = count > 0 ? (sum / count).toFixed(1) : '—';
        }

        const activeDot = document.getElementById('active-dot');

        let campLabel = nTotal.toLocaleString() + ' leads';
        if (data.lead_list_truncated && typeof data.leads_returned === 'number') {
            campLabel += ' (' + String(data.chart_sample?.length ?? data.leads_returned) + '-row chart sample)';
        }
        updateStat('camp-total', campLabel);

        const progressBar = document.getElementById('progress-bar');
        let pct = 0;
        if (progressBar) {
            if (nTotal > 0 && pendingN != null && pendingN >= 0) {
                const touched = Math.min(nTotal, Math.max(0, nTotal - pendingN));
                pct = Math.min(100, (touched / nTotal) * 100);
            } else if (chartSample.length > 0) {
                const progressCalled = chartSample.filter(function (l) { return l.status && l.status !== 'pending'; }).length;
                pct = (progressCalled / chartSample.length) * 100;
            }
            progressBar.style.width = pct + '%';
        }

        if (typeof renderCalls === 'function') {
            renderCalls();
        } else {
            updateCharts(chartSample, dc, data.callback_counts_by_date || {}, {
                timeline_week_labels: data.timeline_week_labels,
                timeline_total_calls: data.timeline_total_calls,
                timeline_interested: data.timeline_interested,
                timeline_dates_iso: data.timeline_dates_iso,
                timeline_inbound_per_day: data.timeline_inbound_per_day,
            }, chartExtras);
        }

        stashCampaignSnapshot({
            texts: {
                'stat-total': nTotal.toLocaleString(),
                'stat-called': Number.isFinite(calledCount) ? calledCount.toLocaleString() : String(calledCount),
                'stat-interested-count': String(interested),
                'stat-not-interested': String(notInterested),
                'stat-failed': String(failed),
                'stat-conversion-rate': conversionRate + '%',
                'stat-inbound-callbacks': String(callbacks),
                'stat-attempts': String(Number.isFinite(calledCount) ? calledCount : called.length),
                'camp-total': campLabel,
            },
            progressPct: pct,
            chartSample: chartSample,
            disposition_counts: data.disposition_counts || {},
            callback_dates: data.callback_counts_by_date || {},
            timeline_week_labels: data.timeline_week_labels,
            timeline_total_calls: data.timeline_total_calls,
            timeline_interested: data.timeline_interested,
            timeline_dates_iso: data.timeline_dates_iso,
            timeline_inbound_per_day: data.timeline_inbound_per_day,
            progress_counts: data.progress_counts,
            weekday_counts: data.weekday_counts,
            hourly_counts: data.hourly_counts,
            workerActive: campaignWorkerActive,
            activeCalls: active,
        });

        campaignStateFetchWarned = false;

        await refreshCampaignManifest({ keepStaleVisible: !!(Array.isArray(allLeads) && allLeads.length > 0) });

        loadInboundCallbacks();

        const gapEl = document.getElementById('campaign-inter-call-gap');
        if (gapEl && data.inter_call_gap_sec != null && document.activeElement !== gapEl) {
            const n = Math.round(Number(data.inter_call_gap_sec));
            gapEl.value = Number.isFinite(n) ? String(n) : '5';
        }
        updateInterCallGapControls(data);
    } catch (e) {
        console.error('Sync failed', e);
        if (typeof showToast === 'function') {
            showToast((e && e.message) ? e.message : 'Sync failed', 'error', 7000);
        }
        setCampaignTotalsIndeterminate(false);
        if (!replayLastCampaignSnapshot()) {
            updateStat('camp-total', '—');
        }
        await refreshCampaignManifest({ keepStaleVisible: !!(Array.isArray(allLeads) && allLeads.length > 0) });
    } finally {
        if (myGen === _campaignSyncGen) {
            setCampaignTotalsIndeterminate(false);
        }
        updateCampaignRunnerChrome();
    }
}

/** Refresh Interested / Not Interested KPIs from loaded manifest when state aggregates were missing. */
function applyManifestDispositionStats() {
    if (!Array.isArray(allLeads) || !allLeads.length || typeof countDispositionFromLeads !== 'function') {
        return;
    }
    const computed = countDispositionFromLeads(allLeads);
    const interested = Number(computed.Interested) || 0;
    const notInterested = Number(computed['Not Interested']) || 0;
    const curI = parseInt(String(document.getElementById('stat-interested-count')?.textContent || '0').replace(/,/g, ''), 10);
    const curNi = parseInt(String(document.getElementById('stat-not-interested')?.textContent || '0').replace(/,/g, ''), 10);
    const shouldRefreshI = interested > 0 && (!Number.isFinite(curI) || curI === 0);
    const shouldRefreshNi = notInterested > 0 && (!Number.isFinite(curNi) || curNi === 0);
    if (shouldRefreshI) updateStat('stat-interested-count', interested);
    if (shouldRefreshNi) updateStat('stat-not-interested', notInterested);
    const calledEl = document.getElementById('stat-called');
    const calledN = parseInt(String(calledEl?.textContent || '0').replace(/,/g, ''), 10);
    if (Number.isFinite(calledN) && calledN > 0) {
        if (shouldRefreshI) updatePct('pct-interested', Math.round((interested / calledN) * 100));
        if (shouldRefreshNi) updatePct('pct-not-interested', Math.round((notInterested / calledN) * 100));
    }
    if ((shouldRefreshI || shouldRefreshNi) && typeof updateCharts === 'function') {
        const chartSample = Array.isArray(allLeads) ? allLeads.filter(isCalled).slice(0, 900) : [];
        if (chartSample.length) {
            updateCharts(chartSample, computed, {}, {});
        }
    }
}

function updateStat(id, val) {
    const el = document.getElementById(id);
    if (!el) return;
    const s = String(val);
    if (el.textContent !== s) el.textContent = s;
}

function updatePct(id, val) {
    const el = document.getElementById(id);
    if (!el) return;
    const s = (val === null || val === undefined || val === '') ? '' : val + '%';
    if (el.textContent !== s) el.textContent = s;
}

/** Campaign Control chrome: status pill + Start button (``/api/campaign/state.active``). */
function updateCampaignRunnerChrome() {
    const pill = document.getElementById('campaign-status-pill');
    const startBtn = document.getElementById('btn-start');
    const stopBtn = document.getElementById('btn-stop');
    const running = typeof campaignWorkerActive !== 'undefined' && !!campaignWorkerActive;

    if (pill) {
        pill.textContent = running
            ? 'Outbound: RUNNING (auto-resumes after deploy/restart while pending leads remain)'
            : 'Outbound: idle — click Start to dial pending leads';
        pill.className = running ? 'badge-tag tag-int' : 'badge-tag tag-cbk';
        pill.style.fontSize = '11px';
        pill.style.fontWeight = '700';
    }
    if (startBtn) {
        startBtn.disabled = !!running;
        startBtn.textContent = running ? 'Running…' : 'Start Campaign';
    }
    if (stopBtn) {
        stopBtn.disabled = false;
    }
}

// ─── Table Rendering ───
function renderCalls() {
    const tbody = document.getElementById('calls-tbody');
    if (!tbody) return;

    const search = (document.getElementById('search-input')?.value || '').toLowerCase().trim();
    let rows = allLeads.filter(isCalled);

    // Apply Filters
    if (currentFilter !== 'all') {
        if (currentFilter === 'failed') {
            rows = rows.filter(isFailed);
        } else if (currentFilter === 'star4') {
            rows = rows.filter(l => (l.rating || 0) >= 4);
        } else if (currentFilter === 'duration30') {
            rows = rows.filter(l => (l.duration_sec || 0) > 30);
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
    
    const fromDate = getFilterDate('filter-date-from', true);
    const toDate = getFilterDate('filter-date-to', false);
    if (fromDate || toDate) {
        rows = rows.filter(function (l) {
            if (!l.start_time) return false;
            const t = l.start_time * 1000;
            if (fromDate && t < fromDate.getTime()) return false;
            if (toDate && t > toDate.getTime()) return false;
            return true;
        });
    }

    if (search) {
        rows = rows.filter(function (l) {
            var p = typeof leadContactPrimary === 'function' ? leadContactPrimary(l) : (l.name || '');
            var s2 = typeof leadContactSecondary === 'function' ? leadContactSecondary(l) : (l.company || '');
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
        tbody.innerHTML = `<tr><td colspan="9" style="text-align:center;padding:40px;color:var(--text-secondary);">No matching calls</td></tr>`;
        return;
    }

    const newHtml = rows.map(r => renderCallRow(r)).join('');
    const sig = rows.map(r => r.id + ':' + r.status + ':' + effectiveDispo(r) + ':' + (r.summary || '')).join('|') + '|' + rows.length;
    if (tbody.dataset.sig !== sig) {
        const temp = document.createElement('tbody');
        temp.innerHTML = newHtml;
        if (tbody.children.length === temp.children.length) {
            const oldChildren = Array.from(tbody.children);
            const newChildren = Array.from(temp.children);
            for (let i = 0; i < oldChildren.length; i++) {
                if (oldChildren[i].outerHTML !== newChildren[i].outerHTML) {
                    oldChildren[i].outerHTML = newChildren[i].outerHTML;
                }
            }
        } else {
            tbody.innerHTML = newHtml;
        }
        tbody.dataset.sig = sig;
    }

    if (typeof syncDashboardMetricsAndCharts === 'function') {
        syncDashboardMetricsAndCharts(rows, fromDate, toDate, search);
    }
}

function renderCallRow(r) {
    const dispo = effectiveDispo(r) || '—';
    const tagClass = dispoTagClass(dispo);
    const summaryHtml = isFailed(r) ? failureSummaryHtml(r) : escapeHtml(r.summary || 'No summary yet');
const mayRec = r.recording_available || r._log_id || r.log_id;
const recHtml = mayRec
    ? `<span style="font-size:11px;color:var(--accent);cursor:pointer;" onclick="event.stopPropagation();openCallDetail(${r.id})">Listen</span>`
    : '<span style="font-size:11px;color:var(--text-secondary);">—</span>';
    
    const pname = escapeHtml(typeof leadContactPrimary === 'function' ? leadContactPrimary(r) : (r.name || '—'));
    const ps2 = escapeHtml(typeof leadContactSecondary === 'function' ? leadContactSecondary(r) : (r.company || ''));
    
    const segment = r.segment || 'rfq';
    const segmentBadge = (apiRoleQ() === 'rfqs') 
        ? (segment === 'seller' 
            ? `<span class="badge-tag" style="background:rgba(239, 68, 68, 0.1);color:rgb(239, 68, 68);border:1px solid rgba(239, 68, 68, 0.2);margin-left:6px;font-size:10px;padding:2px 6px;">Seller</span>` 
            : `<span class="badge-tag" style="background:rgba(59, 130, 246, 0.1);color:rgb(59, 130, 246);border:1px solid rgba(59, 130, 246, 0.2);margin-left:6px;font-size:10px;padding:2px 6px;">RFQ</span>`)
        : '';
    
    const dateHtml = r.start_time 
        ? `<div style="font-size:11px;color:var(--text-secondary);font-weight:500;">${new Date(r.start_time * 1000).toLocaleString(undefined, {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'})}</div>`
        : '<span style="font-size:11px;color:var(--text-secondary);">—</span>';

    return `<tr class="clickable-row" onclick="openCallDetail(${r.id})">
        <td style="padding-left:20px;font-weight:600;">${pname}${segmentBadge}<div style="font-size:11px;color:var(--text-secondary);font-weight:400;">${ps2}</div></td>
        <td style="font-family:var(--font-mono);font-size:12px;">${escapeHtml(r.phone || '—')}</td>
        <td>${dateHtml}</td>
        <td style="font-size:12px;max-width:320px;">${summaryHtml}</td>
        <td>${r.rating ? starsHtml(r.rating) : '—'}</td>
        <td><span class="badge-tag ${tagClass}">${escapeHtml(dispo)}</span></td>
        <td>${formatFailureCell(r)}</td>
        <td>${recHtml}</td>
        <td style="text-align:right;padding-right:20px;"><button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();openCallDetail(${r.id})">View</button></td>
    </tr>`;
}

function getFilterDate(id, startOfDay) {
    const val = document.getElementById(id)?.value;
    return val ? new Date(val + (startOfDay ? 'T00:00:00' : 'T23:59:59')) : null;
}

/** Recent Calls disposition tabs — must stay in sync with ``onclick`` in ``console.html``. */
function setFilter(f, btn) {
    currentFilter = f;
    document.querySelectorAll('.btn-filter').forEach(function (b) {
        b.classList.remove('active');
    });
    if (btn) btn.classList.add('active');
    renderCalls();
}

function clearDateFilters() {
    const fromEl = document.getElementById('filter-date-from');
    const toEl = document.getElementById('filter-date-to');
    if (fromEl) fromEl.value = '';
    if (toEl) toEl.value = '';
    renderCalls();
}

// ─── Callbacks ───
async function loadInboundCallbacks() {
    const tbody = document.getElementById('inbound-callbacks-tbody');
    const tbodyDash = document.getElementById('inbound-callbacks-tbody-dash');
    if (!tbody && !tbodyDash) return;
    try {
        const res = await fetch(apiUrl(`/api/campaign/inbound-callbacks?role=${apiRoleQ()}`), { 
            headers: { 'Authorization': `Bearer ${token()}` }, 
            credentials: 'same-origin' 
        });
        if (res.status === 401 && typeof logout === 'function') logout();
        if (!res.ok) return;
        const data = await res.json();
        const items = data.items || [];
        
        const rowsHtml = items.map(row => {
            const matchedName = escapeHtml(row.matched_name || '—');
            const matchedCompany = row.matched_company ? ` <span style="font-size:11px;color:var(--text-secondary);">(${escapeHtml(row.matched_company)})</span>` : '';
            return `
                <tr>
                    <td><span style="font-weight:600;">${escapeHtml(row.from_phone)}</span></td>
                    <td>${escapeHtml(row.to_phone || '—')}</td>
                    <td>${matchedName}${matchedCompany}</td>
                    <td><span class="badge-tag ${row.campaign_active ? 'badge-tag-success' : 'badge-tag-neutral'}" style="font-size:10px;">${row.campaign_active ? 'Active' : 'No'}</span></td>
                    <td style="text-align:right;"><button class="btn btn-ghost btn-sm" style="color:var(--text-secondary);font-size:11px;" onclick="dismissInboundCallback(${row.id})">Dismiss</button></td>
                </tr>
            `;
        }).join('');
        
        const sig = items.map(r => r.id + ':' + r.campaign_active).join('|') + '|' + items.length;
        if (tbody) {
            if (tbody.dataset.sig !== sig) {
                const temp = document.createElement('tbody');
                temp.innerHTML = rowsHtml;
                if (tbody.children.length === temp.children.length) {
                    const oldC = Array.from(tbody.children);
                    const newC = Array.from(temp.children);
                    for (let i = 0; i < oldC.length; i++) {
                        if (oldC[i].outerHTML !== newC[i].outerHTML) {
                            oldC[i].outerHTML = newC[i].outerHTML;
                        }
                    }
                } else {
                    tbody.innerHTML = rowsHtml;
                }
                tbody.dataset.sig = sig;
            }
            const emptyEl = document.getElementById('inbound-callbacks-empty');
            if (emptyEl) emptyEl.style.display = items.length ? 'none' : 'block';
        }
        if (tbodyDash) {
            if (tbodyDash.dataset.sig !== sig) {
                const temp = document.createElement('tbody');
                temp.innerHTML = rowsHtml;
                if (tbodyDash.children.length === temp.children.length) {
                    const oldC = Array.from(tbodyDash.children);
                    const newC = Array.from(temp.children);
                    for (let i = 0; i < oldC.length; i++) {
                        if (oldC[i].outerHTML !== newC[i].outerHTML) {
                            oldC[i].outerHTML = newC[i].outerHTML;
                        }
                    }
                } else {
                    tbodyDash.innerHTML = rowsHtml;
                }
                tbodyDash.dataset.sig = sig;
            }
            const emptyElDash = document.getElementById('inbound-callbacks-empty-dash');
            if (emptyElDash) emptyElDash.style.display = items.length ? 'none' : 'block';
        }
    } catch (e) {
        console.error('Failed to load inbound callbacks', e);
    }
}

async function dismissInboundCallback(id) {
    await fetch(apiUrl(`/api/campaign/inbound-callbacks/${id}/dismiss?role=${apiRoleQ()}`), { method: 'POST', headers: { 'Authorization': `Bearer ${token()}` }, credentials: 'same-origin' });
    loadInboundCallbacks();
}

const STRICT_GAP_CORE_ROLES = new Set([]);

function updateInterCallGapControls(state) {
    const gapEl = document.getElementById('campaign-inter-call-gap');
    const saveBtn = document.getElementById('campaign-inter-call-gap-save');
    const noteEl = document.getElementById('campaign-inter-call-gap-note');
    if (!gapEl) return;

    const role = typeof apiRoleQ === 'function' ? apiRoleQ() : (typeof currentRole !== 'undefined' ? currentRole : 'data_edge');
    const strict = !!(state && state.inter_call_gap_strict) || STRICT_GAP_CORE_ROLES.has(role);
    const sec = state && state.inter_call_gap_sec != null
        ? Math.round(Number(state.inter_call_gap_sec))
        : (strict ? 150 : 5);

    gapEl.value = Number.isFinite(sec) ? String(sec) : (strict ? '150' : '5');
    gapEl.readOnly = strict;
    gapEl.disabled = strict;
    gapEl.style.opacity = strict ? '0.65' : '1';
    if (saveBtn) saveBtn.disabled = strict;

    if (noteEl) {
        if (strict) {
            const lo = (state && state.inter_call_gap_min_sec) || 135;
            const hi = (state && state.inter_call_gap_max_sec) || 165;
            noteEl.innerHTML =
                'Wait time after each outbound call before dialing the next lead for <strong>this role</strong>. '
                + `<strong>Sellers, Buyers, RFQs, and Dariaan</strong> use a fixed <strong>${sec}s</strong> carrier-safety pause `
                + `(${lo}–${hi}s band; not configurable).`;
        } else {
            noteEl.innerHTML =
                'Wait time after each outbound call before dialing the next lead for <strong>this role</strong>. '
                + 'Enter 0 for no gap, or up to 1200 seconds, then save.';
        }
    }
}

async function saveInterCallGap() {
    const gapEl = document.getElementById('campaign-inter-call-gap');
    if (!gapEl || typeof apiRoleQ !== 'function' || typeof token !== 'function') return;
    const role = apiRoleQ();
    if (STRICT_GAP_CORE_ROLES.has(role)) {
        showToast('Sellers, Buyers, RFQs, and Dariaan use a fixed 150s pause and cannot be changed.', 'error');
        return;
    }
    const raw = Number(gapEl.value);
    if (!Number.isFinite(raw) || raw < 0 || raw > 1200) {
        showToast('Enter a pause between 0 and 1200 seconds.', 'error');
        return;
    }
    try {
        const res = await fetch(apiUrl(`/api/campaign/inter-call-gap?role=${apiRoleQ()}`), {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token()}`, 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({ seconds: raw }),
        });
        const data = await res.json().catch(() => ({}));
        if (res.status === 401 && typeof logout === 'function') logout();
        if (!res.ok) {
            const detail = typeof data.detail === 'string' ? data.detail : (Array.isArray(data.detail) && data.detail[0]?.msg) || res.statusText;
            throw new Error(detail || 'Save failed');
        }
        const sec = data.inter_call_gap_sec != null ? data.inter_call_gap_sec : raw;
        showToast(`Pause between calls: ${sec}s for this role`, 'success');
        await syncState();
    } catch (e) {
        showToast((e && e.message) ? e.message : 'Could not save pause', 'error');
    }
}

// ─── Re-analyze All ───
let _reanalyzePollTimer = null;

function showReanalyzeModal() {
    const m = document.getElementById('modal-reanalyze-all');
    if (m) {
        m.classList.add('modal-open');
        // Reset UI
        document.getElementById('reanalyze-progress-bar').style.width = '0%';
        document.getElementById('reanalyze-progress-text').textContent = '0 / 0';
        document.getElementById('reanalyze-status').textContent = 'Starting...';
        document.getElementById('reanalyze-current').textContent = '\u00a0';
        const errEl = document.getElementById('reanalyze-errors');
        errEl.style.display = 'none';
        errEl.innerHTML = '';
        document.getElementById('btn-reanalyze-cancel').style.display = 'inline-flex';
        document.getElementById('btn-reanalyze-close').style.display = 'none';
    }
}

function startReanalyzeAll() {
    const btn = document.getElementById('btn-reanalyze-all');
    if (btn) btn.disabled = true;
    showReanalyzeModal();
    fetch(apiUrl('/api/campaign/reanalyze-all'), {
        method: 'POST',
        headers: { 'Authorization': `Bearer ${token()}`, 'Content-Type': 'application/json' },
        credentials: 'same-origin',
    }).then(r => r.json()).then(data => {
        if (data.total) {
            document.getElementById('reanalyze-progress-text').textContent = `0 / ${data.total}`;
            document.getElementById('reanalyze-status').textContent = `Processing ${data.total} leads...`;
            _pollReanalyzeProgress();
        } else {
            document.getElementById('reanalyze-status').textContent = 'No eligible leads found.';
            _finishReanalyzeAll();
        }
    }).catch(err => {
        try {
            const resp = err.response;
            if (resp && resp.status === 409) {
                document.getElementById('reanalyze-status').textContent = 'Already running.';
            } else {
                document.getElementById('reanalyze-status').textContent = 'Error: ' + (err.message || 'unknown');
            }
        } catch (_) {
            document.getElementById('reanalyze-status').textContent = 'Error starting re-analysis.';
        }
        _finishReanalyzeAll();
    });
}

function _pollReanalyzeProgress() {
    if (_reanalyzePollTimer) clearTimeout(_reanalyzePollTimer);
    fetch(apiUrl('/api/campaign/reanalyze-all/progress'), {
        headers: { 'Authorization': `Bearer ${token()}` },
        credentials: 'same-origin',
    }).then(r => r.json()).then(state => {
        const total = state.total || 0;
        const done = state.completed || 0;
        const pct = total > 0 ? Math.round((done / total) * 100) : 0;
        document.getElementById('reanalyze-progress-bar').style.width = pct + '%';
        document.getElementById('reanalyze-progress-text').textContent = `${done} / ${total}`;
        document.getElementById('reanalyze-current').textContent = state.current || '\u00a0';
        if (state.errors && state.errors.length) {
            const errEl = document.getElementById('reanalyze-errors');
            errEl.style.display = 'block';
            errEl.innerHTML = state.errors.map(e => '<div>' + escapeHtml(e) + '</div>').join('');
        }
        if (state.running) {
            _reanalyzePollTimer = setTimeout(_pollReanalyzeProgress, 2000);
        } else {
            document.getElementById('reanalyze-status').textContent = 'Done! Refreshing...';
            _finishReanalyzeAll();
        }
    }).catch(() => {
        _reanalyzePollTimer = setTimeout(_pollReanalyzeProgress, 3000);
    });
}

function _finishReanalyzeAll() {
    if (_reanalyzePollTimer) { clearTimeout(_reanalyzePollTimer); _reanalyzePollTimer = null; }
    const btn = document.getElementById('btn-reanalyze-all');
    if (btn) btn.disabled = false;
    document.getElementById('btn-reanalyze-cancel').style.display = 'none';
    document.getElementById('btn-reanalyze-close').style.display = 'inline-flex';
    // refresh state
    syncState();
}

function cancelReanalyzeAll() {
    if (_reanalyzePollTimer) { clearTimeout(_reanalyzePollTimer); _reanalyzePollTimer = null; }
    fetch(apiUrl('/api/campaign/reanalyze-all/cancel'), {
        method: 'POST',
        headers: { 'Authorization': `Bearer ${token()}`, 'Content-Type': 'application/json' },
        credentials: 'same-origin',
    }).catch(() => {});
    const btn = document.getElementById('btn-reanalyze-all');
    if (btn) btn.disabled = false;
    document.getElementById('reanalyze-status').textContent = 'Cancelled.';
    document.getElementById('btn-reanalyze-cancel').style.display = 'none';
    document.getElementById('btn-reanalyze-close').style.display = 'inline-flex';
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
}

// ─── Manual Call ───
// triggerManualTest, modal, and recent list live in restored.js (loaded after this file).

function calculateTimelineData(filteredLeads, fromDate, toDate) {
    let start, end;
    if (fromDate && toDate) {
        start = new Date(fromDate);
        end = new Date(toDate);
    } else {
        if (filteredLeads.length > 0) {
            let minMs = Infinity, maxMs = -Infinity;
            filteredLeads.forEach(l => {
                const ms = leadTimelineMs(l);
                if (!isNaN(ms)) {
                    if (ms < minMs) minMs = ms;
                    if (ms > maxMs) maxMs = ms;
                }
            });
            if (minMs !== Infinity && maxMs !== -Infinity) {
                start = new Date(minMs);
                end = new Date(maxMs);
            }
        }
        if (!start || !end) {
            end = new Date();
            start = new Date();
            start.setDate(end.getDate() - 6);
        }
    }

    const dates = [];
    let curr = new Date(start.getFullYear(), start.getMonth(), start.getDate());
    const limitDate = new Date(curr.getTime() + 30 * 24 * 60 * 60 * 1000); // Max 30 days
    const finalEnd = end.getTime() < limitDate.getTime() ? end : limitDate;
    
    while (curr <= finalEnd) {
        dates.push(curr.toISOString().slice(0, 10));
        curr.setDate(curr.getDate() + 1);
    }

    const totalCalls = Array(dates.length).fill(0);
    const interested = Array(dates.length).fill(0);
    const inbound = Array(dates.length).fill(0);

    const dateIdx = {};
    dates.forEach((d, i) => {
        dateIdx[d] = i;
    });

    filteredLeads.forEach(l => {
        const ms = leadTimelineMs(l);
        if (isNaN(ms)) return;
        const iso = new Date(ms).toISOString().slice(0, 10);
        const idx = dateIdx[iso];
        if (idx !== undefined) {
            totalCalls[idx]++;
            const d = effectiveDispo(l);
            if (d === 'Interested') {
                interested[idx]++;
            }
            if (d === 'Callback' || d === 'Busy') {
                inbound[idx]++;
            }
        }
    });

    return {
        timeline_dates_iso: dates,
        timeline_total_calls: totalCalls,
        timeline_interested: interested,
        timeline_inbound_per_day: inbound,
        timeline_week_labels: dates.map(d => {
            const dateObj = new Date(d + 'T00:00:00');
            return CHART_WEEKDAY_SHORT[dateObj.getDay()];
        })
    };
}

function syncDashboardMetricsAndCharts(rows, fromDate, toDate, search) {
    let filteredAllLeads = allLeads;
    if (fromDate || toDate) {
        filteredAllLeads = filteredAllLeads.filter(l => {
            if (!l.start_time) return false;
            const t = l.start_time * 1000;
            if (fromDate && t < fromDate.getTime()) return false;
            if (toDate && t > toDate.getTime()) return false;
            return true;
        });
    }
    if (search) {
        filteredAllLeads = filteredAllLeads.filter(l => {
            var p = typeof leadContactPrimary === 'function' ? leadContactPrimary(l) : (l.name || '');
            var s2 = typeof leadContactSecondary === 'function' ? leadContactSecondary(l) : (l.company || '');
            return (l.name || '').toLowerCase().includes(search)
                || (p || '').toLowerCase().includes(search)
                || (s2 || '').toLowerCase().includes(search)
                || (l.phone || '').toLowerCase().includes(search)
                || (l.company || '').toLowerCase().includes(search)
                || (l.summary || '').toLowerCase().includes(search);
        });
    }

    const totalCount = (_serverTotalLeads > 0) ? _serverTotalLeads : filteredAllLeads.length;
    const calledCount = rows.length;
    const interestedCount = rows.filter(l => effectiveDispo(l) === 'Interested').length;
    const notInterestedCount = rows.filter(l => effectiveDispo(l) === 'Not Interested').length;
    const failedCount = rows.filter(isFailed).length;
    const callLaterCount = rows.filter(l => effectiveDispo(l) === 'Call Later').length;
    const callbacksCount = rows.filter(l => {
        const d = effectiveDispo(l);
        return d === 'Callback' || d === 'Busy';
    }).length;
    const conversionRate = calledCount > 0 ? Math.round((interestedCount / calledCount) * 100) : 0;
    const attemptsCount = calledCount;

    const pctCalled = totalCount > 0 ? Math.round((calledCount / totalCount) * 100) : 0;
    const pctInterested = calledCount > 0 ? Math.round((interestedCount / calledCount) * 100) : 0;
    const pctNotInterested = calledCount > 0 ? Math.round((notInterestedCount / calledCount) * 100) : 0;
    const pctInbound = totalCount > 0 ? Math.round((callbacksCount / totalCount) * 100) : 0;
    const pctCallLater = totalCount > 0 ? Math.round((callLaterCount / totalCount) * 100) : 0;
    const pctAttempts = totalCount > 0 ? Math.round((attemptsCount / totalCount) * 100) : 0;
    const pctFailed = calledCount > 0 ? Math.round((failedCount / calledCount) * 100) : 0;

    // Handle realestate role dynamic labeling and statistics mapping
    const isRealEstate = typeof currentRole !== 'undefined' && currentRole === 'real_estate';
    
    const lblNotInterested = document.getElementById('lbl-not-interested');
    const lblAttempts = document.getElementById('lbl-attempts');
    if (lblNotInterested) {
        lblNotInterested.textContent = isRealEstate ? 'Site Visit' : 'Not Interested';
    }
    if (lblAttempts) {
        lblAttempts.textContent = isRealEstate ? 'Follow Ups' : 'AI Attempts';
    }

    const filterBtnNotInterested = document.getElementById('filter-btn-NotInterested');
    if (filterBtnNotInterested) {
        const svg = filterBtnNotInterested.querySelector('svg');
        if (svg) {
            filterBtnNotInterested.innerHTML = svg.outerHTML + (isRealEstate ? ' Site Visit' : ' Not Interested');
        } else {
            filterBtnNotInterested.textContent = isRealEstate ? 'Site Visit' : 'Not Interested';
        }
    }

    updateStat('stat-total', totalCount.toLocaleString());
    updateStat('stat-called', calledCount.toLocaleString());
    updateStat('stat-interested-count', interestedCount.toLocaleString());
    
    if (isRealEstate) {
        // Site Visit = Interested Count
        updateStat('stat-not-interested', interestedCount.toLocaleString());
        updatePct('pct-not-interested', pctInterested);
        // Follow Ups = Called Count
        updateStat('stat-attempts', calledCount.toLocaleString());
        updatePct('pct-attempts', pctCalled);
    } else {
        updateStat('stat-not-interested', notInterestedCount.toLocaleString());
        updatePct('pct-not-interested', pctNotInterested);
        updateStat('stat-attempts', attemptsCount.toLocaleString());
        updatePct('pct-attempts', pctAttempts);
    }

    updateStat('stat-failed', failedCount.toLocaleString());
    updateStat('stat-conversion-rate', conversionRate + '%');
    updateStat('stat-inbound-callbacks', callbacksCount.toLocaleString());
    updateStat('stat-call-later', callLaterCount.toLocaleString());

    updatePct('pct-called', pctCalled);
    updatePct('pct-interested', pctInterested);
    updatePct('pct-inbound', pctInbound);
    updatePct('pct-call-later', pctCallLater);
    updatePct('pct-failed', pctFailed);

    const perfAvgEl = document.getElementById('perf-avg-rating');
    const perfCalledEl = document.getElementById('perf-total-called');
    const perfCallbackEl = document.getElementById('perf-callback-rate');
    const perfFailEl = document.getElementById('perf-fail-rate');
    if (perfCalledEl) perfCalledEl.textContent = calledCount;
    if (perfCallbackEl) perfCallbackEl.textContent = (calledCount > 0 ? Math.round((callbacksCount / calledCount) * 100) : 0) + '%';
    if (perfFailEl) perfFailEl.textContent = (calledCount > 0 ? Math.round((failedCount / calledCount) * 100) : 0) + '%';
    if (perfAvgEl) {
        let sum = 0, ratingCount = 0;
        rows.forEach(function (l) {
            const r = l.rating != null ? Number(l.rating) : (l.analysis ? parseInt(l.analysis.rating, 10) : null);
            if (Number.isFinite(r) && r >= 1 && r <= 5) { sum += r; ratingCount++; }
        });
        perfAvgEl.textContent = ratingCount > 0 ? (sum / ratingCount).toFixed(1) : '—';
    }

    const dc = {
        'Interested': interestedCount,
        'Not Interested': notInterestedCount,
        'Call Later': rows.filter(l => effectiveDispo(l) === 'Call Later').length,
        'Busy': rows.filter(l => effectiveDispo(l) === 'Busy').length,
        'Callback': rows.filter(l => effectiveDispo(l) === 'Callback').length,
        'Answered': rows.filter(l => effectiveDispo(l) === 'Answered').length,
        'Failed': failedCount
    };

    const callbackCountsByDate = {};
    rows.forEach(l => {
        const d = effectiveDispo(l);
        if (d === 'Callback' || d === 'Busy') {
            const ms = leadTimelineMs(l);
            if (!isNaN(ms)) {
                const iso = new Date(ms).toISOString().slice(0, 10);
                callbackCountsByDate[iso] = (callbackCountsByDate[iso] || 0) + 1;
                const dayName = weekdayShortUtc(ms);
                callbackCountsByDate[dayName] = (callbackCountsByDate[dayName] || 0) + 1;
            }
        }
    });

    const timelineData = calculateTimelineData(rows, fromDate, toDate);

    const chartExtras = {
        calledCount: calledCount,
        progressCounts: {
            connected: rows.filter(l => (l.status || '').toLowerCase() === 'completed').length,
            failed: rows.filter(l => { var s = (l.status || '').toLowerCase(); return s === 'failed' || s === 'error'; }).length,
            no_answer: rows.filter(l => { var s = (l.status || '').toLowerCase(); return s === 'no answer' || s === 'busy'; }).length,
            pending: filteredAllLeads.filter(l => (l.status || '').toLowerCase() === 'pending' || !l.status).length
        },
        weekdayCounts: [0, 0, 0, 0, 0, 0, 0]
    };

    rows.forEach(l => {
        const ms = leadTimelineMs(l);
        if (isNaN(ms)) return;
        const d = new Date(ms).getDay();
        const idx = d === 0 ? 6 : d - 1;
        if (idx >= 0 && idx <= 6) chartExtras.weekdayCounts[idx]++;
    });

    if (typeof updateCharts === 'function') {
        updateCharts(rows, dc, callbackCountsByDate, timelineData, chartExtras);
    }
}

function openRescheduleModal() {
    try {
        console.log("openRescheduleModal called");
        const modal = document.getElementById('modal-reschedule');
        if (!modal) {
            console.error("modal-reschedule not found in DOM");
            alert("Reschedule modal element not found!");
            return;
        }
        
        const filterFrom = document.getElementById('filter-date-from')?.value;
        const filterTo = document.getElementById('filter-date-to')?.value;
        
        const fromInput = document.getElementById('reschedule-date-from');
        const toInput = document.getElementById('reschedule-date-to');
        if (fromInput && filterFrom) fromInput.value = filterFrom;
        if (toInput && filterTo) toInput.value = filterTo;
        
        const targetInput = document.getElementById('reschedule-target-datetime');
        if (targetInput) {
            const d = new Date();
            d.setHours(d.getHours() + 1);
            d.setMinutes(0);
            const pad = n => String(n).padStart(2, '0');
            const localIsoStr = `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
            targetInput.value = localIsoStr;
        }
        
        const notIntCheck = document.getElementById('reschedule-opt-not-interested');
        if (notIntCheck) notIntCheck.checked = false;
        const warningBox = document.getElementById('reschedule-warning-not-interested');
        if (warningBox) warningBox.style.display = 'none';
        
        if (typeof openModal === 'function') {
            openModal('modal-reschedule');
        } else {
            modal.classList.add('active');
            modal.classList.add('open');
        }
        console.log("modal-reschedule opened successfully");
    } catch (e) {
        console.error("Error in openRescheduleModal:", e);
        alert("Error opening reschedule modal: " + e.message + "\n" + e.stack);
    }
}

function toggleRescheduleNotInterestedWarning(checkbox) {
    const box = document.getElementById('reschedule-warning-not-interested');
    if (box) {
        box.style.display = checkbox.checked ? 'block' : 'none';
    }
}

async function submitRescheduleCampaign() {
    const fromDateVal = document.getElementById('reschedule-date-from')?.value;
    const toDateVal = document.getElementById('reschedule-date-to')?.value;
    const targetDtVal = document.getElementById('reschedule-target-datetime')?.value;
    
    if (!targetDtVal) {
        if (typeof showToast === 'function') showToast('Please select a target reschedule date & time.', 'error');
        return;
    }
    
    const categories = [];
    if (document.getElementById('reschedule-opt-failed')?.checked) categories.push('failed');
    if (document.getElementById('reschedule-opt-cut-middle')?.checked) categories.push('cut_middle');
    if (document.getElementById('reschedule-opt-interested')?.checked) categories.push('interested');
    if (document.getElementById('reschedule-opt-not-interested')?.checked) categories.push('not_interested');
    
    if (categories.length === 0) {
        if (typeof showToast === 'function') showToast('Please select at least one outcome category to target.', 'error');
        return;
    }
    
    const fromTime = fromDateVal ? new Date(fromDateVal + 'T00:00:00').getTime() / 1000 : null;
    const toTime = toDateVal ? new Date(toDateVal + 'T23:59:59').getTime() / 1000 : null;
    const rescheduleTime = new Date(targetDtVal).getTime() / 1000;
    
    try {
        const payload = {
            from_time_epoch: fromTime,
            to_time_epoch: toTime,
            categories: categories,
            reschedule_time_epoch: rescheduleTime
        };
        
        const res = await fetch(apiUrl('/api/campaign/reschedule-outcomes'), {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${token()}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(payload),
            credentials: 'same-origin'
        });
        
        if (res.status === 401 && typeof logout === 'function') {
            logout();
            return;
        }
        
        const data = await res.json();
        if (!res.ok) {
            throw new Error(data.detail || 'Failed to reschedule calls.');
        }
        
        if (typeof showToast === 'function') {
            showToast(`Successfully rescheduled ${data.rescheduled_count || 0} calls!`, 'success');
        }
        
        closeModal('modal-reschedule');
        
        if (typeof syncState === 'function') {
            syncState();
        }
    } catch (err) {
        console.error('Rescheduling failed:', err);
        if (typeof showToast === 'function') {
            showToast(err.message || 'Something went wrong while rescheduling.', 'error');
        }
    }
}

/* ─── Total Called Modal Breakdown ─── */
function openTotalCalledModal() {
    var leads = Array.isArray(allLeads) ? allLeads : [];
    var calledLeads = leads.filter(isCalled);

    if (currentFilter !== 'all') {
        if (currentFilter === 'failed') {
            calledLeads = calledLeads.filter(isFailed);
        } else if (currentFilter === 'star4') {
            calledLeads = calledLeads.filter(function (l) { return (l.rating || 0) >= 4; });
        } else if (currentFilter === 'duration30') {
            calledLeads = calledLeads.filter(function (l) { return (l.duration_sec || 0) > 30; });
        } else if (currentFilter === 'Call Later') {
            calledLeads = calledLeads.filter(function (l) {
                var d = effectiveDispo(l);
                return d === 'Call Later' || d === 'Callback' || d === 'Busy';
            });
        } else {
            calledLeads = calledLeads.filter(function (l) { return effectiveDispo(l) === currentFilter; });
        }
    }

    var fromDate = typeof getFilterDate === 'function' ? getFilterDate('filter-date-from', true) : null;
    var toDate = typeof getFilterDate === 'function' ? getFilterDate('filter-date-to', false) : null;
    if (fromDate || toDate) {
        calledLeads = calledLeads.filter(function (l) {
            if (!l.start_time) return false;
            var t = l.start_time * 1000;
            if (fromDate && t < fromDate.getTime()) return false;
            if (toDate && t > toDate.getTime()) return false;
            return true;
        });
    }

    var calledCount = calledLeads.length;

    var answered = 0, busy = 0, interested = 0, notInterested = 0, failed = 0;
    for (var i = 0; i < calledLeads.length; i++) {
        var l = calledLeads[i];
        var s = String(l.status || '').toLowerCase();
        var d = effectiveDispo(l);

        if (s === 'failed' || s === 'error') {
            failed++;
        } else if (s === 'busy' || d === 'Busy') {
            busy++;
        } else if (d === 'Interested' || (d && d.toLowerCase().indexOf('interested') !== -1 && d.toLowerCase().indexOf('not interested') === -1)) {
            interested++;
        } else if (d === 'Not Interested' || (d && d.toLowerCase().indexOf('not interested') !== -1)) {
            notInterested++;
        } else {
            answered++;
        }
    }

    document.getElementById('tc-total-number').textContent = calledCount.toLocaleString();
    document.getElementById('tc-count-total').textContent = calledCount.toLocaleString();
    document.getElementById('tc-count-answered').textContent = answered;
    document.getElementById('tc-count-busy').textContent = busy;
    document.getElementById('tc-count-interested').textContent = interested;
    document.getElementById('tc-count-notinterested').textContent = notInterested;
    document.getElementById('tc-count-failed').textContent = failed;

    openModal('modal-total-called');
}
