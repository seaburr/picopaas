# Kaisen belongs to a project: UX rethink

> **Decided 2026-11-07.** Option A was built: Kaisen sits inside each project in the left tree, after Apps. Builds stay at `/kaisen/:id`. Apps moved to project URLs (`/projects/:pid/apps`, `/projects/:pid/apps/new`) in the same round; the old `/apps`, `/kaisen` addresses redirect to the current project's page. The text below is the original proposal.

Status: proposal, 2026-11-07. Design only; nothing built. Author: ui-ux-designer. Trigger (Chas, after testing the redesign):
"Kaisen settings show the last project you clicked on but it's not at all clear how to work in a project or switch from the
Kaisen settings. ... Kaisen stuff needs to fall under the projects somehow."

Mockups: Design canvas "PicoPaaS UI redesign", new row "Kaisen inside a project" (https://claude.ai/artifact/CH5AW3c7tHu3rKrDqAnmUw).

## 1. What is true today (verified in code)

- Data: Kaisen settings (`GET/PUT /projects/{id}/kaisen`, members' limits, uploads) and builds (`kaisen_builds.project_id`) belong
  to a project. Builds deploy apps into their project and spend from that project's Anthropic key. Owners edit settings;
  members see what is set and can create and run builds (`docs/design/kaisen.md` section 2).
- Frontend: `stores/projects.ts` keeps `currentId` (localStorage `picopaas.project`, set whenever you click a project in the
  left tree; falls back to the personal project). `KaisenHomeView`, `KaisenSettingsView`, `KaisenNewView` and the Kaisen
  store all read `projects.current` and reload when it changes. `KaisenBuildView` is the exception: it uses the build's own
  `project_id`.
- Left tree (`AppSidebar.vue`): `Projects > project > apps`, plus a sibling top-level `Kaisen Preview > Builds, Settings`
  (only when `auth.user.features.kaisen`). Clicking a project name goes to `/apps` and sets `currentId`.
- Routes: `/kaisen`, `/kaisen/new`, `/kaisen/settings`, `/kaisen/:id`. The route guard hides all `/kaisen*` without the flag.
- API already supports everything needed: `GET /kaisen/builds?project_id=`, `GET /projects/{id}/kaisen`, build detail carries
  `project_id`. No backend change is required.

## 2. Problems

1. **The project is invisible state.** The only hint on Kaisen settings is a small grey "Project: Team sandbox" next to the
   title. It is the project you last clicked in a different part of the sidebar, which may have been ten minutes and three pages ago.
   Nothing says "you are working in this project" and nothing on the page lets you change it.
2. **Two trees for one hierarchy.** Kaisen settings are a project's settings, but they live in a top-level section far from
   the project. The user has to know that a click in the Projects branch silently re-targets the Kaisen branch.
3. **Switching is a side effect, not an action.** To switch you must leave Kaisen, expand Projects, click a project (which
   lands on its Apps list), then click Kaisen > Settings again. The tree does not mark the current project while you are on
   a Kaisen page (the highlight only shows on `/apps`), so you cannot even confirm the switch worked. The New build page
   says "Change the project in the sidebar", which is the only hint and points at a control that does not look like a switcher.
4. **Build pages disagree with list pages.** A build opened from a link belongs to project A while the sidebar and the
   Kaisen list may say project B. The build page uses the build's project; its Settings link goes to the current project's settings.
   Deep links therefore show Kaisen settings for the wrong project. The same hole exists in `BuildInbox`, `BuildOutputFields` (links to
   `/kaisen/settings`) and `KaisenChooser`.
5. **Permissions are unexplained.** A member of a team project sees read-only settings and a disabled New build with "Add an
   Anthropic key in Kaisen settings first", but cannot add one and is not told who can.
6. **Bookmarks and shared links can't carry the project.** `/kaisen/settings` means a different thing for each person and each session.

Root cause: the URL does not say which project, so the app invents one. The fix is to put the project in the URL and the tree,
and show it at the top of every Kaisen page.

## 3. Options

### A. Kaisen inside each project (project-scoped routes) -- recommended

The tree becomes `Projects > project > [Kaisen Preview > Builds, Settings] + apps`. Pages live at
`/projects/:pid/kaisen`, `/projects/:pid/kaisen/new`, `/projects/:pid/kaisen/settings`. A build keeps `/kaisen/:id`
(ids are global and the API is already keyed that way) and shows its project in the breadcrumb. The top-level Kaisen section
is removed. Every Kaisen page starts with a breadcrumb `Project / Kaisen / ...` where the project is a link.

- Pro: the hierarchy in the tree matches the data model; the project is both in the URL and in the tree path, so it can never
  be wrong; links are shareable and bookmarkable; switching = clicking another project's Kaisen row.
- Pro: no new concepts, no pickers; uses the tree users already have.
- Con: more rows in the tree (one collapsed row per project; two when open). Deeper nesting on phones (4 levels).
- Con: one-time redirect shim for old URLs.

### B. Keep top-level Kaisen, add an explicit project bar

Keep `/kaisen*`. Add a "Project: Team sandbox  [Switch]" row at the top of each Kaisen page (a select or menu).

- Pro: smallest change.
- Con: still two places for one hierarchy; still state in localStorage; the switch is a picker, which the design rules avoid;
  a second control to maintain; deep links still ambiguous. It treats the symptom.

### C. Fold Kaisen into the project's own page

Builds list and Kaisen settings become sections of `/projects/:id` (the project Settings page: members, limits, rename).

- Pro: one page per project.
- Con: the project page is a settings page for owners; members would reach builds through it, and builds are a daily
  workflow, not a setting. It makes the busiest Kaisen page the deepest. It also fights the approved IA (two pages per
  thing linked by buttons, no tabs) by piling a third subject onto Project.

## 4. Recommendation: Option A, plus a visible project everywhere

Five lines:
1. Kaisen moves into the Projects branch: `project > Kaisen Preview > Builds | Settings`, shown only with the feature flag.
2. URLs carry the project: `/projects/:pid/kaisen`, `/new`, `/settings`; `/kaisen/:id` stays and shows its project in the breadcrumb.
3. The tree marks the project you are in (bold name, and its Kaisen branch open) on every page that belongs to it, apps included.
4. Every Kaisen page opens with a breadcrumb and a title that names the project ("Kaisen builds in Team sandbox"); no picker.
5. Old `/kaisen*` URLs redirect to the current project's page; the router, not localStorage, is then the source of truth.

### 4.1 Tree

```
Projects
  v Team sandbox  2
      v Kaisen  [Preview]          <- collapsed row by default, open on Kaisen routes
          Builds  4
          Settings
        grafana
        n8n
  > Personal  6
  > Demo  3
Templates
Docs
```

- Kaisen row sits first inside a project, above the apps. Collapsed it costs one line; apps are not pushed down much and a
  long app list does not bury it.
- The count beside Builds is the number of builds (monospace, as for apps); no count is shown until that project's builds are loaded.
- The project name row is `cur` (bold) whenever the route belongs to that project: `/apps` while it is the selected
  project, `/apps/:id`, `/projects/:pid/kaisen*`, `/kaisen/:id` of one of its builds.
- Clicking a project name still goes to its Apps list. Clicking `Kaisen` goes to Builds; the triangle toggles.
- Without the flag there is no Kaisen row, and `/projects/:pid/kaisen*` redirects to `/apps` (as today for `/kaisen*`).

### 4.2 Project-scoped builds page

Breadcrumb `Team sandbox / Kaisen`. Title `Kaisen` + Preview badge + summary (`5 builds, $0.74 this month`). Page-top actions:
`Settings` (outlined text button, same project) and `New build` (primary text button). Table as in the approved Kaisen board.
For users in several projects a quiet line under the title lists the others as text links with build counts:
`Other projects: Personal 2 · Demo 0`. It doubles as the switcher on phones and for anyone who does not use the tree.

Where "New build" lives: here (page top, as now), and in the New app chooser ("Build with Kaisen"), which links to the
project of the Apps page it was opened from. The New build page title is `New build in Team sandbox`; the sentence
"Change the project in the sidebar" is deleted. There is no project field: to build elsewhere, go to that project's Kaisen.

### 4.3 Project-scoped settings

Breadcrumb `Team sandbox / Kaisen / Settings`; title `Kaisen settings`; the first line under it states scope and role in
plain words: `Applies to every build in Team sandbox. You are an owner.` / `... You are a member: you can see what is set, not change it.`
Sections unchanged (Anthropic key, model, effort, caps, members' limits, web access, GitHub token, uploads). The member state is
the read-only form with the existing info banner extended with the cost of waiting: `Ask an owner of Team sandbox to add a key.`

### 4.4 Empty project with no key

Builds page replaces the empty table with one block (text + one action, no illustration):

- Owner: `Kaisen needs an Anthropic API key in Team sandbox before it can build.` + `Open Kaisen settings` (primary).
- Member: `Kaisen isn't set up in Team sandbox yet. Ask an owner to add an Anthropic API key.` + disabled `New build` with the reason as its label (as today).
- Platform admin who is not a member: unchanged rules; they see only projects they belong to in the tree.

The tree needs no extra marker (one request per project to detect "no key" is not worth it); the page says it at the point of need.

### 4.5 Members vs owners

Same pages, same URLs. Tree rows are identical for both (a member can still open Settings to read what applies). Owners get the
editable form. Role is stated at the top of Settings (4.3). Build creation, running and the Output card stay member-level.

### 4.6 Feature flag

Unchanged semantics: no flag = no Kaisen row, `/projects/:pid/kaisen*` and `/kaisen*` redirect to `/apps`. The flag is per user, so
two members of one project can see different trees; nothing else changes. The project page (`/projects/:id`) may show a
`Kaisen` text link next to its other actions for flagged users (optional).

### 4.7 Deep links and several projects

- `/projects/:pid/kaisen*` are canonical. A user who is not a member of `:pid` gets the same 404 page as an unknown project today.
- `/kaisen` -> `/projects/{currentOrPersonalId}/kaisen`; `/kaisen/new` -> `.../new`; `/kaisen/settings` -> `.../settings`.
  This is the only place `projects.current` is still consulted, as a best guess for old bookmarks and in-app links not yet
  updated. The landing page names the project in its breadcrumb and title, so a wrong guess is visible at once.
- `/kaisen/:id` is unchanged (a build knows its project). The build page breadcrumb is `Project / Kaisen / build name`; its Settings
  and "Add a GitHub token" links use the build's project.
- On entering any project-scoped route the router calls `projects.select(pid)`, so Apps and the New app chooser follow the
  project you were last working in (this replaces the old accident with a rule: the project you are looking at is the current one).
- Visiting `/apps` itself still uses the stored project; that page has the same invisible-state weakness. Follow-up
  (not in this change): give it a project breadcrumb the same way and consider `/projects/:pid/apps`.

### 4.8 Phones

The menu already renders the same tree full screen. New rows are 44px tall; Kaisen is collapsed by default, so the menu does not
grow for people who do not use it. The page-top breadcrumb carries the project at 390px (wraps, no truncation) and
`Other projects:` is a wrapping row of 44px-high text links. Page-top actions wrap under the title as on other pages.

## 5. Implementation outline (frontend only)

Files:
- `frontend/src/router/index.ts`: add `/projects/:pid(\\d+)/kaisen`, `.../kaisen/new`, `.../kaisen/settings` (same three components,
  `props: true`). Replace `/kaisen`, `/kaisen/new`, `/kaisen/settings` by redirect functions that await `projects.load()` and
  return the project-scoped path (`projects.current?.id`, else personal, else `/apps`). Keep `/kaisen/:id(\\d+)`. Extend the flag
  guard to `to.path.startsWith('/kaisen') || /^\/projects\/\d+\/kaisen/.test(to.path)`. Add `beforeEach`/`beforeEnter`: unknown project or
  non-member -> existing not-found behaviour; otherwise `projects.select(pid)`.
- `frontend/src/views/KaisenHomeView.vue`, `KaisenSettingsView.vue`, `KaisenNewView.vue`: take `pid` from the route (prop), not
  `projects.current`; remove the `watch(projects.currentId)` reloads (route param changes reload instead, key the view on `pid`);
  add the breadcrumb, project-named titles, role sentence, "Other projects" line and the empty-state block. Remove "Change the
  project in the sidebar".
- `frontend/src/stores/kaisen.ts`: `load(pid)`, `refreshBuilds(pid)` take the project id as an argument.
- `frontend/src/views/KaisenBuildView.vue`: breadcrumb uses `build.project_id` and project name from the projects store; Settings links
  use it.
- `frontend/src/components/AppSidebar.vue`: remove the top-level Kaisen `<li>`; inside each project's `<ul>` add the Kaisen node (flag only,
  node key `k:<pid>`, collapsed unless the path is that project's Kaisen route; `reveal()` adds `p:<pid>` and `k:<pid>`); mark the project row
  `cur` for any route belonging to it (apps, app pages, project Kaisen routes, `/kaisen/:id` via the build's project: look it up in
  the kaisen store or fetch once like `resolveAppProject`). Builds count is optional and may come from `listBuilds` on open.
- `frontend/src/components/BuildInbox.vue`, `BuildOutputFields.vue`, `KaisenChooser.vue`: links become `/projects/${pid}/kaisen/...`
  (BuildInbox and BuildOutputFields already have the build; KaisenChooser gets the current project id).
- Docs: `docs/USER_GUIDE.md` Kaisen section and the left-tree description; `docs/design/kaisen-ui.md` route table.

Tests:
- Playwright (`frontend/e2e`): tree shows Kaisen under a project and not at top level; flag off hides it; `/kaisen`, `/kaisen/new`,
  `/kaisen/settings` redirect to the project-scoped URL; opening Settings of project B while A was last clicked shows B's name in
  the breadcrumb and loads B's settings (assert the request path `/projects/B/kaisen`); member sees read-only settings and the
  "ask an owner" sentence; empty project shows the setup block with `Open Kaisen settings` for an owner only; phone: menu shows the
  Kaisen row and the breadcrumb wraps without horizontal scroll. Update `smoke.spec.ts` (`page.goto('/kaisen')`) and the shell-tree specs.
- Vitest, if present: router redirect function (current project present / absent / no projects).

No backend change. Optional later: a `member count`/`has_key` field on the project list so the tree could mark "no key"
(not recommended now).

## 6. Questions only Chas can answer

1. Kaisen row first inside a project (recommended) or after the apps? (First keeps it findable with many apps; after keeps apps at the same place.)
2. Keep `/kaisen/:id` for builds, or move to `/projects/:pid/kaisen/:id`? Recommended: keep; ids are global and old links keep working.
3. Should `/apps` get the same project-in-URL treatment (`/projects/:pid/apps`) as a follow-up? It has the same hidden-state weakness.
4. Is the "Other projects" line enough of a switcher on the Kaisen pages, or should project switching also appear in the page header for everything?
