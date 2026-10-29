# Projects (team apps) design, 2026-10-28

Goal: users invited into a project; several people manage the project's apps.

**Model** (alembic, 3-step: add nullable, backfill, enforce later): `projects(id,name,slug,is_personal,app_limit null,created_by,created_at,deleted_at)`;
`project_members(project_id,user_id,role owner|member,added_at; unique)`; `project_invites(id,project_id,email,token_hash sha256,role,invited_by,expires_at 7d,accepted_at,revoked_at)`;
`apps.project_id` FK (owner_id kept as creator). Every user gets a personal project (create_user + backfill); personal projects cannot be deleted or take members.
**Limits**: `effective_app_limit(user)` stays for personal; add `effective_project_app_limit(project)` (project.app_limit else default, capped at max_apps_per_user; admin override). Count active apps per project_id. Per-app quotas unchanged.
**Permissions**: one helper `get_app_for_member(db, app_id, user)` replaces `_get_owned_app_or_404` and all `owner_id` filters (`grep owner_id app/`). Member: deploy/edit/env/restart/backup. Owner adds: members, roles, project delete, project limit. Non-members get 404. API keys act as the user, membership checked per request; keys cannot manage members (session only). Notification target: project owners (or all members).
**Invites**: no email is sent; UI shows a link. Single use, 7-day expiry, rate-limited, 32-byte token, hash stored; accept needs login (email match, or any account for link invite). Pending (unapproved) users cannot join until approved.
**Edge cases**: last owner cannot leave/demote (409); project delete blocked while apps exist; move-app between projects owner-only with limit check; DB/bucket stay attached to App; user deletion blocked if sole owner of a project with apps; suspending a user does not stop team apps.
**API**: /projects, /projects/{id}/members, /projects/{id}/invites, /invites/{token}/accept; POST /apps optional project_id (default personal); GET /apps?project_id=; spec YAML `project:`; CLI --project / PICOPAAS_PROJECT. Audit actions project.create/invite/join/remove/role_change/delete, project id in detail.
**UI**: project switcher in App.vue (Pinia, localStorage), ProjectView.vue (members, roles, invite link, leave, delete), /invite/:token route, project shown in AppFormView, limit/usage per project on AppsListView.
**Stages**: BE-1 models+migration+backfill+personal project; BE-2 get_app_for_member refactor (existing tests pass unchanged); BE-3 project/member/invite CRUD, limits, audit; BE-4 spec/CLI/docs/admin override/notification routing; FE-1 types/api/store/switcher; FE-2 ProjectView/invite route/AppForm field; final pentest (IDOR across projects, invite token, key access after removal).
