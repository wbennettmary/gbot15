# UX Contract

## Product context

- Audience: operators running inbox-placement and Workspace sender tests.
- Primary jobs: compare test outcomes, keep inbox accounts, rename grey users, and rotate spam domains.
- Target market(s): global email operations.
- Active locales: English UI with browser locale-aware dates.
- Language/content register and native-review policy: concise operational English.
- Timezone/calendar policy: server timestamps are displayed in the browser locale; stored analysis snapshots retain UTC timestamps.
- Accessibility target: WCAG 2.2 AA.

## Visual contract

- Project `DESIGN.md`: `DESIGN.md`.
- Token ownership model: existing Inbox Intelligence CSS primitives remain canonical.
- Runtime design-system/token source: `templates/inbox_intelligence.html` and shared base styles.
- Mapping/export/adapters: none; legacy Flask templates use shared `ii-*` classes.
- Token drift gate: review changed `ii-*` rules against `DESIGN.md`.
- Supported themes: existing application themes.
- Design-context owner/review policy: keep new analytics controls within the existing Inbox Intelligence visual language.

## Canonical UI Map

| Capability | Canonical owner | Source of truth | Allowed variants | Verification |
|---|---|---|---|---|
| Table Selection | Best domains table selection toolbar; AFRAID Saved Lists bulk-selection toolbar | this contract + page DOM | current page / all visible saved lists | browser interaction test |
| Select/Listbox | Native `select` | existing `ii-select` styles | native | browser interaction test |
| Scrollbar | Global application stylesheet | shared base styles | table/detail overflow | browser viewport check |
| CRUD | Saved analysis routes | Flask API and owner scope | create/delete | API smoke test |

## Component behavior

| Component | Default | Hover | Focus | Active | Disabled | Busy | Error |
|---|---|---|---|---|---|---|---|
| Button | outline or semantic intent | border emphasis | visible focus ring | pressed state | muted and inert | preserve dimensions | inline status |
| Input | labeled | border emphasis | visible focus ring | n/a | muted | n/a | inline status |
| Table/list | bounded page | row emphasis | native control focus | selected checkbox | n/a | preserve table footprint | inline status |

## Dataset navigation

- Admin tables: Best domains use page navigation with 5, 10, 25, or 50 rows per page.
- Decision queues expose Inbox accounts, Inbox account,user, Grey users, Spam accounts, and Spam account,user lists; each account,user list uses the same bounded scroll surface and has a dedicated copy action.
- URL state: page and page size are transient analysis-view state because the source is a saved snapshot; they reset when changing the selected analysis.
- Empty/no-results/error/loading treatment: preserve the table frame and show an inline empty or error message.
- Selection scope: current visible page; selected count stays visible; bulk status actions apply only to selected rows and clear selection after server acknowledgement.

## Flow ledger

| Operation | Trigger | Pending | Success destination | Success feedback | Failure recovery | Focus outcome | Source ref |
|---|---|---|---|---|---|---|---|
| Bulk domain status | Select visible domains, choose Inbox/Spam/Clear | inline saved status | stay in analysis | saved count in live status | preserve prior snapshot and show error | stay in table toolbar | domain-status API |
| Individual domain status | Change row select | inline saved status | stay in row | saved count in live status | server snapshot remains authoritative | remain on row select | domain-status API |
| Saved analysis deletion | Select one or more analyses, then confirm | inline deleting state | stay in Saved Lists | deleted count in live status | keep the dialog open and show the error | restore the initiating control | bulk-delete API |
| AFRAID saved-list deletion | Select one or more visible saved lists, then confirm | modal deleting state | stay in AFRAID List Management | deleted count in live status | keep the dialog open and show the error | restore the initiating control | bulk-delete API |
| Legacy archives | Activate disclosure | load on open | stay in disclosure | loaded archive content | inline retry via refresh | summary remains focus context | existing archive API |

## Navigation and responsive behavior

- Breadcrumb/tab policy: use the existing Inbox Intelligence tabs.
- Responsive table strategy: preserve all columns through horizontal table overflow; controls wrap on narrow screens.
- Truncation/full-value access: long domain and analysis names wrap rather than disappear.
- Focus restoration: status controls remain native and the table does not navigate after save.

## Async and resilience

- Mutation default: pessimistic server-confirmed save; individual domain changes are serialized to avoid stale overwrites.
- Idempotency and duplicate-submit policy: repeated status values are safe; the queued client mutation prevents overlapping status writes.
- Offline/read-stale/write behavior: failed saves preserve the displayed snapshot and expose an inline recovery message.
- Stale-request cancellation/invalidation: saved analysis response replaces the local snapshot only after server acknowledgement.

## Permission and clipboard

- Permission UI strategy: server owner scoping and existing `inbox_intelligence` permission guard return the app’s normal login/403 behavior.
- Clipboard copy policy: explicit copy buttons emit one domain/account per line; feedback appears in the saved-analysis live status.
- Identity randomization: the Workspace Accounts & API Senders flow, bulk randomization, and the Mega Upgrade “Auth + Randomize User Aliases” action require at least one selected name type (Asian, African, Latin, or Caucasian). All four are selected by default for backward-compatible behavior; when multiple types are selected, each generated identity chooses one selected category and one locale within it so its first and last names stay within the same locale-based pool. The server validates the selection, skips admin users, preserves ASCII-safe unique email local-parts, and returns the selected categories with the run result.
- Cloudflare accounts: Settings can maintain multiple named Cloudflare connections. The shared floating Cloudflare loader and AFRAID destination loader start with all enabled connections selected, allow one or more connections to be chosen, and show the owning Cloudflare account beside every loaded domain. The API returns per-account load errors without hiding successful results from other selected connections.
- Failed-user analytics: User Inbox and AI analytics show one row per failed sender with its Workspace account and grouped failure cause(s); the copy action emits `account,user (failed)` one per line.
- AFRAID CNAME domain freshness: the Create CNAME Subdomain action validates the FreeDNS registry cache before creation and refreshes it only when older than one hour; domains missing from a complete refresh are removed before they can be selected. The explicit Sync Registry action forces a full refresh.
- AFRAID quota cleanup: loading existing FreeDNS records displays the current record count, shows 10 records per page, and preserves selections across pages before deletion.
- AFRAID analytics tagging: public AFRAID domains observed in completed inbox tests show `Domain: inbox` and/or `spam-inbox: spam`; the Domains In Selected TLD filters return only the selected placement(s).
- AFRAID domain selection: the Domains In Selected TLD control accepts a count and Select descending chooses up to that many currently available domains by descending domain name; used domains remain separate for reactivation.
- AFRAID TLD group filtering: operators can search and select multiple available TLD groups (for example, `.com`, `.net`, and `.ca`), load domains from those groups only, and carry the combined selection into CNAME creation; no group selection does not fetch the entire registry.
- AFRAID usage bookkeeping: successful CNAME creation and saved-list All/Pick verification stamp the represented AFRAID base domains as used and refresh the visible used-domain surfaces.
- Active & Completed Tests ownership: the app-user filter is server-side; admins and mailers can view all or choose an existing admin/mailer user, while other viewers are restricted to their own tests. Mailers' All view is limited to tests owned by those app-user roles.
- User Inbox Analytics scope: the app-user selector includes only existing `admin` and `mailer` users; admins and mailers can select any listed user, while other viewers receive only their own user and tests from the server.
- User Inbox Analytics test selection: the selected app user’s terminal User Inbox tests (`COMPLETED`, `PARTIAL`, `FAILED`, or `STOPPED`) are shown in a second native select before analytics is loaded.

## Verification

- Required static commands: Python compile, Jinja parse, inline JavaScript parse, `git diff --check`.
- Browser/device matrix: authenticated Chrome desktop and narrow viewport.
- Component-state coverage: empty state, populated table, individual status, bulk status, pagination, and collapsed archive disclosure.
- Canonical sibling flow used for comparison: existing Saved Lists and Active & Completed Tests controls.
