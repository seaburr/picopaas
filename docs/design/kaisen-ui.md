# Kaisen (Preview) UI: mockups and spec

Status: mockup for Chas's approval, 2026-10-29. Nothing implemented. Source: section 6 of `docs/design/kaisen.md`.
Rule: **no new visual vocabulary.** Every screen below is built from the classes and components PicoPaaS already ships
(`style.css`: `.card .card-pad .btn-* .input .label .help .meta .mono .link .badge-* .alert-* .section-title .page-title`;
components `NavBar`, `ProjectSwitcher`, `IconButton`, `StatusBadge`, `HostLink`, `CopyButton`, `ConfirmDialog`,
`AppStatusNote`). Page shell is identical to `TemplatesView`/`AppsListView`:

```
<div class="min-h-screen bg-pico-bg"><NavBar /><main id="main" tabindex="-1" class="mx-auto max-w-6xl px-4 py-4 sm:px-6">
  <nav class="meta mb-2" aria-label="Breadcrumb">...</nav> ...
```

All Kaisen pages use `max-w-6xl` (consistent widths). Only `/kaisen/new` is a form page; it uses `max-w-2xl` like
`/apps/new`. Wireframe legend: `[ Button ]` = `.btn-*`, `[i]` = `IconButton`, `(Badge)` = `.badge-*`.

## 0. Shared pieces

**Preview badge**: `<span class="badge-info">Preview</span>`. Always `badge-info` (blue, same as the "primary" and add-on
badges), always the literal word "Preview", placed right after the title/label it qualifies. Never colour-only, never an icon-only marker.
New small component `PreviewBadge.vue` (3 lines) so the wording lives in one place.

**Build status** (extend `StatusBadge` `looks` map, same glyph set, label + glyph, never colour-only):

| status | label | class | glyph | tip |
|---|---|---|---|---|
| `queued` | Queued | badge-neutral | clock | Waiting for the builder. |
| `running` | Building | badge-warn | pulse | Kaisen is writing code. |
| `waiting` | Needs you | badge-info | dot | Kaisen asked a question. |
| `deploying` | Deploying | badge-warn | pulse | Starting the preview app. |
| `done` | Ready | badge-ok | dot | Last run finished. |
| `failed` | Failed | badge-error | cross | The last run stopped with an error. |
| `paused` | Paused | badge-neutral | square | Stopped at a spend cap. |

(`running` collides with app "Running"; key the map as `build:<status>` or pass a `kind="build"` prop. frontend-dev's call.)

**Currency**: spend shown as `$0.42`, `.mono`, never rounded below a cent (`<$0.01`).

---

## 1. Nav entry

Desktop (`NavBar` `<nav aria-label="Main">`): one more `.nav-link` after Apps, before Docs. Active on `/kaisen*`.

```
[PicoPaaS]  Apps   Kaisen (Preview)   Docs   | Admin Health          [Personal v][gear][+]  (o)  [->]
```

- `<RouterLink to="/kaisen" class="nav-link" active-class="nav-active">Kaisen <span class="badge-info ml-1.5">Preview</span></RouterLink>`
- Hidden entirely when `auth.user.features.kaisen === false` (see API assumptions). Admin disabled -> no link, and `/kaisen*` routes redirect to `/apps`.
- Not in the top-right cluster; no extra icon button (Chas: don't add top-bar buttons for things reachable elsewhere). Settings for Kaisen is reached from the Kaisen home page, not the navbar.

Mobile sheet (390px): same link in the `flex-col` list, between Apps and Docs; badge stays inline.

```
 Apps
 Kaisen  (Preview)
 Docs
```

A11y: link text is "Kaisen Preview" to screen readers (badge is real text). `aria-current="page"` comes from RouterLink exact/active class.

---

## 2. New app chooser (`/apps/new`)

Change is additive and sits above the current page content; the existing form, template link and Form/YAML tabs are untouched
(those are an in-form view switch that already exists, not new IA). The chooser is two **links-as-cards**, not tabs; the page title stays "New app".

Desktop (container stays `max-w-2xl`):

```
Apps / New app
New app
+------------------------------------+  +------------------------------------+
| Deploy an image                    |  | Build with Kaisen (Preview)        |
| Run a public Docker image you      |  | Describe an app and Kaisen writes  |
| already have. Live in a minute.    |  | and deploys it for you.            |
| (Selected)                         |  |                       [ Start ->]  |
+------------------------------------+  +------------------------------------+
Deploy a public Docker image. ... Browse templates
[ Form | YAML (advanced) ]
 ...existing form...
```

390px: cards stack (`grid grid-cols-1 gap-2 sm:grid-cols-2`), Kaisen card second.

Classes/components:
- Wrapper `<div class="mb-4 grid grid-cols-1 gap-2 sm:grid-cols-2" role="group" aria-label="How do you want to create this app?">`.
- Left card: `<div class="card card-pad border-accent-light" aria-current="true">` with `<h2 class="section-title">Deploy an image</h2>`, `<p class="mt-1 text-sm text-zinc-300">` and `<span class="badge-info">Selected</span>`.
- Right card: `<RouterLink to="/kaisen/new" class="card card-pad block hover:bg-pico-card">` (whole card is the link; focus ring from global `:focus-visible`), title `Build with Kaisen` + `<PreviewBadge/>`, text `text-sm text-zinc-300`, a trailing `.link` "Start" with an `ArrowRight` lucide icon (aria-hidden).
- If `features.kaisen === false`: right card not rendered; left card also hidden (no chooser with one option).
- If the current project has no Anthropic key, the card still works; the New build page shows the missing-key notice (section 4).

Copy: Left: "Deploy an image" / "Run a public Docker image you already have. Live in about a minute." Right: "Build with Kaisen" + Preview / "Describe an app in plain words. Kaisen writes it, runs it and deploys it here."

States: no loading state (static). Error: none. A11y: the two cards are in a labelled group; the selected one has `aria-current`; the link card's accessible name is its heading + text.

The empty-apps card on `/apps` ("Deploy your first app") gets one more button after "Try a sample app": `<RouterLink to="/kaisen/new" class="btn-secondary">Build with Kaisen (Preview)</RouterLink>`.

---

## 3. Kaisen home (`/kaisen`)

Header pattern copied from `AppsListView` (title + count on the left, `IconButton` Plus on the right). Project comes from the existing
switcher; title mirrors apps: "Personal builds" / "Builds in {project}".

Desktop:

```
Kaisen
Personal builds  (Preview)   3 builds                          [i:Settings] [i:+ New build]

+-------------------------------------------------------------------------------------------+
| todo-api  (Ready)                                                  [i:HeartPulse][i:Wand][i:Trash]|
| todo-api.picopaas.xyz [copy][open]                                                        |
| Last run 12 min ago . 4 files . $0.42                                                     |
+-------------------------------------------------------------------------------------------+
| recipe-box  (Needs you)  ...  "Kaisen has a question."                                    |
| ...                                                                                       |
+-------------------------------------------------------------------------------------------+
Kaisen is a Preview. Builds spend from this project's Anthropic key.            This month: $1.37 of $20
```

390px: identical single column; icon buttons stay right (as in the apps list). Spend footer wraps to two lines.

Classes/components:
- `<h1 class="page-title">` + `<PreviewBadge/>` + `<span class="meta">3 builds</span>` in `flex items-baseline gap-3`.
- Right actions: `IconButton :icon="Settings" label="Kaisen settings" to="/kaisen/settings"` and `IconButton :icon="Plus" label="New build" to="/kaisen/new"` (the Plus is hidden in the empty state, as on Apps).
- Rows: `<ul class="space-y-2"><li class="card card-pad">` same inner layout as an apps row: name `RouterLink` `font-medium text-white hover:underline truncate` to `/kaisen/:id`, `<StatusBadge kind="build">`, then `HostLink` if the build has an app, then `p.meta.mono` with "Last run 12 min ago · 4 files · $0.42" (reuse `relTime`).
- Row icon buttons: `HeartPulse` "View app health" (`/apps/:app_id`, only when app exists), `Pencil`/`Wand2` "Open build" (`/kaisen/:id`), `Trash2` danger "Delete build" -> `ConfirmDialog danger require-text="{name}"`, with the slot holding a `.check` "Also delete the app (todo-api)" checkbox (default off). Message: "This removes the build, its files and run history. Spend totals stay in your usage."
- A needs-you row shows `<p class="alert-info mt-2">Kaisen has a question. <RouterLink class="link">Answer it</RouterLink></p>`; a failed row shows `.alert-error` with `last_error` (one line, `truncate`).
- Footer line: `<p class="help mt-3">` with spend this month and cap, from `GET /kaisen/spend`.
- Poll every 4s while any build is `queued|running|deploying` (same `syncPolling` pattern as AppsListView).

Empty state (no builds), a `.card .card-pad` exactly like "Deploy your first app":

```
Build your first app with Kaisen  (Preview)
Tell Kaisen what you want in a few sentences. It writes the code, runs it and deploys it as an app
in this project. You can download the code or push it to GitHub.
[ New build ]  [ Read the guide ]
```

No-key variant (settings has no Anthropic key): replace the buttons with an `.alert-warn`:
"Kaisen needs an Anthropic API key for this project before it can build. [Add it in Kaisen settings]" (members: "Ask a project owner to add one.", no link). `New build` stays enabled so people can write the brief first? **No**: disable it (`aria-disabled`, tooltip "Add an Anthropic key in Kaisen settings first").

Loading: three skeleton `li.card.card-pad` rows exactly as AppsListView (`aria-busy="true" aria-label="Loading builds"`).
Error: `div[role=alert].alert-error` with Retry `IconButton` (`RotateCw`).
App-limit reached (project at app limit and no existing app to update): New build disabled with `meta` "App limit reached. Ask an administrator to raise it." Same as Apps.

A11y: the list is a `<ul>`; status never colour-only (StatusBadge glyph+label); icon buttons have labels; delete dialog focus starts in the typed-confirm input (existing behaviour).

---

## 4. New build (`/kaisen/new`)

Form page, `max-w-2xl`, patterned on `AppFormView` (new mode) including the project select.

```
Apps / Kaisen / New build           (breadcrumb: Kaisen -> /kaisen)
New build  (Preview)
Describe the app. Kaisen builds it, deploys it as an app in the project you pick, and asks if it needs to know something.

Name
[ todo-api                              ]
Lowercase letters, numbers and dashes. This becomes the app's name and address.

What should it do?
[ A tiny to-do list with a JSON API and a plain web page.   ]
[ Items have a title and done flag. No login needed.         ]
[                                                            ]
Examples: "A URL shortener with click counts" . "A markdown notes page that saves to a database"
  (each example is a button.btn-ghost.btn-sm that fills the textarea)

Project
[ Personal                                v ]
Kaisen deploys into this project and spends from its Anthropic key.

[ Start building ]  [ Cancel ]
```

390px: identical (single column). Buttons `flex flex-col gap-2 sm:flex-row`.

Classes/components:
- Fields: `.label`, `.input`, `textarea.input min-h-32`, `.help`, `.field-error` with `aria-invalid` and `aria-describedby`, exactly as AppFormView.
- Name validation reuses the app-name rules; show `field-error` "Use lowercase letters, numbers and dashes."; name taken: "You already have an app with that name."
- Example chips: `<button type="button" class="btn-ghost btn-sm">`, in a `flex flex-wrap gap-1` row labelled by a `help`.
- Project select: `select.input` like the app form, defaulting to `projects.current`; hidden when the user has only the personal project (then the Project line becomes a `.help`: "Deploys to your personal apps.").
- Primary `btn-primary` "Start building"; busy -> disabled with "Starting…". Success redirects to `/kaisen/:id`.
- No-key notice at top (`.alert-warn`, as in section 3) and the submit disabled; at-limit notice likewise.

Error states: server 403/422 messages shown in `div[role=alert].alert-error` above the buttons (plain-language, per section 5 of the design). Loading: not needed (projects store already loaded).
A11y: labels bound to inputs, `autocomplete="off"`, textarea has a character counter `.help` ("0 / 4000", `aria-live="polite"` only on over-limit).

---

## 5. Build page (`/kaisen/:id`)

The densest screen. Rule: **no tabs.** One scrolling column of `.card` sections, same as AppDetailView. The app's own pages are reached by buttons.

Desktop:

```
Apps / Kaisen / todo-api                                       (breadcrumb: Kaisen -> /kaisen)
todo-api  (Ready) (Preview)                                    [i:Download] [i:Github Push] [i:Settings] [i:Trash]
Started 2 days ago by you . $0.42 spent on this build

+- Live app -------------------------------------------------------------------------------+
| todo-api.picopaas.xyz [copy][open]   (Running)                                           |
| [ App health ]  [ App settings ]                                                         |
+------------------------------------------------------------------------------------------+
+- Needs your answer (1) --------- (alert-info) -------------------------------------------+
| Kaisen asks: "Should items be deletable, or only marked done?"                           |
| [ Answer...........................................................] [ Send answer ]   |
+------------------------------------------------------------------------------------------+
+- Tell Kaisen what to change -------------------------------------------------------------+
| [ Make the done items grey and add a delete button.                                    ] |
| [                                                                                      ] |
| Kaisen edits the code and redeploys. About a minute.                    [ Send to Kaisen ]|
+------------------------------------------------------------------------------------------+
+- Runs -----------------------------------------------------------------------------------+
| #3 feedback (Ready)  12 min ago . 48 s . $0.11                         [v Transcript]    |
|   Summary: Added delete button and grey styling.                                         |
|   > transcript (collapsed)                                                               |
| #2 feedback (Ready) ...                                                                  |
| #1 build (Ready) ...                                                                     |
+------------------------------------------------------------------------------------------+
+- Files (4) ------------------------------------------------------------------------------+
| app.py  | main.py   1.2 KB   <- list (left, 1/3)   | viewer (right, 2/3): <pre> code     |
| kaisen.json                                           |                                    |
+------------------------------------------------------------------------------------------+
+- Output ---------------------------------------------------------------------------------+
| [ Download .tar.gz ]  [ Push to GitHub ]   Pushes to acme/todo on branch kaisen/todo-api |
+------------------------------------------------------------------------------------------+
+- Spend ----------------------------------------------------------------------------------+
| This build $0.42 of $5 cap      [=====-------]                                            |
| This month, project $1.37 of $20                                                         |
+------------------------------------------------------------------------------------------+
```

390px: Files card stacks (list above viewer; list is a `<ul>` of full-width buttons, viewer below with `overflow-x-auto` inside the `<pre>` only, never the page). Header icons wrap beneath the title (`flex-wrap`). The Output buttons become `flex-col` full width. Live app card's two buttons stack.

Section order (top to bottom): Live app, Needs your answer (only when open), Tell Kaisen, Runs, Files, Output, Spend. A running build moves nothing: the question card is inserted above "Tell Kaisen" with `role="status"`.

### 5.1 Header
- Breadcrumb: `Apps` is replaced by `Kaisen`: `<RouterLink to="/kaisen" class="link">Kaisen</RouterLink> / {name}`.
- `h1.page-title.break-all` name, `StatusBadge kind=build`, `PreviewBadge`.
- `p.meta`: "Started 2 days ago by you · $0.42 spent". (`relTime`, creator email shortened to "you" when it is the viewer.)
- Right icon cluster (`IconButton`): `Download` "Download code (.tar.gz)", `Github` "Push to GitHub" (disabled with tooltip when GitHub is not configured: "Set up GitHub in Kaisen settings"), `Settings` "Kaisen settings" -> `/kaisen/settings`, `Trash2` danger "Delete build". Download/Push duplicate the Output card on purpose (quick access vs. explained version); **if Chas prefers fewer, drop the header pair and keep the Output card only.** Open question Q1 below.

### 5.2 Live app card
- `section.card.card-pad[aria-labelledby]`, `h2.section-title` "Live app".
- Content: `HostLink :hostname`, `StatusBadge :status="app.status"` (the app's own status), `AppStatusNote compact` for queued/deploying/error as in AppsListView.
- Buttons (`flex flex-col gap-2 sm:flex-row`): `<RouterLink :to="/apps/:id" class="btn-secondary">App health</RouterLink>` and `<RouterLink :to="/apps/:id/edit" class="btn-secondary">App settings</RouterLink>`. These are the "pages linked by buttons".
- Empty (no app yet): `p.text-sm.text-zinc-300` "Not deployed yet. Kaisen deploys the app when the first build finishes." Deleted app: `.alert-warn` "The app was deleted. [ Deploy again ]" (POST .../deploy).

### 5.3 Needs your answer
- Rendered once per open inbox item with `kind=question`: `section.alert-info` (or `.card` with `border-accent/40`) containing the question text (`whitespace-pre-wrap`, plain text, never HTML), a `textarea.input` (2 rows, `aria-label="Your answer"`) and `btn-primary` "Send answer". After send: item collapses to "Answered. Kaisen is continuing." and the build status goes to Building.
- Other inbox kinds: `budget` -> `.alert-warn` "Kaisen stopped at the spend cap ($5). Raise the cap in Kaisen settings or [ Resume ]"; `refusal`/`error` -> `.alert-error` with the message; `push`/`deploy` -> `.alert-success` or `.alert-error` one-liners. Dismissible with `IconButton X sm` like the app alerts.

### 5.4 Tell Kaisen what to change
- `section.card.card-pad`, `h2.section-title` "Tell Kaisen what to change", `textarea.input` (3 rows), `p.help` "Kaisen edits the code and redeploys. About a minute." and `btn-primary` "Send to Kaisen" right-aligned (`flex justify-end`).
- Disabled (not hidden) with `.help` reason while a run is active: "Kaisen is working. You can send more once this run finishes." Disabled when the project has no key or the user is a viewer.
- First-run state (no runs yet and status queued): card replaced by `.help` "Your first build is queued." (not an empty box).

### 5.5 Runs
- `section.card`, `h2.section-title` "Runs (3)". Each run is a `<details>` (native, keyboard accessible, matches the repo's `summary` cursor rule), newest first, newest open if it is running or failed.
- `<summary>` row: `#3`, kind label (Build / Change / Resume), `StatusBadge`, `meta` "12 min ago · 48 s · $0.11", all in one wrapping flex row. Chevron via lucide `ChevronRight` rotating on `[open]` (decorative).
- Body: run `summary` text (`text-sm text-zinc-200`), `error` in `.alert-error` if failed, then the **transcript** as a nested `<details>`: summary "Transcript (redacted)" with `.help` "Secrets and tokens are removed before it is shown."; content `<pre class="mono max-h-96 overflow-auto whitespace-pre-wrap break-words rounded-md bg-pico-bg p-3 text-zinc-200" tabindex="0" aria-label="Transcript of run 3">` (same look as the log box in HealthLogs). Turns are prefixed `you:` / `kaisen:` / `tool:`; render as text only. `CopyButton sm` on the transcript.
- Live run: a pulse line "Kaisen is working… step 7" (`motion-safe:animate-pulse` text, `aria-live="polite"`), polled every 3s (same as AppDetailView poll).
- Empty: "No runs yet." Old transcript trimmed (>14 days): `meta` "The transcript was removed after 14 days. The summary is kept."

### 5.6 Files
- `section.card.card-pad`, `h2.section-title` "Files (4)". Layout `grid gap-3 md:grid-cols-3`: left `ul` (md:col-span-1) of `button.btn-ghost.btn-sm.w-full.justify-start.mono` with `aria-pressed` for the selected file, plus `meta` size; right (md:col-span-2) viewer: header row with the path (`mono`, `break-all`), `CopyButton sm`, then `<pre class="mono max-h-96 overflow-auto ...">` same as transcript. Files fetched on select (`GET .../files/{path}`), skeleton line while loading, `.alert-error` + Retry on failure; binary or >200 KB: "This file is too large to preview. Download the code to see it."
- Empty: "No files yet. They appear as soon as Kaisen writes them."
- The viewer is read-only. No editing in the preview.

### 5.7 Output
- `section.card.card-pad`, `h2.section-title` "Output". Buttons: `btn-secondary` "Download .tar.gz" (link with `download` to the API URL) and `btn-secondary` "Push to GitHub".
- Under the buttons, one `.help` line: configured -> "Pushes to {owner/repo} on branch {branch}. Opens a pull request." ; not configured -> "GitHub is optional. [Set it up in Kaisen settings]".
- After a push: `.alert-success` "Pushed. [View on GitHub]" (external link with the "opens in new tab" sr-only text, as in HostLink).
- Push disabled for read-only users? No: members can push (they can run builds). Only a read-scope API key is blocked; show the platform's error text.

### 5.8 Spend
- `section.card.card-pad`, `h2.section-title` "Spend". Use the existing `UsageBar` component (label "This build", usage `{used, limit}` = cost vs `cap_build`) and a second `UsageBar` "This month, project". If caps are 0 (off): plain `meta` "No cap set".

### 5.9 States
- Loading: header + one tall skeleton, same as AppDetailView (`aria-busy`).
- 404: reuse `AppNotFound` markup via a tiny `BuildNotFound` ("This build doesn't exist or was deleted." + link to Kaisen).
- Error: `.alert-error` + Retry.
- Kaisen disabled by admin mid-session: `.alert-warn` "Kaisen is turned off on this instance." read-only view of files/Download only.
- Delete build: `ConfirmDialog danger require-text=name`, slot checkbox "Also delete the app".

### 5.10 A11y
Each section has `aria-labelledby`; status changes (Building -> Ready, new question) are announced through one visually hidden `role="status"` region at the top of `main`; textareas are labelled; `<details>`/`<summary>` give keyboard toggle for free; all icon buttons labelled; transcript/code `<pre>` are focusable (`tabindex=0`) so keyboard users can scroll; no horizontal page scroll (only the `<pre>` scrolls).

---

## 6. Kaisen settings (`/kaisen/settings`)

For the current project (switcher applies). Owners edit; members get the same page read-only. `max-w-6xl` container, but the form column is `max-w-2xl` inside it so it reads like the app settings (`AppFormView` edit mode lays out `card`s; do the same: one `.card.card-pad` per group).

```
Kaisen / Settings
Kaisen settings  (Preview)                                          Project: Personal
These settings belong to the project "Personal". Everyone who can run builds here spends from this key.

+- Anthropic ------------------------------------------------------------------------------+
| API key           ends in ...abcd                                                        |
| [ ****************                  ]  [ ] Clear stored key                               |
|   Leave blank to keep the saved key.                                                     |
| Model [ claude-sonnet-5-5 v ]   Effort [ medium v ]                                     |
| > How to get an Anthropic API key                                                        |
+------------------------------------------------------------------------------------------+
+- Spend limits (USD) ---------------------------------------------------------------------+
| Per run [ 2.00 ]  Per day [ 10.00 ]  Per build [ 5.00 ]       This month: $1.37          |
+------------------------------------------------------------------------------------------+
+- Output ---------------------------------------------------------------------------------+
| You can always download a build as a .tar.gz. Pushing to GitHub is optional.             |
| (o) Download only   ( ) Download and push to GitHub                                      |
|   Repository URL [ https://github.com/you/repo ]  Branch [ kaisen/<build> ]              |
|   Access token ends in ...wxyz [ ******* ]  [ ] Clear stored token                       |
|   > How to create the access token (about 2 minutes)   <- details, open if none set      |
|   [x] Push automatically when a build finishes   [x] Open a pull request after pushing   |
+------------------------------------------------------------------------------------------+
[ Save settings ]                                       [ Test Anthropic ] [ Test GitHub ]
```

390px: Model/Effort and the three caps stack to one column (`grid gap-3 sm:grid-cols-2`, caps `sm:grid-cols-3`); the button row becomes `flex-col` with Save first.

Classes/components:
- Cards `.card.card-pad`, titles `h2.section-title`, fields `.label`/`.input`/`.help`/`.check` (checkboxes use `.check` with an inline `label.text-sm.text-zinc-200`, as elsewhere), selects `select.input` (with the built-in chevron), numbers `input.input type=number step=0.01 min=0` (spinners are already hidden globally).
- Secret fields: reuse `PasswordField` (type=password, `autocomplete="off"`, placeholder "Leave blank to keep"). Hint `help`: "Saved, ends in ...abcd" or "Not set". Never show the value. "Clear stored key" is a `.check`.
- Output choice: radio group in a `<fieldset><legend class="label">`; GitHub fields are shown only when "push" is chosen (`v-if`, with the radio's `aria-controls`).
- Token instructions: native `<details class="help">`, `summary` "How to create the access token (about 2 minutes)", open when no token is stored. **Copy reused verbatim** from `/Users/cberndt/source/kaisen/service/kaisen/templates/settings.html` (the 8-step `<ol>` and the "Why only these permissions" paragraph), with these edits: "Kaisen writes to its own branch" -> keep; the link opens in a new tab with `rel="noopener noreferrer"`; styling `list-decimal pl-5 space-y-1 text-sm text-zinc-300`, code via `<code class="mono">`. Add the equivalent Anthropic `<details>`: "1. Open console.anthropic.com > API keys. 2. Create a key named picopaas-kaisen. 3. Paste it above. Set a spend limit on the Anthropic side too. 4. Save, then press Test Anthropic."
- Removed vs. the standalone page: all PicoPaaS URL/API key/project/public URL fields and Pause-all (not needed: seamless deploy). Do **not** carry them over.
- Test buttons: `btn-secondary` "Test Anthropic", "Test GitHub" (GitHub only when output is push). They test the **saved** settings; if the form is dirty the buttons are disabled with `.help` "Save first, then test." Result inline next to the buttons as `.alert-success` ("Anthropic works. Model claude-sonnet-5-5 answered.") or `.alert-error` (plain reason, e.g. "GitHub says the token cannot write to that repository.") in a `role="status"` region; also a toast on success (existing `useToastStore`).
- Save: `btn-primary` "Save settings", busy -> "Saving…", success toast "Kaisen settings saved." Validation: `field-error` (e.g. "Enter a GitHub repository URL.").
- **Member read-only**: banner `.alert-info` "Only project owners can change these settings. You can see what is set." All inputs `disabled` (styled dashed by `.input:disabled`), secrets show only "Set / Not set", Save and Test GitHub hidden, **Test Anthropic stays** (it is harmless) -> decide Q3. Caps/model/effort values visible.
- "Spend this month" is a `meta` line from `GET /kaisen/spend?project_id=`.

States: loading = two skeleton cards; error = `.alert-error` + Retry; 403 on PUT -> `.alert-error` with server text.
A11y: every field labelled; secret fields described by their hint via `aria-describedby`; the read-only state announced by the banner (`role="status"`) and by `disabled`; `details` keyboard native; tab order = visual order.

---

## 7. Badges on built apps

**Apps list** (`AppsListView` row): after the name and `StatusBadge`, add `<RouterLink :to="/kaisen/:id" class="badge-info">Built with Kaisen · Preview</RouterLink>` when `app.kaisen_build_id`. On 390px the long text would crowd the row, so below `sm` show just "Kaisen" (`<span class="sm:hidden">Kaisen</span><span class="hidden sm:inline">Built with Kaisen · Preview</span>`), with the full text in `title` and sr-only. Row action icons unchanged; the badge is a link, so the extra `Wand2` "Open in Kaisen" icon is **not** added to rows (avoid more icons).

```
todo-api (Running) (Built with Kaisen . Preview)                   [health][restart][edit][delete]
todo-api.picopaas.xyz
```

**App health** (`AppDetailView` header, `flex flex-wrap items-center gap-2` row next to the status badge): same badge (non-link), and in the header icon cluster one new `IconButton :icon="Wand2" label="Open in Kaisen" :to="/kaisen/:id"` placed first. The existing cluster is Check image / Restart / Settings, so this is a 4th icon only on built apps (acceptable: it is a real per-app navigation, not a duplicate).
Settings page (`/apps/:id/edit`): a `.alert-info` at the top of the form for built apps: "This app is built by Kaisen. Changes you make here can be overwritten the next time Kaisen deploys. [Open in Kaisen]". (Kaisen redeploys its own spec; image/env edits would be reset.) Delete in the Danger zone adds: "The build and its code stay in Kaisen." (backend unlinks the build).

---

## 8. Files, routes, stores (for frontend-dev)

New routes (all `requiresAuth`, redirect to `/apps` if `features.kaisen === false`), lazy-loaded in `router/index.ts`:

| path | view | title |
|---|---|---|
| `/kaisen` | `KaisenHomeView.vue` | Kaisen |
| `/kaisen/new` | `KaisenNewView.vue` | New build |
| `/kaisen/settings` | `KaisenSettingsView.vue` | Kaisen settings |
| `/kaisen/:id(\\d+)` | `KaisenBuildView.vue` (props id) | Build |

Register `/kaisen/settings` and `/kaisen/new` before `/kaisen/:id(\\d+)`; the `\\d+` constraint already prevents clashes.

New files:
- `frontend/src/api/kaisen.ts` (typed wrappers over `api/client`), types in `frontend/src/types.ts` or `types/kaisen.ts`.
- `frontend/src/stores/kaisen.ts` (Pinia): `builds`, `loading`, `load(projectId)`, `fetchOne`, `create`, `remove`, `upsert`; `settings` for the current project; `spend`. Re-load on `projects.currentId` change (copy the `watch` in AppsListView).
- Components: `PreviewBadge.vue`, `KaisenChooser.vue` (the two cards, used in AppFormView), `BuildStatusBadge.vue` (or `kind` prop on StatusBadge), `BuildRuns.vue` (runs + transcripts), `BuildFiles.vue` (list + viewer), `BuildInbox.vue` (questions/notices), `KaisenKeyHelp.vue` (the two `details` instructions blocks, plain text so tech-writer can reuse them in the User Guide).
- Edits to existing files (frontend-dev, not this task): `NavBar.vue` (link + mobile), `AppFormView.vue` (chooser, new mode only), `AppsListView.vue` (badge, empty-state button), `AppDetailView.vue` (badge + Wand2 icon), `AppFormView.vue` edit mode (info alert), `StatusBadge.vue` (build statuses), `types.ts` (`App.kaisen_build_id`, `features.kaisen`).
- Polling: reuse the interval pattern from AppsListView (4s list) and AppDetailView (3s build page while `queued|running|deploying|waiting`); stop on unmount.
- Icons (all already in lucide-vue-next): `Wand2`, `Github`, `Download`, `Settings`, `Plus`, `Trash2`, `HeartPulse`, `ChevronRight`, `ArrowRight`, `RotateCw`, `X`.

## 9. API fields each screen needs (assumptions, to confirm with backend-dev)

Based on section 5 of the design; nothing below exists yet. Shapes are my assumptions:

- **Features/me**: `auth.user.features.kaisen: boolean` (admin setting + env). Without it the nav entry cannot be hidden.
- **App**: `kaisen_build_id: number | null` on `GET /apps` and `GET /apps/{id}`.
- **Build list item** (`GET /kaisen/builds?project_id=`): `id, name, status, app_id|null, app_hostname|null, app_status|null, file_count, cost_usd, last_run_at, last_error|null, open_questions: number, created_by (id, email), created_at`.
- **Build detail** (`GET /kaisen/builds/{id}`): the above plus `brief, project_id, slug, files: [{path, size}], runs: [{id, kind, status, summary, error, cost_usd, started_at, finished_at, transcript: [{role, text}]|null, transcript_trimmed: bool}]` (all redacted server-side), `inbox: [{id, kind, text, status, answer, created_at}]`, `spend: {build_usd, cap_build, month_usd}`, `output: {mode: "download"|"github", repo, branch, can_push: bool}`, `can_run: bool` (false for read-scope key / viewer).
- **Files**: `GET .../files/{path}` -> `{path, size, content, binary: bool, truncated: bool}`; `download` returns a tar.gz stream.
- **Actions**: `POST /kaisen/builds {project_id, name, brief}` -> build; `run`, `feedback {text}`, `inbox/{id}/answer {text}`, `deploy`, `push` return the updated build (so the page can `upsert` without a second fetch). `DELETE ?delete_app=` -> 204.
- **Settings** (`GET /projects/{id}/kaisen`): `anthropic_key: {set: bool, last4}`, `model, effort, models: string[], efforts: string[]`, `cap_run, cap_day, cap_build`, `output_mode, github_repo, github_branch, github_token: {set, last4}, push_on_finish, open_pr`, `can_edit: bool` (owner), `updated_at`. PUT takes the same plus `clear_anthropic_key`, `clear_github_token` (secrets omitted = keep). Test: `POST .../test/{anthropic|github}` -> `{ok: bool, message: string}`.
- **Spend** (`GET /kaisen/spend?project_id=`): `{month_usd, cap_day, today_usd}`.
- Statuses: build `queued|running|waiting|deploying|done|failed|paused` (my mapping of `kaisen_builds.status`; backend to confirm the enum or tell me the real values).
- Errors in the platform's `{detail}` plain-language style, so `ApiError.message` can be shown as-is.

## 10. Open questions for Chas

**Decided 2026-10-29 (manager, all as recommended; Chas can overrule):** 1 Output card only; 2 long form desktop / short on phones; 3 yes; 4 disabled button + link to Kaisen settings; 5 "Per build".

1. Download/Push in both the build header and the Output card, or the Output card only? (Recommend card only; one place, fewer icons.)
2. "Built with Kaisen · Preview" on every built app row vs. just "Kaisen" everywhere. (Recommend long form on desktop, short on phones, as drawn.)
3. Members on the read-only settings page: allow "Test Anthropic"? (Recommend yes, it only reads.)
4. New build on a project with no Anthropic key: disabled button (recommended) or allow writing the brief and queue it as "Paused"?
5. Per-build cap is called "Per project" in the standalone UI; here "Per build" (the design's `cap_build`) to avoid clashing with PicoPaaS projects. OK?

## 11. Not covered / next step

No static HTML mockup was produced; the ASCII wireframes plus existing classes are the contract. After Chas approves, hand to `frontend-dev` with sections 8 and 9 and re-check in a real browser at desktop and 390px (ui-ux-designer review) once built.
