# Lead Preview Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a slide-over preview panel to the leads list so a salesperson can see contact details, why a lead scored as it did, and the recommended approach without leaving the list.

**Architecture:** A new preview icon per row (`app.js` row template + `index.htm` table header) opens a slide-over panel (`index.htm` markup + CSS, `app.js` open/close/render logic) that fetches the existing `/api/leads/{id}` detail endpoint and renders a condensed view. A prerequisite refactor moves `lead.htm`'s inline contact-rendering helpers into `app.js` so the panel and the full detail page share one implementation instead of growing a second copy.

**Tech Stack:** Vanilla JS (no build step, no framework), served as static files by FastAPI (`api/main.py`). No new dependencies, no backend changes.

## Global Constraints

- No new backend endpoints or schema changes — reuse `GET /api/leads/{id}` (`api/schemas.py:47` `CompanyDetailResponse`) as-is.
- No automated frontend test suite exists in this repo (`tests/` only covers `engine/`, `ingestion/`, `governor/`). Verification is manual via the browser tool, per the spec's Testing section — each task's "test" step is a manual check, not a `pytest` run.
- Follow the existing cache-busting convention: `index.htm` loads app.js as `<script src="/static/app.js?v=10"></script>` (`frontend/index.htm:1088`). The final task bumps this to `v=11`.
- Match existing code style: `var`/`let` mix as already used in each file, string-concatenation HTML building (no template literals introduced — the codebase doesn't use them), reuse `escHtml()` (already present in both files) for all user/company data interpolated into HTML.
- Reuse existing CSS custom properties only (`--bg-elevated`, `--border`, `--text-primary`, `--text-secondary`, `--text-muted`, `--teal`, `--blue`, `--rose`, `--shadow-panel`) — both `index.htm` and `lead.htm` define the same token set (`index.htm:39-109`), so no new tokens are needed.

---

### Task 1: Extract shared contact-rendering helpers into `app.js`

**Files:**
- Modify: `frontend/app.js` (add helpers near the bottom, after the existing `confidenceLevel` helper section starting at `app.js:656`)
- Modify: `frontend/lead.htm:1077-1130` (remove the local copies, call the shared versions)

**Interfaces:**
- Produces (for Task 4 and for `lead.htm`): `CHANNEL_ICONS` (object, keys `email`/`phone`/`linkedin`), `_confRank` (object), `tierColor(level: string|null): string`, `initialsOf(name: string|null): string`, `renderChannelChip(kind: string, label: string, value: string|null, conf: {level,reason}|null, linkHref?: string): string` (returns HTML string), `toggleCtReason(id: string, btn: HTMLElement): void`, `sortContactsByConfidence(contacts: Array|null): Array` (returns a new sorted array, best-confidence first), `bestContact(contacts: Array|null): object|null` (returns the single best contact, or `null` if the list is empty/missing).

- [ ] **Step 1: Read the exact current helpers in `lead.htm` to copy verbatim**

Run: nothing to run — this step is a review, already done during planning. The exact current source (`frontend/lead.htm:1077-1130`) is:

```js
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
```

Note: the plan's copy above un-escapes the `<\a>`, `<\button>`, `<\div>` typos present in the live file's closing tags (backslash instead of forward slash before `a`/`button`/`div` in three closing tags) back to correct `</a>`, `</button>`, `</div>` — those typos happen to work in real browsers (HTML parsers tolerate a stray backslash inside a tag name position by treating the whole thing as a bogus-but-recovered close tag) but must not be copied forward into new shared code. Fix them in the moved version.

- [ ] **Step 2: Add the helpers to `app.js`, plus two new functions**

Add this block to `frontend/app.js`, immediately after the closing `}` of `confidenceLevel` (the function block starting at `app.js:656`):

```js
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
```

- [ ] **Step 3: Load `app.js` from `lead.htm` and remove the now-duplicated local copies**

In `frontend/lead.htm`, add a script tag loading `app.js` immediately before the existing inline `<script>` at line 741:

```html
<script src="/static/app.js?v=10"></script>
<script>
```

Then delete lines 1077-1117 of `frontend/lead.htm` (the `CHANNEL_ICONS` var through the `toggleCtReason` function — everything shown in Step 1 above) since those now come from `app.js`.

Then in `renderContacts()` (`frontend/lead.htm`, the function starting at what is currently line 1119 but will shift up ~41 lines after the Step 3 deletion), replace the inline sort:

```js
var sorted = contacts.slice().sort(function(a, b) {
  var ra = _confRank[(a.confidence && a.confidence.overall && a.confidence.overall.level) || ''] || 0;
  var rb = _confRank[(b.confidence && b.confidence.overall && b.confidence.overall.level) || ''] || 0;
  return rb - ra;
});
```

with:

```js
var sorted = sortContactsByConfidence(contacts);
```

- [ ] **Step 4: Manual verification**

Start the app (`python run_server.py`), open a lead detail page for an already-assessed company with at least one contact (e.g. navigate to `/lead/{id}` for any row showing a fit score). Confirm:
- The contact card(s) render exactly as before — name, initials avatar, role, tier color, "Best contact" label on the first card when there's more than one, email/phone/LinkedIn chips with the "?" reason toggle working.
- No browser console errors (open dev tools, check for `ReferenceError` — would indicate a helper wasn't moved correctly or `app.js` didn't load before it's needed).

Expected: identical visual output to before this task; zero behavior change.

- [ ] **Step 5: Commit**

```bash
git add frontend/app.js frontend/lead.htm
git commit -m "refactor: share contact-rendering helpers between lead.htm and app.js

Prerequisite for the lead preview panel (Task 2+), which needs the same
best-contact selection and channel-chip rendering lead.htm already has.
Moves CHANNEL_ICONS, _confRank, tierColor, initialsOf, renderChannelChip,
toggleCtReason into app.js; extracts the inline best-contact sort into
sortContactsByConfidence()/bestContact(). Pure move — no behavior change
to the existing detail page."
```

---

### Task 2: Add the preview icon to each row (no behavior yet)

**Files:**
- Modify: `frontend/index.htm:1055-1056` (table header), `frontend/index.htm` CSS block (before `</style>` at line 832)
- Modify: `frontend/app.js:619-620` (row template)

**Interfaces:**
- Produces: a `<button class="lead-preview-btn" data-id="{lead.id}">` in every row, with no click handler wired yet (Task 3 wires it).

- [ ] **Step 1: Add the column header**

In `frontend/index.htm`, between the existing `Fit` header (line 1055) and `Size` header (line 1056):

```html
<th class="col-preview"></th>
```

So the header row reads (unchanged lines omitted):
```html
<th class="col-fit"><button class="sort-btn" id="sort-score" data-col="score" onclick="handleSortClick('score')">Fit <span class="sort-arrow" id="arrow-score">↕</span></button></th>
<th class="col-preview"></th>
<th class="col-md"><button class="sort-btn" data-col="size" onclick="handleSortClick('size')">Size <span class="sort-arrow" id="arrow-size">↕</span></button></th>
```

- [ ] **Step 2: Add the CSS**

Add before the `</style>` tag at `frontend/index.htm:832`:

```css
.col-preview { width: 36px; }
.lead-preview-btn {
  display: inline-flex; align-items: center; justify-content: center;
  width: 28px; height: 28px; border-radius: 6px; border: 1px solid transparent;
  background: transparent; color: var(--text-muted); cursor: pointer;
  transition: background 0.15s ease, color 0.15s ease;
}
.lead-preview-btn:hover { background: var(--bg-panel-light); color: var(--text-primary); border-color: var(--border); }
```

- [ ] **Step 3: Add the button to the row template**

In `frontend/app.js`, insert a new `<td>` between the `col-fit` cell (line 619) and the `col-md` size cell (line 620):

```js
+ '<td class="col-fit">' + fitCircleHtml + '</td>'
+ '<td class="col-preview"><button type="button" class="lead-preview-btn" data-id="' + lead.id + '" title="Quick preview" aria-label="Quick preview"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/></svg></button></td>'
+ '<td class="col-md">' + sizeEl + '</td>'
```

- [ ] **Step 4: Manual verification**

Reload the leads list in the browser. Confirm: every row (assessed and unassessed alike) shows a small eye icon between the Fit ring and the Size badge; clicking it does nothing yet (no handler wired — that's Task 3); no layout shift or overlap with neighboring columns; no console errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/index.htm frontend/app.js
git commit -m "feat: add preview icon column to leads table

Non-functional yet (Task 3 wires the click handler) - lands the column,
button markup, and styling first so the interactive behavior change is
isolated to its own commit."
```

---

### Task 3: Add the slide-over panel shell and open/close mechanics

**Files:**
- Modify: `frontend/index.htm` (panel markup before `<script src="/static/app.js?v=10">` at line 1088; CSS before `</style>` at line 832)
- Modify: `frontend/app.js` (new functions; click delegation)

**Interfaces:**
- Consumes: `lead-preview-btn` (from Task 2), `apiFetch` (`app.js:875`).
- Produces: `openLeadPreview(id: string): void`, `closeLeadPreview(): void` — both used by Task 4 onward, and callable from the row button and future close triggers.

- [ ] **Step 1: Add the panel markup**

In `frontend/index.htm`, immediately after the `ai-activity-panel` div closes (line 1086) and before `<script src="/static/app.js?v=10">` (line 1088):

```html
<div id="lead-preview-backdrop" class="lp-backdrop" onclick="closeLeadPreview()"></div>
<div id="lead-preview-panel" class="lp-panel" role="dialog" aria-modal="true" aria-label="Lead preview">
  <div class="lp-header">
    <div class="lp-header-title" id="lp-title">Preview</div>
    <button type="button" class="lp-close" id="lp-close-btn" title="Close" aria-label="Close preview">&times;</button>
  </div>
  <div class="lp-body" id="lp-body"></div>
</div>
```

- [ ] **Step 2: Add the panel CSS**

Add before `</style>` at `frontend/index.htm:832` (after the CSS added in Task 2):

```css
.lp-backdrop {
  position: fixed; inset: 0; background: rgba(0,0,0,0.5);
  opacity: 0; pointer-events: none; transition: opacity 0.2s ease; z-index: 200;
}
.lp-backdrop.open { opacity: 1; pointer-events: auto; }

.lp-panel {
  position: fixed; top: 0; right: 0; height: 100vh; width: 420px; max-width: 92vw;
  background: var(--bg-elevated); border-left: 1px solid var(--border);
  box-shadow: var(--shadow-panel); transform: translateX(100%);
  transition: transform 0.25s ease; z-index: 201;
  display: flex; flex-direction: column;
}
.lp-panel.open { transform: translateX(0); }

.lp-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 16px 20px; border-bottom: 1px solid var(--border); flex-shrink: 0;
}
.lp-header-title { font-size: 14px; font-weight: 600; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.lp-close {
  background: none; border: none; color: var(--text-muted); font-size: 22px;
  line-height: 1; cursor: pointer; padding: 4px 8px; border-radius: 6px; flex-shrink: 0;
}
.lp-close:hover { background: var(--bg-panel-light); color: var(--text-primary); }

.lp-body { flex: 1; overflow-y: auto; padding: 20px; }

@media (max-width: 480px) {
  .lp-panel { width: 100vw; max-width: 100vw; }
}
```

- [ ] **Step 3: Implement open/close functions and wire triggers**

Add to `frontend/app.js`, after the `bestContact` function added in Task 1:

```js
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
```

Then wire the row button into the existing click-delegation handler in `frontend/app.js` — add this branch to the `document.addEventListener('click', ...)` handler (`app.js:627`), immediately after the `qualifyBtn` branch (after the `return;` at line 642, before the `row` fallback block at line 644):

```js
  const previewBtn = e.target.closest('.lead-preview-btn');
  if (previewBtn) {
    e.stopPropagation();
    e.preventDefault();
    openLeadPreview(previewBtn.dataset.id);
    return;
  }
```

- [ ] **Step 4: Manual verification**

Reload the leads list. Click a row's preview icon: panel should slide in from the right showing "Loading… (rendering not implemented yet)", backdrop dims the list behind it, list stays visible/scrollable is not required at this stage (backdrop blocks interaction by design — that's correct, matches the modal-style backdrop chosen in the spec). Confirm all three close paths work: clicking the × button, clicking the dimmed backdrop, pressing Escape. Confirm clicking the row itself (not the icon) still navigates to `/lead/{id}` as before (unaffected).

- [ ] **Step 5: Commit**

```bash
git add frontend/index.htm frontend/app.js
git commit -m "feat: add slide-over panel shell with open/close mechanics

Panel opens on preview-icon click, closes via X/backdrop/Escape. Content
rendering (fetchAndRenderLeadPreview) is a placeholder here — Task 4
implements the real fetch and the assessed-lead view."
```

---

### Task 4: Render the assessed-lead preview (registry basics, contact, top signal, summary, angle)

**Files:**
- Modify: `frontend/app.js` (replace the `fetchAndRenderLeadPreview` placeholder from Task 3; add rendering helpers)

**Interfaces:**
- Consumes: `apiFetch` (`app.js:875`), `escHtml`, `bestContact`, `tierColor`, `initialsOf`, `renderChannelChip` (Task 1), `_leadPreviewCurrentId` (Task 3).
- Produces: `renderLeadPreview(detail: object): void`, `topOpportunityDimension(signal: object|null): {key: string, dim: object}|null`, `yearsFrom(iso: string|null): number|null` (a second, independent copy of `lead.htm`'s existing helper of the same name — see rationale below).

- [ ] **Step 1: Replace the placeholder fetch function**

In `frontend/app.js`, replace the `fetchAndRenderLeadPreview` function added in Task 3 with:

```js
async function fetchAndRenderLeadPreview(id) {
  var body = document.getElementById('lp-body');
  if (!body) return;
  try {
    var detail = await apiFetch('/api/leads/' + id);
  } catch (e) {
    if (_leadPreviewCurrentId !== id) return; // panel moved on to a different lead
    renderLeadPreviewError(id, e && e.message ? e.message : 'Failed to load preview.');
    return;
  }
  if (_leadPreviewCurrentId !== id) return; // user opened a different row while this was in flight
  renderLeadPreview(detail);
}
```

- [ ] **Step 2: Add the opportunity-dimension picker and a local `yearsFrom`**

Add to `frontend/app.js`, after `fetchAndRenderLeadPreview`:

```js
// Second, independent copy of lead.htm's yearsFrom() (frontend/lead.htm:968) —
// a 4-line pure function; not worth a cross-file dependency for this alone
// (the two files already each keep their own copy of escHtml for the same
// reason).
function yearsFrom(iso) {
  if (!iso) return null;
  try { return new Date().getFullYear() - parseInt(iso.slice(0, 4)); }
  catch (e) { return null; }
}

var OPP_SIGNAL_ORDER = ['business_fit', 'regulatory_fit', 'digital_maturity', 'evidence_coverage', 'payment_visibility', 'decision_maker_access'];
var OPP_SIGNAL_LABELS = {
  business_fit: 'Business fit',
  regulatory_fit: 'Regulatory fit',
  digital_maturity: 'Digital maturity',
  evidence_coverage: 'Evidence coverage',
  payment_visibility: 'Payment visibility',
  decision_maker_access: 'Decision-maker access',
};

// Highest-pct dimension wins; business_fit wins ties because it's first in
// OPP_SIGNAL_ORDER and only a strictly-greater pct replaces the running
// best — see the design spec's tie-break rule.
function topOpportunityDimension(signal) {
  if (!signal) return null;
  var best = null;
  for (var i = 0; i < OPP_SIGNAL_ORDER.length; i++) {
    var key = OPP_SIGNAL_ORDER[i];
    var dim = signal[key];
    if (!dim || typeof dim.pct !== 'number') continue;
    if (!best || dim.pct > best.dim.pct) best = { key: key, dim: dim };
  }
  return best;
}
```

- [ ] **Step 3: Add the main render function**

Add to `frontend/app.js`, after `topOpportunityDimension`:

```js
function renderLeadPreview(detail) {
  var body = document.getElementById('lp-body');
  var title = document.getElementById('lp-title');
  if (!body) return;

  var company = detail.company || detail;
  var enrichment = detail.enrichment || {};
  var na = enrichment.narrative_assessment || {};
  var isAssessed = enrichment.qualification_score !== null && enrichment.qualification_score !== undefined;

  if (title) title.textContent = company.legal_name || 'Preview';

  var registryHtml = '<div class="lp-section lp-registry">'
    + '<div class="lp-registry-row"><span class="lp-label">County</span><span>' + escHtml(company.county || '--') + '</span></div>'
    + '<div class="lp-registry-row"><span class="lp-label">CRO Status</span><span>' + escHtml(company.cro_status || '--') + '</span></div>'
    + '<div class="lp-registry-row"><span class="lp-label">Years operating</span><span>' + (yearsFrom(company.incorporation_date) != null ? yearsFrom(company.incorporation_date) : '--') + '</span></div>'
    + '</div>';

  if (!isAssessed) {
    body.innerHTML = registryHtml
      + '<div class="lp-section">'
      + '<button type="button" class="lp-assess-cta" id="lp-assess-cta-btn" data-id="' + escHtml(company.id) + '">Assess this lead</button>'
      + '</div>';
    var assessBtn = document.getElementById('lp-assess-cta-btn');
    if (assessBtn) {
      assessBtn.addEventListener('click', function() {
        renderLeadPreviewAssessing(company.id);
      });
    }
    return;
  }

  var contacts = na.contacts || [];
  var contact = bestContact(contacts);
  var contactHtml;
  if (contact) {
    var overall = contact.confidence && contact.confidence.overall;
    var tier = tierColor(overall && overall.level);
    contactHtml = '<div class="lp-section"><div class="lp-section-title">Contact</div>'
      + '<div class="ct-card" style="--tier-color:' + tier + ';">'
      + '<div class="ct-header">'
      + '<div class="ct-avatar">' + escHtml(initialsOf(contact.name || contact.full_name)) + '</div>'
      + '<div class="ct-id">'
      + '<div class="ct-name">' + escHtml(contact.name || contact.full_name || 'Unknown') + '</div>'
      + (contact.role ? '<div class="ct-role">' + escHtml(contact.role) + '</div>' : '')
      + '</div>'
      + '</div>'
      + '<div class="ct-channels">'
      + renderChannelChip('email', 'Email', contact.email, contact.confidence && contact.confidence.email)
      + renderChannelChip('phone', 'Phone', contact.phone, contact.confidence && contact.confidence.phone)
      + renderChannelChip('linkedin', 'LinkedIn', contact.linkedin_url ? 'View profile' : null, contact.confidence && contact.confidence.linkedin, contact.linkedin_url)
      + '</div>'
      + '</div></div>';
  } else {
    var address = company.registered_address;
    contactHtml = '<div class="lp-section"><div class="lp-section-title">Contact</div>'
      + '<div class="lp-no-contact">No publicly listed directors, senior management, email, or phone number could be verified.'
      + (address ? '<br><strong>Registered office:</strong> ' + escHtml(address) : '')
      + '</div></div>';
  }

  var score = enrichment.qualification_score;
  var topDim = topOpportunityDimension(na.opportunity_signal || enrichment.opportunity_signal);
  var scoreHtml = '<div class="lp-section">'
    + '<div class="lp-score-row"><span class="lp-score-num">' + escHtml(String(score)) + '</span><span class="lp-score-max">/100</span></div>'
    + (topDim ? '<div class="lp-dimension"><span class="lp-label">' + escHtml(OPP_SIGNAL_LABELS[topDim.key]) + ' (' + Math.round(topDim.dim.pct) + '%)</span><p>' + escHtml(topDim.dim.reason || '') + '</p></div>' : '')
    + '</div>';

  var summary = na.executive_summary || enrichment.executive_summary || '';
  var angle = na.opening_angle || enrichment.opening_angle || '';
  var narrativeHtml = '<div class="lp-section">'
    + (summary ? '<div class="lp-section-title">Why this score</div><p class="lp-summary">' + escHtml(summary) + '</p>' : '')
    + (angle ? '<div class="lp-section-title">Approach</div><p class="lp-angle">' + escHtml(angle) + '</p>' : '')
    + '</div>';

  var profileLinkHtml = '<div class="lp-section"><a class="lp-profile-link" href="/lead/' + escHtml(company.id) + '">Open full profile →</a></div>';

  body.innerHTML = registryHtml + contactHtml + scoreHtml + narrativeHtml + profileLinkHtml;
}

function renderLeadPreviewError(id, message) {
  var body = document.getElementById('lp-body');
  if (!body) return;
  body.innerHTML = '<div class="lp-error">'
    + '<p>' + escHtml(message) + '</p>'
    + '<button type="button" class="lp-retry-btn" id="lp-retry-btn">Retry</button>'
    + '</div>';
  var retryBtn = document.getElementById('lp-retry-btn');
  if (retryBtn) {
    retryBtn.addEventListener('click', function() {
      var body2 = document.getElementById('lp-body');
      if (body2) body2.innerHTML = '<div class="lp-loading">Loading…</div>';
      fetchAndRenderLeadPreview(id);
    });
  }
}

// Placeholder for Task 6 — replaced there with the real assess-and-refresh flow.
function renderLeadPreviewAssessing(id) {
  var body = document.getElementById('lp-body');
  if (body) body.innerHTML = '<div class="lp-loading">Assessing… (not implemented yet)</div>';
}
```

- [ ] **Step 4: Add the remaining panel-content CSS**

Add before `</style>` at `frontend/index.htm:832` (after the CSS added in Tasks 2-3):

```css
.lp-section { margin-bottom: 20px; }
.lp-section:last-child { margin-bottom: 0; }
.lp-section-title { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.03em; color: var(--text-muted); margin-bottom: 8px; }
.lp-label { font-size: 12px; color: var(--text-muted); }
.lp-registry-row { display: flex; justify-content: space-between; font-size: 13px; color: var(--text-primary); padding: 4px 0; }
.lp-loading, .lp-no-contact { font-size: 13px; color: var(--text-secondary); }
.lp-score-row { display: flex; align-items: baseline; gap: 4px; margin-bottom: 8px; }
.lp-score-num { font-size: 28px; font-weight: 700; color: var(--text-primary); }
.lp-score-max { font-size: 13px; color: var(--text-muted); }
.lp-dimension p { font-size: 13px; color: var(--text-secondary); margin-top: 4px; }
.lp-summary, .lp-angle { font-size: 13px; line-height: 1.5; color: var(--text-secondary); }
.lp-profile-link { display: inline-block; font-size: 13px; font-weight: 600; color: var(--blue); text-decoration: none; }
.lp-profile-link:hover { text-decoration: underline; }
.lp-assess-cta {
  width: 100%; padding: 10px; border-radius: 8px; border: none;
  background: var(--paybrix-red); color: #fff; font-size: 13px; font-weight: 600; cursor: pointer;
}
.lp-error { font-size: 13px; color: var(--rose); }
.lp-retry-btn {
  margin-top: 10px; padding: 6px 14px; border-radius: 6px; border: 1px solid var(--border);
  background: transparent; color: var(--text-primary); font-size: 12px; cursor: pointer;
}
```

- [ ] **Step 5: Manual verification**

Open the preview for an already-assessed lead that has at least one verified contact (check `/api/leads?sort_by=score&sort_dir=desc&limit=5` in the browser or via the "Top Opportunities" panel to find one). Confirm: title shows the firm name; registry basics show county/CRO status/years operating; contact card shows name, initials, role, and email/phone/LinkedIn chips matching what the full detail page shows for the same contact; score number and the single highest-scoring dimension (with its reason text) are shown; executive summary and opening angle paragraphs render; "Open full profile" link navigates to `/lead/{id}` correctly.

Then open the preview for an already-assessed lead with zero contacts (`na.contacts` empty — cross-check against the full detail page's "No publicly listed directors..." message to find one). Confirm the honest fallback message renders instead of a broken/empty contact card.

- [ ] **Step 6: Commit**

```bash
git add frontend/app.js frontend/index.htm
git commit -m "feat: render assessed-lead content in the preview panel

Registry basics, best-evidenced contact (or the honest no-contact
fallback), the highest-scoring opportunity_signal dimension, executive
summary, opening angle, and a link to the full profile. Unassessed leads
still show only the Task 3 placeholder pending Task 6."
```

---

### Task 5: Wire the unassessed-lead Assess CTA to the real enrichment flow

**Files:**
- Modify: `frontend/app.js` (replace the `renderLeadPreviewAssessing` placeholder from Task 4)

**Interfaces:**
- Consumes: `enrichSingle(companyId: string): Promise<void>` (existing function, `app.js:826`), and the two globals it and its internal `connectRunEventsForRow` (`app.js:211`) already populate: `enrichmentErrors[companyId]` (set on failure, `app.js:850`) and `allLeads[idx].qualification_score` (set on success once the run's SSE stream reports `_run` complete, `app.js:226`). Neither existing function is modified — the panel only reads state they already write.

`connectRunEventsForRow` (called internally by `enrichSingle`) opens its own `EventSource` and has no callback parameter and no return value — it's fire-and-forget, updating `allLeads`/`enrichmentErrors` as a side effect for the row badge to pick up on its next render. Rather than thread a new callback through two existing functions (a riskier, wider diff touching working code), the panel polls the same two globals those functions already write to, exactly as if it were "the row badge" watching for a change.

- [ ] **Step 1: Replace the placeholder**

Replace `renderLeadPreviewAssessing` in `frontend/app.js` (added in Task 4) with:

```js
function renderLeadPreviewAssessing(id) {
  var body = document.getElementById('lp-body');
  if (body) body.innerHTML = '<div class="lp-loading">Assessing…</div>';
  enrichSingle(id);
  var pollCount = 0;
  var pollInterval = setInterval(function() {
    if (_leadPreviewCurrentId !== id) { clearInterval(pollInterval); return; }
    pollCount++;
    if (enrichmentErrors[id]) {
      clearInterval(pollInterval);
      renderLeadPreviewError(id, enrichmentErrors[id]);
      return;
    }
    var row = allLeads.find(function(l) { return l.id === id; });
    if (row && row.qualification_score != null) {
      clearInterval(pollInterval);
      fetchAndRenderLeadPreview(id);
      return;
    }
    // ~3 minutes at 1s: this session's own 2026-07-21 validation run
    // measured real LLM assessment calls at 170-220s, so 180s is a floor,
    // not a guess — below it, this would time out mid-assessment on a
    // normal run.
    if (pollCount > 180) {
      clearInterval(pollInterval);
      renderLeadPreviewError(id, 'Assessment is taking longer than expected. Close and reopen the preview to check again.');
    }
  }, 1000);
}
```

- [ ] **Step 2: Manual verification**

Open the preview for a never-assessed lead. Confirm registry basics show and the "Assess this lead" button appears. Click it: panel shows "Assessing…", the underlying row in the list behind the (now-open) panel also flips to its own "Assessing..." state, and once the real assessment completes (same wait as clicking Assess directly on the row — this hits the real LLM provider, so expect the same latency as any other assessment in this app), the panel re-renders as the assessed view from Task 4 with real data. Confirm the row itself also updates to show its fit score once you close the panel.

- [ ] **Step 3: Commit**

```bash
git add frontend/app.js
git commit -m "feat: wire preview panel's Assess CTA to the real enrichment flow

Reuses enrichSingle() (app.js:826) rather than duplicating the
assess-and-poll logic a third time; re-fetches and re-renders the panel
as the assessed view once the run completes."
```

---

### Task 6: Final manual verification pass and cache-bust bump

**Files:**
- Modify: `frontend/index.htm:1088` (bump `app.js?v=10` to `?v=11`)

**Interfaces:** None — this task only re-verifies prior work end-to-end and ships the cache-bust.

- [ ] **Step 1: Bump the cache-busting version**

In `frontend/index.htm`, change:

```html
<script src="/static/app.js?v=10"></script>
```

to:

```html
<script src="/static/app.js?v=11"></script>
```

- [ ] **Step 2: Run the full manual verification pass from the spec's Testing section**

With the server running, in the browser tool, confirm all four scenarios end to end (repeating Tasks 4-5's checks in one pass to catch any interaction between them):
1. Assessed lead with a verified contact — full panel content correct.
2. Assessed lead with zero verified contacts — honest fallback shown.
3. Unassessed lead — Assess CTA works through to a re-rendered assessed view.
4. Simulated fetch failure — open dev tools, throttle/block the `/api/leads/{id}` request (e.g. via the browser's network-conditions offline toggle) for one preview open, confirm the error message and Retry button appear, then restore the network and click Retry, confirming it recovers.

Also confirm: opening a second row's preview while one is already open replaces the content correctly (no stacking, no stale data flash) — click preview on row A, then immediately click preview on row B before A's fetch would have resolved, confirm only B's data ends up displayed.

- [ ] **Step 3: Commit**

```bash
git add frontend/index.htm
git commit -m "chore: bump app.js cache-bust version for the lead preview panel

Final task of the lead-preview-panel plan (docs/superpowers/plans/2026-07-21-lead-preview-panel.md).
All four spec verification scenarios pass."
```
