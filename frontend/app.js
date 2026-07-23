const API = '';
let allLeads = [];
let counties = [];
let enrichingIds = new Set();
var enrichmentErrors = {};

// ── AI Activity panel: floating, right-docked live run trace ──
// One visible panel at a time, showing whichever assess/outreach run was
// most recently triggered in the foreground — independent from
// connectRunEventsForRow()'s silent per-row bookkeeping below, which keeps
// tracking every in-flight row's own badge regardless of what the panel
// is currently showing.
var STEP_LABELS = {
  company_research: 'Collecting company evidence',
  company_identity: 'Validating company identity',
  contact_discovery: 'Finding contact candidates',
  contact_validation: 'Checking contact evidence',
  sales_context: 'Building commercial assessment',
  outreach_prep: 'Preparing outreach context',
};
var ASSESS_STEPS = ['company_research', 'company_identity', 'contact_discovery', 'contact_validation', 'sales_context'];
var OUTREACH_STEPS = ['outreach_prep'];
var _activityEvents = {};
var _activitySteps = ASSESS_STEPS;
var _activityStartTime = null;
var _activityPanelEventSource = null;
var _activityTickInterval = null;

function glyphFor(status) {
  if (status === 'complete') return '<span class="aap-glyph complete">✓</span>';
  if (status === 'running') return '<span class="aap-glyph running"></span>';
  if (status === 'failed') return '<span class="aap-glyph failed">✕</span>';
  return '<span class="aap-glyph pending">○</span>';
}

// Split from the trace list on purpose: the elapsed-time ticker calls only
// this, every second, while running. Earlier this was one function that
// also rebuilt <ul id="aap-trace"> — recreating those <li> nodes every
// second retriggered their CSS entrance animation every second, which is
// what read as the panel "appearing and disappearing."
function renderStatusText(status) {
  var panel = document.getElementById('ai-activity-panel');
  var statusEl = document.getElementById('aap-status');
  if (panel) panel.setAttribute('data-status', status);
  if (statusEl) {
    if (status === 'running') {
      var elapsed = _activityStartTime ? ((Date.now() - _activityStartTime) / 1000).toFixed(0) : '0';
      statusEl.textContent = 'Running · ' + elapsed + 's';
    } else {
      var secs = _activityStartTime ? ((Date.now() - _activityStartTime) / 1000).toFixed(1) : '?';
      statusEl.textContent = status === 'complete' ? (secs + 's') : 'Failed';
    }
  }
}

function renderActivityPanel(status) {
  var ul = document.getElementById('aap-trace');
  if (ul) {
    ul.innerHTML = _activitySteps.map(function(step) {
      var ev = _activityEvents[step];
      var label = (ev && ev.label) || STEP_LABELS[step];
      var st = ev ? ev.status : 'pending';
      var clickable = st === 'complete';
      return '<li class="' + (clickable ? 'clickable' : '') + '"' + (clickable ? ' onclick="showActivityDetail(\'' + step + '\')"' : '') + '>'
        + glyphFor(st) + '<span>' + escHtml(label) + '</span></li>';
    }).join('');
  }
  renderStatusText(status);
}

function connectRunEvents(runId, opts) {
  opts = opts || {};
  _activitySteps = opts.steps || ASSESS_STEPS;
  if (_activityPanelEventSource) { _activityPanelEventSource.close(); }
  if (_activityTickInterval) { clearInterval(_activityTickInterval); _activityTickInterval = null; }
  _activityEvents = {};
  _activityStartTime = Date.now();
  var panel = document.getElementById('ai-activity-panel');
  var companyEl = document.getElementById('aap-company');
  var detailEl = document.getElementById('aap-detail');
  if (detailEl) detailEl.classList.add('hidden');
  if (companyEl) companyEl.textContent = opts.companyName || '';
  if (panel) { panel.classList.remove('collapsed'); panel.classList.add('visible'); }
  renderActivityPanel('running');
  // Live-ticking elapsed time while running — this is the "still thinking"
  // signal a live SSE event can't provide by itself for slow steps.
  _activityTickInterval = setInterval(function() { renderStatusText('running'); }, 1000);

  function handleTerminal(status) {
    if (_activityTickInterval) { clearInterval(_activityTickInterval); _activityTickInterval = null; }
    renderActivityPanel(status);
    if (opts.onDone) opts.onDone(status);
  }

  var es = new EventSource('/api/runs/' + runId + '/events');
  _activityPanelEventSource = es;
  es.onmessage = function(ev) {
    var data = JSON.parse(ev.data);
    if (data.step === '_run') {
      es.close();
      if (_activityPanelEventSource === es) _activityPanelEventSource = null;
      handleTerminal(data.status);
      return;
    }
    _activityEvents[data.step] = data;
    renderActivityPanel('running');
  };
  es.onerror = function() {
    es.close();
    if (_activityPanelEventSource === es) _activityPanelEventSource = null;
    if (_activityTickInterval) { clearInterval(_activityTickInterval); _activityTickInterval = null; }
  };
}

// Turns each step's real, already-computed metadata into a short narrative
// — grounded entirely in what the backend actually measured, never
// invented text, so "what the AI is thinking" reads as plain sentences
// instead of a raw key:value dump.
function describeStepMetadata(step, ev) {
  var m = ev.metadata || {};
  var lines = [];
  if (step === 'company_research') {
    var n = m.evidence_sources;
    lines.push(n ? ('Reviewed ' + n + ' evidence source' + (n === 1 ? '' : 's') + ' — company website, search results, and public registries.') : 'No usable evidence sources were found.');
  } else if (step === 'company_identity') {
    lines.push(m.has_website ? 'A verified official website was identified for this company.' : 'No official website could be confidently attributed to this company.');
    if (m.research_confidence != null) lines.push('Overall research confidence: ' + m.research_confidence + '%.');
  } else if (step === 'contact_discovery') {
    var c = m.contacts_checked;
    lines.push(c ? ('Found ' + c + ' contact candidate' + (c === 1 ? '' : 's') + ' worth evaluating.') : 'No named contact candidates were found in the available sources.');
  } else if (step === 'contact_validation') {
    var h = m.high_confidence_contacts;
    lines.push(h ? (h + ' contact' + (h === 1 ? ' meets' : 's meet') + ' the high-confidence bar.') : 'None of the candidates met the high-confidence bar — treat contact details as tentative.');
  } else if (step === 'sales_context') {
    lines.push(m.guard_passed ? ('Guard checks passed with a score of ' + m.guard_score + '/100.') : ('Guard checks flagged issues — score ' + m.guard_score + '/100, review before relying on this assessment.'));
  } else if (step === 'outreach_prep') {
    var w = m.quality_warnings;
    lines.push(w ? (w + ' quality concern' + (w === 1 ? '' : 's') + ' were flagged and addressed in a rewrite before this draft was kept.') : 'The draft passed quality checks on the first attempt — no generic filler or unsupported claims detected.');
  }
  if (ev.status === 'failed') lines.push('This step did not complete successfully.');
  return lines;
}

function showActivityDetail(step) {
  var ev = _activityEvents[step];
  var el = document.getElementById('aap-detail');
  if (!el || !ev) return;
  var lines = describeStepMetadata(step, ev);
  var html = lines.map(function(l) { return '<p style="margin:0 0 6px;">' + escHtml(l) + '</p>'; }).join('');
  html += '<div style="opacity:.65;font-size:10px;margin-top:2px;">Checked at ' + escHtml(ev.completed_at || '') + '</div>';
  el.innerHTML = html;
  el.classList.remove('hidden');
}

document.addEventListener('DOMContentLoaded', function() {
  var header = document.getElementById('aap-header');
  var closeBtn = document.getElementById('aap-close');
  var panel = document.getElementById('ai-activity-panel');
  if (header && panel) {
    header.addEventListener('click', function(e) {
      if (e.target === closeBtn) return;
      panel.classList.toggle('collapsed');
    });
  }
  if (closeBtn && panel) {
    closeBtn.addEventListener('click', function(e) {
      e.stopPropagation();
      panel.classList.remove('visible');
    });
  }
});

function frontendLog(msg) {
  try {
    var blob = new Blob([JSON.stringify({msg:msg, ts:Date.now()})], {type:'application/json'});
    navigator.sendBeacon('/api/log', blob);
  } catch(e) {}
  console.log('[EVENT]', msg);
}

// Safe DOM text-set — a missing element here previously threw and silently
// killed the rest of loadStats() (including the Top Opportunities render,
// which runs last in that same try block). Never let one missing/renamed
// element take down everything after it.
function setText(id, value) {
  var el = document.getElementById(id);
  if (el) el.textContent = value;
}

function getEnrichingIds() { return enrichingIds; }

async function initDashboard() {
  restoreFiltersFromUrl();
  await Promise.all([loadStats(), loadCounties()]);
  restoreFiltersFromUrl();
  await loadLeads();
  var savedY = sessionStorage.getItem('leadReturnScrollY');
  if (savedY != null) {
    window.scrollTo(0, parseInt(savedY, 10));
    sessionStorage.removeItem('leadReturnScrollY');
  }
  await reconcileActiveRuns();
}

// ── Run reconciliation: server truth for "what's actually running" ──
// Replaces relying on the in-memory enrichingIds Set surviving navigation —
// it doesn't (a full page reload wipes it), so on every load we ask the
// backend which assessments are actually still in flight and reconnect to
// them, instead of showing a stale "Assess" button for a run that's still
// going server-side.
function connectRunEventsForRow(runId, companyId) {
  var es = new EventSource('/api/runs/' + runId + '/events');
  es.onmessage = function(ev) {
    var data = JSON.parse(ev.data);
    if (data.step === '_run') {
      es.close();
      enrichingIds.delete(companyId);
      apiFetch('/api/leads/' + companyId).then(function(detail) {
        // detail is the CompanyDetailResponse shape ({..., enrichment: {...}}),
        // not the flat list-row shape — qualification_score lives nested
        // under enrichment, same as the pre-existing enrichSingle() pattern
        // this replaces (`const lead = data.enrichment || data;`).
        var enr = detail.enrichment || {};
        var idx = allLeads.findIndex(function(l) { return l.id === companyId; });
        if (idx !== -1) {
          allLeads[idx].qualification_score = enr.qualification_score != null ? enr.qualification_score : null;
          allLeads[idx].recommended_angle = enr.recommended_angle || allLeads[idx].recommended_angle;
          allLeads[idx].employee_band = enr.employee_band || allLeads[idx].employee_band;
        }
        applyFilters();
        loadStats();
      }).catch(function() {});
    }
  };
  es.onerror = function() { es.close(); enrichingIds.delete(companyId); };
}

async function reconcileActiveRuns() {
  try {
    var active = await apiFetch('/api/runs/active');
    var runs = (active.runs || []).filter(function(r) { return r.kind === 'assess' && r.status === 'running'; });
    if (!runs.length) return;
    runs.forEach(function(r) {
      enrichingIds.add(r.company_id);
      connectRunEventsForRow(r.run_id, r.company_id);
    });
    applyFilters();
  } catch (e) { /* non-fatal */ }
}

async function loadStats() {
  try {
    const s = await apiFetch('/api/stats');
    // single source of truth — all widgets derive from this object
    var total   = s.total_companies || 0;
    var assessed = s.assessed || 0;
    var awaiting = s.awaiting || 0;
    var strong   = s.strong || 0;
    var moderate = s.moderate || 0;
    var lower    = s.lower || 0;
    var review   = s.needs_review || 0;
    var qualified = s.qualified || 0;
    var avgFit    = s.average_fit || 0;
    var outreach  = s.outreach_ready || 0;

    // Stat cards
    setText('stat-total', total.toLocaleString());
    setText('stat-qualified', qualified.toLocaleString());
    setText('stat-outreach', outreach.toLocaleString());
    setText('stat-avg-fit', avgFit > 0 ? avgFit.toFixed(1) : '--');
    setText('dash-model', 'Model: ' + (s.model || '--'));

    if (total > 0) {
      var qOfTotal = ((qualified / total) * 100).toFixed(1);
      var qOfAssessed = assessed > 0 ? ((qualified / assessed) * 100).toFixed(1) : '0.0';
      setText('stat-qualified-pct',
        qualified + ' / ' + total.toLocaleString() + ' (' + qOfTotal + '%) \u00b7 '
        + qualified + ' of ' + assessed + ' assessed (' + qOfAssessed + '%)');
      var ePct = ((outreach / total) * 100).toFixed(1);
      setText('stat-outreach-pct', outreach + ' / ' + total.toLocaleString() + ' (' + ePct + '%)');
    }

    // Pipeline Health: bracket legend counts
    setText('bracket-strong', strong);
    setText('bracket-moderate', moderate);
    setText('bracket-lower', lower);
    setText('bracket-review', review);
    setText('bracket-unqualified', awaiting + ' awaiting');

    // Assessed ratio — shared formatting for donut, bar, and label
    var pct = total > 0 ? (assessed / total * 100) : 0;
    var fmtPct;
    if (pct === 0) fmtPct = '0%';
    else if (pct < 0.01) fmtPct = pct.toFixed(3) + '%';
    else if (pct < 0.1) fmtPct = pct.toFixed(2) + '%';
    else if (pct < 10) fmtPct = pct.toFixed(1) + '%';
    else fmtPct = Math.round(pct) + '%';
    var pp = document.getElementById('pipeline-pct');
    if (pp) pp.textContent = assessed + ' / ' + total.toLocaleString() + ' (' + fmtPct + ')';

    // Donut / gauge
    var gaugeFill = document.getElementById('pipeline-gauge-fill');
    if (gaugeFill) {
      var circ = 263.89;
      gaugeFill.style.strokeDashoffset = circ - (pct / 100) * circ;
    }
    var gaugePct = document.getElementById('pipeline-gauge-pct');
    if (gaugePct) gaugePct.textContent = fmtPct;

    // Multicolor stacked pipeline bar
    var segs = [
      { label: 'Strong Fit',   count: strong,   pct: total > 0 ? (strong   / total * 100) : 0, color: '#34d399' },
      { label: 'Moderate Fit', count: moderate,  pct: total > 0 ? (moderate / total * 100) : 0, color: '#38BDF8' },
      { label: 'Lower Fit',    count: lower,     pct: total > 0 ? (lower    / total * 100) : 0, color: '#6d75a0' },
      { label: 'Needs Review', count: review,    pct: total > 0 ? (review   / total * 100) : 0, color: '#fb7185' },
      { label: 'Awaiting',     count: awaiting,  pct: total > 0 ? (awaiting / total * 100) : 0, color: 'rgba(255,255,255,0.06)' },
    ];
    var barEl = document.getElementById('pipeline-bar');
    if (barEl) {
      barEl.innerHTML = segs.filter(function(s) { return s.count > 0; }).map(function(s) {
        return '<div style="width:' + s.pct.toFixed(1) + '%;background:' + s.color + ';min-width:2px;height:100%;transition:width .6s ease;" title="' + s.label + ': ' + s.count + ' (' + s.pct.toFixed(1) + '%)"></div>';
      }).join('');
    }

    // Top Opportunities
    renderTopOpportunities();
  } catch (e) {
    console.error('Stats failed:', e);
  }
}

async function loadCounties() {
  try {
    const data = await apiFetch('/api/counties');
    counties = data.counties || [];
    const sel = document.getElementById('filter-county');
    if (!sel) return;
    counties.forEach(c => {
      const opt = document.createElement('option');
      opt.value = c;
      opt.textContent = c;
      sel.appendChild(opt);
    });
  } catch (e) {
    console.error('Counties failed:', e);
  }
}

async function renderTopOpportunities() {
  var el = document.getElementById('top-opps');
  if (!el) return;
  try {
    var data = await apiFetch('/api/leads?sort_by=score&sort_dir=desc&limit=7&min_score=1');
    var top = (data.leads || []).slice(0, 7);
    if (top.length === 0) {
      el.innerHTML = '<div class="opp-empty" style="padding:24px;text-align:center;color:var(--text-muted);font-size:12px;">Qualify leads to populate top opportunities</div>';
      return;
    }
    el.innerHTML = top.map(function(o) {
      var croStatus = (o.cro_status || '').toLowerCase();
      var croDot = croStatus === '' ? '<span class="lead-cro-dot" style="background:var(--border)"></span>' : '<span class="lead-cro-dot ' + (croStatus === 'normal' ? 'active' : 'inactive') + '"></span>';
      var croDisplay = croStatus === 'normal' ? 'Active' : croStatus === '' ? '' : 'Dissolved';
      var score = o.qualification_score || 0;
      var lvl = confidenceLevel(score);
      var ring = score > 0 ? '<span class="fit-ring ' + lvl + '" title="' + score + '%" style="--pct:' + score + ';"><span class="fit-ring-num">' + score + '</span></span>' : '<span class="lead-badge-muted">--</span>';
      var sizeEl = o.employee_band ? '<span class="lead-badge-assessed">' + escHtml(o.employee_band) + '</span>' : '<span class="lead-badge-muted">--</span>';
      var nameHtml = '<a href="/lead/' + o.id + '" class="lead-firm-link"><div><div class="lead-firm-name">' + escHtml(o.legal_name || '--') + '</div>' + (o.trading_name ? '<div class="lead-trading-name">' + escHtml(o.trading_name) + '</div>' : '') + '</div></a>';
      return '<div class="opp-row" data-href="/lead/' + o.id + '">'
        + '<span style="display:flex;align-items:center;gap:8px;">' + nameHtml + '</span>'
        + '<span class="col-md"><span class="lead-county">' + escHtml(o.county || '--') + '</span></span>'
        + '<span class="col-lg"><span class="lead-cro-status">' + croDot + '<span>' + escHtml(croDisplay) + '</span></span></span>'
        + '<span class="col-fit">' + ring + '</span>'
        + '<span class="col-md">' + sizeEl + '</span>'
        + '<span><svg class="lead-arrow" fill="none" stroke="currentColor" viewBox="0 0 24 24" width="14" height="14"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/></svg></span>'
        + '</div>';
    }).join('');
  } catch (e) {
    el.innerHTML = '<div class="opp-empty" style="padding:24px;text-align:center;color:var(--text-muted);font-size:12px;">Could not load opportunities</div>';
  }
}

async function loadLeads() {
  const loading = document.getElementById('leads-loading');
  const wrapper = document.getElementById('leads-table-wrapper');
  const empty = document.getElementById('leads-empty');
  if (loading) loading.classList.remove('hidden');
  if (wrapper) wrapper.classList.add('hidden');
  if (empty) { empty.classList.add('hidden'); empty.classList.remove('flex'); }
  try {
    const params = buildFilterParams();
    const data = await apiFetch('/api/leads?' + params);
    allLeads = data.leads || [];
    applyFilters({ syncUrl: false });
  } catch (e) {
    console.error('Leads failed:', e);
    showToast('Failed to load leads', 'error');
  } finally {
    if (loading) loading.classList.add('hidden');
  }
}

function buildFilterParams() {
  const p = new URLSearchParams();
  const county = document.getElementById('filter-county')?.value;
  if (county) p.set('county', county);
  // Read sort state from the table sort controller
  const sortCol = window._tableSortCol || 'score';
  const sortDirVal = window._tableSortDir || 'desc';
  p.set('sort_by', sortCol);
  p.set('sort_dir', sortDirVal);
  p.set('limit', '9999');
  return p.toString();
}

function applyFilters() {
  const options = arguments[0] || {};
  let filtered = allLeads;
  const search = document.getElementById('filter-search')?.value?.toLowerCase().trim();
  if (search) {
    filtered = filtered.filter(l =>
      (l.legal_name || '').toLowerCase().includes(search) ||
      (l.trading_name || '').toLowerCase().includes(search) ||
      (l.county || '').toLowerCase().includes(search)
    );
  }
  const countyFilter = document.getElementById('filter-county')?.value || '';
  if (countyFilter) filtered = filtered.filter(l => (l.county || '') === countyFilter);
  const statusFilter = document.getElementById('filter-status')?.value || '';
  if (statusFilter === 'qualified') filtered = filtered.filter(l => l.qualification_score != null);
  else if (statusFilter === 'with_email') filtered = filtered.filter(l => l.email_status === 'draft');
  else if (statusFilter === 'pending') filtered = filtered.filter(l => l.qualification_score == null);
  const assessedFilter = document.getElementById('filter-assessed')?.value || '';
  if (assessedFilter) {
    const now = Date.now();
    const THRESHOLDS = { '24h': 24 * 3600e3, '7d': 7 * 86400e3, '30d': 30 * 86400e3 };
    filtered = filtered.filter(l => {
      if (!l.assessed_at) return false;
      const ts = new Date(l.assessed_at).getTime();
      if (isNaN(ts)) return false;
      const age = now - ts;
      return assessedFilter === 'older' ? age > THRESHOLDS['30d'] : age <= THRESHOLDS[assessedFilter];
    });
  }
  if (options.syncUrl !== false) syncFiltersToUrl();
  renderLeads(filtered);
}

function clearFilters() {
  document.getElementById('filter-search').value = '';
  document.getElementById('filter-county').value = '';
  document.getElementById('filter-status').value = '';
  const assessedEl = document.getElementById('filter-assessed');
  if (assessedEl) assessedEl.value = '';
  if (window.resetBracketFilter) window.resetBracketFilter();
  else applyFilters();
}

function currentListPath() {
  const qs = window.location.search || '';
  return '/' + qs;
}

function restoreFiltersFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const q = params.get('q') || '';
  const county = params.get('county') || '';
  const status = params.get('status') || '';
  const assessed = params.get('assessed') || '';
  const searchEl = document.getElementById('filter-search');
  const countyEl = document.getElementById('filter-county');
  const statusEl = document.getElementById('filter-status');
  const assessedEl = document.getElementById('filter-assessed');
  if (searchEl) searchEl.value = q;
  if (countyEl) countyEl.value = county;
  if (statusEl) statusEl.value = status;
  if (assessedEl) assessedEl.value = assessed;
  sessionStorage.setItem('leadReturnUrl', currentListPath());
}

function syncFiltersToUrl() {
  const params = new URLSearchParams();
  const search = document.getElementById('filter-search')?.value?.trim();
  const county = document.getElementById('filter-county')?.value;
  const status = document.getElementById('filter-status')?.value;
  const assessed = document.getElementById('filter-assessed')?.value;
  if (search) params.set('q', search);
  if (county) params.set('county', county);
  if (status) params.set('status', status);
  if (assessed) params.set('assessed', assessed);
  const next = params.toString() ? '/?' + params.toString() : '/';
  window.history.replaceState(null, '', next);
  sessionStorage.setItem('leadReturnUrl', next);
}

let sortKey = '';
let sortDir = 'desc';

function setSort(key) {
  if (sortKey === key) sortDir = sortDir === 'asc' ? 'desc' : 'asc';
  else { sortKey = key; sortDir = 'desc'; }
  var bandOrder = { '1-10':1, '11-50':2, '51-200':3, '200+':4 };
  allLeads.sort(function(a, b) {
    var va = key === 'employee_band' ? bandOrder[a[key]] : a[key];
    var vb = key === 'employee_band' ? bandOrder[b[key]] : b[key];
    if (va == null) va = -Infinity;
    if (vb == null) vb = -Infinity;
    if (typeof va === 'string') va = va.toLowerCase();
    if (typeof vb === 'string') vb = vb.toLowerCase();
    if (va < vb) return sortDir === 'asc' ? -1 : 1;
    if (va > vb) return sortDir === 'asc' ? 1 : -1;
    return 0;
  });
  document.querySelectorAll('.sort-arrow').forEach(function(el) { el.textContent = ''; });
  var arrow = document.getElementById('arrow-' + key);
  if (arrow) arrow.textContent = sortDir === 'asc' ? ' \u25b2' : ' \u25bc';
  applyFilters();
}

function fmtAssessedAt(iso) {
  // Assessment date AND time — sales needs to see how fresh an assessment is.
  if (!iso) return '';
  try {
    var d = new Date(iso);
    return d.toLocaleDateString('en-IE', { day: 'numeric', month: 'short', year: 'numeric' })
      + ', ' + d.toLocaleTimeString('en-IE', { hour: '2-digit', minute: '2-digit' });
  } catch (e) { return iso; }
}

function renderLeads(leads) {
  const tbody = document.getElementById('leads-tbody');
  const wrapper = document.getElementById('leads-table-wrapper');
  const empty = document.getElementById('leads-empty');
  const countEl = document.getElementById('lead-count');
  if (!tbody) return;
  countEl.textContent = leads.length + ' firm' + (leads.length !== 1 ? 's' : '');
  if (leads.length === 0) {
    if (wrapper) wrapper.classList.add('hidden');
    if (empty) { empty.classList.remove('hidden'); empty.classList.add('flex'); }
    return;
  }
  if (empty) { empty.classList.add('hidden'); empty.classList.remove('flex'); }
  if (wrapper) wrapper.classList.remove('hidden');
  tbody.innerHTML = '';
  const enrichingIds = getEnrichingIds();
  leads.forEach((lead, idx) => {
    const row = document.createElement('tr');
    row.className = 'lead-row';
    row.dataset.href = '/lead/' + encodeURIComponent(lead.id);
    row.style.animationDelay = Math.min(idx * 8, 300) + 'ms';

    const isEnriched = lead.qualification_score != null;
    const croStatus = (lead.cro_status || '').toLowerCase();
    const isInactive = croStatus.includes('dissolved') || croStatus.includes('liquidation') || croStatus.includes('ceased') || croStatus.includes('struck');
    const isEnriching = enrichingIds.has(lead.id);
    const errorMsg = enrichmentErrors[lead.id];

    let croDisplay = croStatus === 'normal' ? 'Active' : 'Dissolved';
    if (croStatus === '') { croDisplay = ''; }

    const croDotActive = croStatus === 'normal';
    const croDot = croStatus === ''
      ? '<span class="lead-cro-dot" style="background:var(--border)"></span>'
      : '<span class="lead-cro-dot ' + (croDotActive ? 'active' : 'inactive') + '"></span>';

    let assessEl;
    if (isInactive) {
      assessEl = '<span class="lead-badge-muted">--</span>';
    } else if (isEnriching) {
      assessEl = '<span class="lead-badge-muted">Assessing...</span>';
    } else if (errorMsg) {
      var isRateLimit = errorMsg.includes('429') || errorMsg.toLowerCase().includes('rate limit');
      assessEl = '<span class="lead-badge-error">' + (isRateLimit ? 'Rate limit' : 'Failed') + '</span>';
    } else if (isEnriched) {
      assessEl = '<button class="lead-btn lead-btn-assess reassess-btn" data-id="' + lead.id + '" title="Re-run assessment">Re-assess</button>';
    } else {
      assessEl = '<button class="lead-btn lead-btn-assess qualify-btn" data-id="' + lead.id + '">Assess</button>';
    }

    let emailBadge;
    if (isInactive) {
      emailBadge = '<span class="lead-badge-muted">--</span>';
    } else if (lead.email_status === 'draft') {
      emailBadge = '<span class="lead-badge-ready">Ready</span>';
    } else if (isEnriched) {
      emailBadge = '<button class="lead-btn lead-btn-email gen-email-btn" data-id="' + lead.id + '">Generate</button>';
    } else {
      emailBadge = '<span class="lead-badge-muted">--</span>';
    }

    let sizeEl;
    if (isInactive) {
      sizeEl = '<span class="lead-badge-muted">--</span>';
    } else if (lead.employee_band) {
      sizeEl = '<span class="lead-badge-assessed">' + escHtml(lead.employee_band) + '</span>';
    } else {
      sizeEl = '<span class="lead-badge-muted">--</span>';
    }

    var fitCircleHtml = assessEl;
    if (isEnriched && lead.qualification_score != null) {
      var fs = Math.min(lead.qualification_score, 100);
      var lvl = confidenceLevel(fs);
      fitCircleHtml = '<span class="fit-ring ' + lvl + '" title="' + fs + '%" style="--pct:' + fs + ';"><span class="fit-ring-num">' + fs + '</span></span>';
    }
    if (isEnriched && lead.assessed_at) {
      fitCircleHtml += '<div class="lead-assessed-at" title="When this assessment was run">' + escHtml(fmtAssessedAt(lead.assessed_at)) + '</div>';
    }

    row.innerHTML = '<td>'
      + '<a href="/lead/' + lead.id + '" class="lead-firm-link">'
        + '<div>'
          + '<div class="lead-firm-name">' + escHtml(lead.legal_name || '--') + '</div>'
          + (lead.trading_name ? '<div class="lead-trading-name">' + escHtml(lead.trading_name) + '</div>' : '')
        + '</div>'
      + '</a>'
    + '</td>'
    + '<td class="col-md"><span class="lead-county">' + escHtml(lead.county || '--') + '</span></td>'
    + '<td class="col-lg"><span class="lead-cro-status">' + croDot + '<span>' + escHtml(croDisplay) + '</span></span></td>'
    + '<td class="col-fit">' + fitCircleHtml + '</td>'
    + '<td class="col-preview"><button type="button" class="lead-preview-btn" data-id="' + lead.id + '" title="Quick preview" aria-label="Quick preview"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/></svg></button></td>'
    + '<td class="col-md">' + sizeEl + '</td>'
    + '<td class="col-lg" style="text-align:center;">' + emailBadge + '</td>'
    + '<td><svg class="lead-arrow" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/></svg></td>';
    tbody.appendChild(row);
  });
}

document.addEventListener('click', function(e) {
  const btn = e.target.closest('.gen-email-btn');
  if (btn) {
    e.stopPropagation();
    e.preventDefault();
    generateEmail(btn);
  }
  const qualifyBtn = e.target.closest('.qualify-btn,.reassess-btn');
  if (qualifyBtn) {
    e.stopPropagation();
    e.preventDefault();
    var _preAssessScrollY = window.scrollY;
    frontendLog('CLICK Assess id=' + qualifyBtn.dataset.id);
    enrichSingle(qualifyBtn.dataset.id);
    requestAnimationFrame(function() { window.scrollTo(0, _preAssessScrollY); });
    return;
  }
  const previewBtn = e.target.closest('.lead-preview-btn');
  if (previewBtn) {
    e.stopPropagation();
    e.preventDefault();
    openLeadPreview(previewBtn.dataset.id);
    return;
  }
  const row = e.target.closest('.lead-row, .opp-row');
  if (row && !e.target.closest('a,button')) {
    sessionStorage.setItem('leadReturnUrl', currentListPath());
    sessionStorage.setItem('leadReturnScrollY', String(window.scrollY));
    window.location.href = row.dataset.href;
  }
}, true);

// ===================================================================
// Helper functions
// ===================================================================

function confidenceLevel(val) {
  if (val >= 70) return 'high';
  if (val >= 40) return 'medium';
  return 'low';
}

// ── Shared contact-rendering helpers (also used by lead.htm) ──
var CHANNEL_ICONS = {
  email: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 4h16v16H4z" opacity="0"/><path stroke-linecap="round" stroke-linejoin="round" d="M3 6l9 7 9-7M4 5h16v14H4z"/></svg>',
  phone: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M22 16.92v3a2 2 0 01-2.18 2 19.79 19.79 0 01-8.63-3.07 19.5 19.5 0 01-6-6 19.79 19.79 0 01-3.07-8.67A2 2 0 014.11 2h3a2 2 0 012 1.72c.127.96.362 1.903.7 2.81a2 2 0 01-.45 2.11L8.09 9.91a16 16 0 006 6l1.27-1.27a2 2 0 012.11-.45c.907.338 1.85.573 2.81.7A2 2 0 0122 16.92z"/></svg>',
  linkedin: '<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M20.5 2h-17A1.5 1.5 0 002 3.5v17A1.5 1.5 0 003.5 22h17a1.5 1.5 0 001.5-1.5v-17A1.5 1.5 0 0020.5 2zM8 19H5v-9h3zM6.5 8.25A1.75 1.75 0 118.3 6.5a1.78 1.78 0 01-1.8 1.75zM19 19h-3v-4.74c0-1.42-.6-1.93-1.38-1.93A1.74 1.74 0 0013 14.19a.66.66 0 000 .14V19h-3v-9h2.9v1.3a3.11 3.11 0 012.7-1.4c1.55 0 3.36.86 3.36 3.66z"/></svg>',
};
var _confRank = { high: 3, medium: 2, low: 1 };

function tierColor(level) {
  return level === 'high' ? 'var(--teal)' : level === 'medium' ? 'var(--blue)' : level === 'low' ? 'var(--rose)' : 'var(--text-muted)';
}

function initialsOf(name) {
  var parts = (name || '').trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return '?';
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

var _ctReasonSeq = 0;
function renderChannelChip(kind, label, value, conf, linkHref) {
  var level = conf ? conf.level : null;
  var color = tierColor(level);
  var reasonId = 'ct-reason-' + (_ctReasonSeq++);
  var displayValue = value
    ? (linkHref ? '<a href="' + escHtml(linkHref) + '" target="_blank" rel="noopener">' + escHtml(value) + '</a>' : escHtml(value))
    : 'Not available';
  var reason = (conf && conf.reason) || 'No evidence available for this channel.';
  return '<div class="ct-channel ' + (value ? 'has-value' : 'unavailable') + '">'
    + '<span class="ct-channel-icon" style="--channel-color:' + color + ';">' + CHANNEL_ICONS[kind] + '</span>'
    + '<span class="ct-channel-val">' + displayValue + '</span>'
    + (conf ? '<button type="button" class="ct-why" aria-expanded="false" onclick="toggleCtReason(\'' + reasonId + '\', this)" title="Why this confidence level?">?</button>' : '')
    + '</div>'
    + (conf ? '<div class="ct-reason" id="' + reasonId + '">' + escHtml(reason) + '</div>' : '');
}

function toggleCtReason(id, btn) {
  var el = document.getElementById(id);
  if (!el) return;
  var open = el.classList.toggle('open');
  btn.setAttribute('aria-expanded', open ? 'true' : 'false');
}

// Best-evidenced contact first — used by both the full detail page's
// multi-contact list and the preview panel's single "best contact" card,
// so the two surfaces can never disagree about who the recommended
// contact is.
function sortContactsByConfidence(contacts) {
  return (contacts || []).slice().sort(function(a, b) {
    var ra = _confRank[(a.confidence && a.confidence.overall && a.confidence.overall.level) || ''] || 0;
    var rb = _confRank[(b.confidence && b.confidence.overall && b.confidence.overall.level) || ''] || 0;
    return rb - ra;
  });
}

function bestContact(contacts) {
  var sorted = sortContactsByConfidence(contacts);
  return sorted.length ? sorted[0] : null;
}

// ── Lead preview panel: open/close mechanics ──
var _leadPreviewCurrentId = null;

function openLeadPreview(id) {
  _leadPreviewCurrentId = id;
  var panel = document.getElementById('lead-preview-panel');
  var backdrop = document.getElementById('lead-preview-backdrop');
  var body = document.getElementById('lp-body');
  var title = document.getElementById('lp-title');
  if (!panel || !backdrop || !body) return;
  title.textContent = 'Preview';
  body.innerHTML = '<div class="lp-loading">Loading…</div>';
  panel.classList.add('open');
  backdrop.classList.add('open');
  fetchAndRenderLeadPreview(id);
}

function closeLeadPreview() {
  var panel = document.getElementById('lead-preview-panel');
  var backdrop = document.getElementById('lead-preview-backdrop');
  var body = document.getElementById('lp-body');
  if (panel) panel.classList.remove('open');
  if (backdrop) backdrop.classList.remove('open');
  if (body) body.innerHTML = '';
  _leadPreviewCurrentId = null;
}

// Placeholder for Task 4 — replaced there with the real fetch+render.
function fetchAndRenderLeadPreview(id) {
  var body = document.getElementById('lp-body');
  if (body) body.innerHTML = '<div class="lp-loading">Loading… (rendering not implemented yet)</div>';
}

document.addEventListener('DOMContentLoaded', function() {
  var closeBtn = document.getElementById('lp-close-btn');
  if (closeBtn) closeBtn.addEventListener('click', closeLeadPreview);
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') closeLeadPreview();
  });
});

// NOTE: loadLeadDetail() and its dedicated helpers (yearsSince,
// setDetailAssessmentLoading, signalLevelLabel, signalPct, copyEmail,
// copySubject) were removed 2026-07 — lead.htm has its own self-contained
// inline <script> and never loads app.js, so this ~260-line function and
// its helpers were unreachable (gated behind `onDetail`, which can only be
// true on a page that has no #leads-tbody — i.e. never, since app.js only
// ever runs on index.htm). See lead.htm's inline script for the live
// implementation of the detail page.


// ===================================================================
// Actions
// ===================================================================

async function triggerIngestion() {
  frontendLog('CLICK Ingest');
  const btn = document.getElementById('btn-ingest');
  btn.disabled = true;
  showBadge(true);
  showToast('Ingestion started...', 'info');
  try {
    await apiFetch('/api/ingest', { method: 'POST' });
    showToast('Ingestion running. Refreshing in 30s...', 'success');
    setTimeout(function() {
      Promise.all([loadStats(), loadLeads(), loadCounties()]).then(function() {
        btn.disabled = false;
        showBadge(false);
      });
    }, 30000);
  } catch (e) {
    showToast('Ingestion failed: ' + e.message, 'error');
    btn.disabled = false;
    showBadge(false);
  }
}

async function triggerEnrichAll() {
  frontendLog('CLICK Run All');
  const btn = document.getElementById('btn-enrich');
  btn.disabled = true;
  showBadge(true);
  showToast('Assessment started...', 'info');
  try {
    await apiFetch('/api/enrich-all', { method: 'POST' });
    showToast('Assessment running.', 'success');
    let polls = 0;
    const interval = setInterval(function() {
      Promise.all([loadStats(), loadLeads()]).then(function() {
        polls++;
        if (polls >= 30) {
          clearInterval(interval);
          btn.disabled = false;
          showBadge(false);
        }
      });
    }, 10000);
  } catch (e) {
    var errorMsg = e.message || '';
    if (errorMsg.includes('429') || errorMsg.toLowerCase().includes('rate limit')) {
      showToast('API rate limit reached. Please try again later.', 'error');
    } else {
      showToast('Assessment failed: ' + errorMsg, 'error');
    }
    btn.disabled = false;
    showBadge(false);
  }
}

async function triggerEnrichN() {
  const btn = document.getElementById('btn-enrich');
  const input = document.getElementById('input-enrich-n');
  const limit = parseInt(input.value, 10);
  if (!limit || limit < 1) { showToast('Enter a valid number', 'error'); return; }
  btn.disabled = true;
  showBadge(true);
  showToast('Assessing ' + limit + ' leads...', 'info');
  try {
    await apiFetch('/api/enrich-all?limit=' + limit, { method: 'POST' });
    showToast('Assessment running.', 'success');
    let polls = 0;
    const interval = setInterval(function() {
      Promise.all([loadStats(), loadLeads()]).then(function() {
        polls++;
        if (polls >= 30) {
          clearInterval(interval);
          btn.disabled = false;
          showBadge(false);
        }
      });
    }, 10000);
  } catch (e) {
    var errorMsg = e.message || '';
    if (errorMsg.includes('429') || errorMsg.toLowerCase().includes('rate limit')) {
      showToast('API rate limit reached. Please try again later.', 'error');
    } else {
      showToast('Assessment failed: ' + errorMsg, 'error');
    }
    btn.disabled = false;
    showBadge(false);
  }
}

async function generateEmail(btn) {
  frontendLog('CLICK GenerateEmail');
  const companyId = typeof btn === 'string' ? btn : btn.dataset.id;
  const detailBtn = typeof btn === 'string' ? document.getElementById('btn-generate-email') : null;
  if (detailBtn) { detailBtn.disabled = true; detailBtn.innerHTML = '<span class="inline-spinner"></span>'; }
  if (typeof btn !== 'string') {
    btn.disabled = true;
    btn.style.color = '#6b7280';
    btn.innerHTML = '<span class="inline-spinner"></span>';
  }
  try {
    const data = await apiFetch('/api/email/' + companyId, { method: 'POST' });
    if (typeof btn === 'string') {
      showToast('Email generated!', 'success');
      setTimeout(function() { loadLeadDetail(companyId); }, 2000);
    } else {
      // /api/email/{id} now queues the generation in the background and
      // returns a run_id immediately — poll its status instead of assuming
      // "the request came back" means "the email is ready".
      var _rowLead = allLeads.find(function(l) { return l.id === companyId; });
      connectRunEvents(data.run_id, { steps: OUTREACH_STEPS, companyName: (_rowLead && _rowLead.legal_name) || '' });
      var pollCount = 0;
      var pollInterval = setInterval(function() {
        pollCount++;
        fetch('/api/runs/' + data.run_id).then(function(r) { return r.json(); }).then(function(run) {
          if (run.status === 'complete') {
            clearInterval(pollInterval);
            btn.outerHTML = '<span class="text-[10px] text-emerald-400 font-medium">Ready</span>';
          } else if (run.status === 'failed') {
            clearInterval(pollInterval);
            btn.disabled = false;
            btn.style.color = '#a78bfa';
            btn.textContent = 'Failed';
          }
        }).catch(function() {});
        if (pollCount >= 40) {
          clearInterval(pollInterval);
          btn.disabled = false;
          btn.style.color = '#a78bfa';
          btn.textContent = 'Retry';
        }
      }, 3000);
    }
  } catch (e) {
    var errorMsg = e.message || '';
    var isRateLimit = errorMsg.includes('429') || errorMsg.toLowerCase().includes('rate limit');
    
    if (typeof btn === 'string') {
      if (detailBtn) { detailBtn.disabled = false; detailBtn.textContent = 'Generate Outreach Email'; }
      if (isRateLimit) {
        showToast('API rate limit reached. Please try again later.', 'error');
      } else {
        showToast('Email generation failed: ' + errorMsg, 'error');
      }
    } else {
      btn.disabled = false;
      btn.style.color = '#a78bfa';
      btn.textContent = isRateLimit ? 'Retry' : 'Failed';
    }
  }
}

async function enrichSingle(companyId) {
  frontendLog('ASSESS START id=' + companyId);
  enrichingIds.add(companyId);
  // applyFilters() (not renderLeads(allLeads) directly) — this re-render
  // must still show whatever the user currently has filtered/searched for.
  // Rendering the raw allLeads array here silently discarded the active
  // search/county/status filter the instant Assess was clicked: the search
  // box kept its typed text, but the table behind it snapped back to the
  // full unfiltered list, which looked exactly like "the lead disappeared
  // and I got sent back to the older list."
  applyFilters();
  showToast('Assessing lead...', 'info');
  try {
    frontendLog('ASSESS CALLING_API id=' + companyId);
    const data = await apiFetch('/api/enrich/' + companyId, { method: 'POST' });
    frontendLog('ASSESS QUEUED id=' + companyId + ' run_id=' + data.run_id);
    showToast('Assessment queued...', 'success');
    delete enrichmentErrors[companyId];
    connectRunEventsForRow(data.run_id, companyId);
    var _rowLead = allLeads.find(function(l) { return l.id === companyId; });
    connectRunEvents(data.run_id, { steps: ASSESS_STEPS, companyName: (_rowLead && _rowLead.legal_name) || '' });
  } catch (e) {
    var errorMsg = e.message || '';
    frontendLog('ASSESS FAIL id=' + companyId + ' error=' + errorMsg.substring(0, 200));
    enrichmentErrors[companyId] = errorMsg;
    enrichingIds.delete(companyId);
    applyFilters();
    if (errorMsg.includes('429') || errorMsg.toLowerCase().includes('rate limit')) {
      showToast('API rate limit reached. Please try again later.', 'error');
    } else {
      showToast('Assessment failed: ' + errorMsg, 'error');
    }
  }
}

function copyEmail() {
  const body = document.getElementById('d-email-body')?.textContent || '';
  navigator.clipboard.writeText(body).then(function() {
    showToast('Email body copied', 'success');
  }).catch(function() { showToast('Copy failed', 'error'); });
}

function copySubject() {
  const subj = document.getElementById('d-email-subject')?.textContent || '';
  navigator.clipboard.writeText(subj).then(function() {
    showToast('Subject line copied', 'success');
  }).catch(function() { showToast('Copy failed', 'error'); });
}

async function apiFetch(url, opts) {
  opts = opts || {};
  const headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
  const res = await fetch(url, { headers: headers, ...opts });
  if (!res.ok) {
    var text;
    try {
      var json = await res.json();
      text = json.detail || json.error || json.message || JSON.stringify(json);
    } catch (_) {
      text = await res.text().catch(function() { return res.statusText; });
    }
    throw new Error(text.substring(0, 200));
  }
  return res.json();
}

function formatDate(iso) {
  if (!iso) return '--';
  try {
    const d = new Date(iso.substring(0, 10));
    return d.toLocaleDateString('en-IE', { year: 'numeric', month: 'short', day: 'numeric' });
  } catch (e) { return iso; }
}

function escHtml(str) {
  if (!str) return '';
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function showBadge(visible) {
  const b = document.getElementById('status-badge');
  if (!b) return;
  b.classList.toggle('hidden', !visible);
  if (visible) b.classList.add('flex');
  else b.classList.remove('flex');
}

var toastTimer;
function showToast(msg, type) {
  type = type || 'info';
  const toast = document.getElementById('toast');
  const msgEl = document.getElementById('toast-msg');
  const iconEl = document.getElementById('toast-icon');
  if (!toast || !msgEl) return;
  msgEl.textContent = msg;
  if (iconEl) {
    iconEl.textContent = type === 'success' ? '\u2713' : type === 'error' ? '\u2715' : '\u2139';
    iconEl.style.color = type === 'success' ? '#10b981' : type === 'error' ? '#ef4444' : '#60a5fa';
  }
  toast.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(function() { toast.classList.add('hidden'); }, 4000);
}

if (document.getElementById('leads-tbody')) {
  initDashboard();
}
