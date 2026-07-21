# Lead Preview Panel — Design Spec

## Origin

Boss feedback on the demo: *"The one consideration I would suggest is how does a sales person use it quickly? I would propose from the list of leads you have a preview option that gives a sales person the high level. Contact details, high level explanation why it's be scored as it has and the approach they should take."*

## Goal

Let a salesperson working down the leads list get "who do I call, why is this lead worth it, and what do I open with" without leaving the list — no full-page navigation required to make the go/no-go call on a lead.

## Decisions made during brainstorming

| Question | Decision |
|---|---|
| Trigger | Dedicated preview icon on each row, next to the fit-score ring. Existing firm-name link and buttons (Assess / Generate) are untouched. |
| Surface | Slide-over panel from the right edge. List stays visible underneath (vs. a centered modal, which fully hides it, or inline row-expand, which reflows the list). |
| Content depth | Minimal + top score driver: executive summary (2-4 sentences), the opening angle (the actual conversational hook), and the single highest-`pct` `opportunity_signal` dimension — not the full 6-dimension scorecard, not discovery questions, not personalisation reference points. |
| Unassessed leads | Preview icon shows on every row. For an unassessed lead, the panel shows registry basics (county, CRO status, years operating) plus an "Assess this lead" button that calls the existing `enrichSingle(companyId)` (`app.js:826`) — the same function the row's own `.qualify-btn` already calls. |
| Actions in panel | Read-only, plus one "Open full profile" link to `/lead/{id}` for anything deeper (full contact list, Generate Email, re-assess). No action buttons duplicated into the panel beyond the unassessed-lead Assess CTA above. |
| Data source | Existing `/api/leads/{id}` detail endpoint (no backend changes). Rejected a new lean `/api/leads/{id}/preview` endpoint — would require duplicating "pick the best contact" logic server-side for a payload-size saving that doesn't matter at this scale. Rejected an iframe of the full lead page — no precedent for iframes in this app, heavier, fragile to constrain. |

## Architecture

**Shared helper extraction (prerequisite cleanup):** `lead.htm` does not load `app.js` — its contact-rendering logic (`_confRank`, `tierColor()`, `initialsOf()`, `renderChannelChip()`, and an inline best-contact sort) is defined entirely inside `lead.htm`'s own `<script>` block. Building the panel would otherwise create a third, independent copy of "how do we pick and render the best contact." Instead:

- Move `_confRank`, `tierColor(level)`, `initialsOf(name)`, `renderChannelChip(type, label, value, confidence, href)` into `app.js` as shared functions.
- Extract the inline best-contact sort in `lead.htm`'s `renderContacts()` into a named `bestContact(contacts)` function in `app.js`.
- `lead.htm` adds `<script src="app.js"></script>` and calls the shared versions instead of its local copies. No behavior change to the existing detail page — this is a pure move, not a rewrite.

**New UI (`index.htm` + `app.js`):**
- One preview icon per row in the leads table: a new narrow column inserted between `FIT` and `SIZE` (the row template's `<td class="col-fit">` block, per `app.js`'s `renderLeads()`), rather than crowding inside the existing fit-ring cell. Renders for every row regardless of assessment status (per the unassessed-lead decision above).
- One `<div id="lead-preview-panel">` slide-over container, defined once in `index.htm`, hidden by default (off-screen via CSS transform, not `display:none`, so the slide-in/out is animatable).
- `openLeadPreview(id)` — shows the panel in a loading state, fetches, renders.
- `closeLeadPreview()` — hides the panel, clears its content so stale data isn't visible on next open.
- `renderLeadPreview(detail)` — the panel's own render function; branches on assessed vs. unassessed.

## Data flow

1. Click preview icon on a row → `openLeadPreview(id)`.
2. Panel slides in immediately showing a loading skeleton (don't wait for the fetch to start the animation — perceived speed matters for a "quickly" feature).
3. `apiFetch('/api/leads/' + id)` — existing endpoint, existing helper.
4. Branch on response:
   - **Assessed** (`qualification_score` present): render registry basics (county, CRO status, years operating), `bestContact(detail.contacts)` as a single contact card (name, role, email/phone/linkedin chips — reusing the existing chip rendering, tier color, and honest "no verified contact — registered office only" fallback when `contacts` is empty), the score plus the single `opportunity_signal` dimension with the highest `pct` (tie-break: `business_fit` wins ties, since that's the dimension the boss's "why scored" question maps to most directly — see Scoring Guidance in `assessment_system.md`), `executive_summary`, `opening_angle`, and an "Open full profile" link to `/lead/{id}`.
   - **Not assessed**: registry basics only, plus an "Assess this lead" button. Clicking it calls the same enrichment trigger the row's Assess button already uses (reusing that existing function, not a new one), shows a brief in-panel loading state, then re-fetches `/api/leads/{id}` and re-renders as the assessed case once complete.
5. Close via: X button in the panel header, clicking the dimmed area outside the panel, or Escape key.
6. Opening a different row's preview while one is already open just replaces the panel's content (fetch + re-render); no stacking, no need to close first.

## Error handling

- Fetch failure (network error, 5xx): panel shows an inline error message with a "Retry" button — matches this app's existing pattern of surfacing errors rather than a silent blank state (e.g. the rate-limit banner elsewhere in the app).
- Zero verified contacts on an assessed lead: the same honest fallback message `lead.htm` already renders ("No publicly listed directors, senior management, email, or phone number could be verified" + registered office if available) — not a broken empty card.
- Assess-from-panel fails (e.g. rate limit): same inline error treatment, consistent with the list's existing `lead-badge-error` badge behavior.
- Double-click / rapid re-trigger on the same row: `openLeadPreview` is idempotent — a second call for the same id while a fetch is in flight just lets the in-flight fetch's response render (no duplicate overlapping requests needed for a single-row preview).

## Testing

No frontend test suite exists in this repo today (`tests/` covers only the Python `engine/`, `ingestion/`, `governor/` layers). Consistent with that, this feature is verified manually via the browser tool against four cases before considering it done:
1. An already-assessed lead with a verified contact.
2. An already-assessed lead with zero verified contacts (honest fallback path).
3. An unassessed lead (registry basics + Assess CTA path, through to a successful assess-and-refresh).
4. A simulated fetch failure (error + Retry path).

## Explicitly out of scope

- Bulk/multi-lead preview.
- Any new backend endpoint or schema change.
- Editing anything from within the panel (contacts, score, etc.) — read-only by design.
- Analytics/activity-log event for opening a preview (not requested; can be added later if wanted).
