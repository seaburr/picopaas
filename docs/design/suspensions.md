# Suspensions: users, projects, apps (design, 2026-10-31)

Status: agreed with Chas 2026-10-31, queued after the eval batch-2 follow-ups. Not built.

## Why
Today a platform admin can suspend a **user** (their personal-project apps scale to 0) or a single **app**. Two gaps:
abuse in a shared (team) project can only be stopped app by app, and Kaisen ignores suspension (a build a suspended
user started keeps running on the project's key and keeps redeploying its preview). Chas: if abuse is found, suspend
the whole project, not a single app.

## Rules
| Action | Effect |
|---|---|
| Suspend user | Login blocked (as today). Their **personal project is suspended** (rule below). Every Kaisen run they started, in any project, stops. Their team projects keep running for the other members. |
| Suspend project (new) | Every app in it is suspended (0 replicas, 503 page). Kaisen is blocked in it: no new builds, runs or deploys, and active runs stop. Members can still read the project (read-only, banner). Writes return 403. |
| Suspend app | Kept in the API and reconciler for odd cases (one runaway app); removed from the admin UI in favour of project suspension. |
| Unsuspend | Restores only what that suspension put to sleep. Apps record why they're suspended (by user, by project, or individually); an app or project suspended on its own stays suspended. |

Every suspension takes a **reason** (required), stored with who and when, written to the audit log (`project.suspend`,
`project.unsuspend`; `user.suspend` gains the reason) and shown to project owners on the banner.

## Data
- `projects`: `status` (`active` | `suspended`), `suspended_at`, `suspended_by` (user id), `suspended_reason`,
  `suspended_by_user` (true when the suspension came from suspending the owner of a personal project).
- `apps`: replace the single `suspended_by_user` flag with `suspended_via` (`user` | `project` | `admin`) so
  unsuspend can restore precisely (migrate existing rows: `suspended_by_user=true` → `user`, else `admin`).
- `users`: `suspended_reason` (shown in the Suspensions panel).

## Enforcement points
- `app/routers/admin.py`: `POST /admin/projects/{id}/suspend` (body: reason) and `/unsuspend`; user suspend/unsuspend
  go through the project rule for the personal project.
- `app/projects.py` member checks: writes to a suspended project → 403 with the reason; reads allowed.
- Reconciler: unchanged (it already renders suspended apps at 0 replicas without ingress).
- Kaisen: the worker checks, before each turn and before any deploy, that the project is active and that the run's
  creator is active and still a member; otherwise it stops the run with a plain stop reason. The API refuses new
  builds, runs, deploys and settings changes in a suspended project.
- Backups keep running for suspended projects (evidence and recovery); restores into a suspended project are refused.

## Admin UI
A **Suspensions** tab in the admin page (tabs live in `?tab=`): one table of suspended users, projects and apps with
reason, when, by whom and an Unsuspend action; a "Suspend project" action on the admin projects list (reason
required, confirm dialog naming the app count). The project page shows a banner to members. Mockup first
(ui-ux-designer), per the usual rule for layout changes.

## Tests
Unit: each rule above, including the "restore only what this suspension did" cases and Kaisen stops (run
creator suspended; project suspended mid-run; deploy refused). Live e2e (`tests/e2e/test_live.py`): suspend a test
project and the test user on throwaway apps, poll until every app stops serving, unsuspend, poll until they serve
again. Security review of the authz changes.
