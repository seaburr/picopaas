# Operations

Moved: generic administration is in [ADMIN_GUIDE.md](ADMIN_GUIDE.md); this instance's runbooks, keys, DR and accepted risks are in [deployment/](deployment/README.md); admin routes are in [API.md](API.md).

## Backups stuck or leaving leftovers

The leader sweeps every 10 minutes (`app/backup_sweeper.py`): see `sweeper` in `GET /admin/health/backups` (`last_run_at`, `cleaned`, `errors`, `orphan_repos`) and the `backup.orphan_cleaned` audit rows. A run that stays `running` past its deadline, an orphan `bk-*` Job/Secret in `picopaas-backup` or `app-*`, or a stale restic lock should clear itself within one sweep; if `errors` is not empty or `last_run_at` is old, check that the leader runs with `RECONCILER_ENABLED=1` and the backup key can list the bucket. `orphan_repos` are repo prefixes with no app config: inspect, never delete automatically. The `restic unlock` step needs the backup image republished and `BACKUP_IMAGE` re-pinned.
