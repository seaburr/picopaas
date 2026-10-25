# 0009: Turn off sql_require_primary_key on managed MySQL (status: APPROVED by Chas 2026-10-25)

1) **What is being deployed.** `sql_require_primary_key = false` on the managed MySQL cluster `picopaas-mysql-nyc3`, via a new
   Terraform resource `digitalocean_database_mysql_config` in `infra/database.tf` (provider 2.106.0 supports the argument).
   No new cluster, no replacement. Removing the resource later does not reset the setting on the cluster (provider behaviour).
2) **Why.** Ghost 6 migration 6.61 creates a table without a primary key (upstream will not change it), which the cluster rejects;
   Wiki.js hit the same blocker, and we conform to apps rather than patch them.
3) **Cost.**
   - **Static: $0.** A configuration change on the existing cluster.
   - **Per app deployed: $0.**

**Risk and notes**
- DigitalOcean recommends primary keys (replication and node recovery can misbehave on large PK-less tables). Our cluster is single
  node (`node_count = 1` in `infra/database.tf`, no standby, no read replicas), so replication is not in play.
- Tenants can now create tables without primary keys; the server no longer enforces it. docs/USER_GUIDE.md and docs/ADMIN_GUIDE.md
  still describe the old behaviour and should be updated after the apply.
- Backups: restic of mysqldump output. Dumps restore the same tables with or without the setting, so no impact expected
  (dumps of PK-less tables work as before).
- Restart: DO's docs do not say whether this change restarts the database. MySQL can set this variable at runtime and DO applies
  config PATCHes through its API, so a live change is expected, but it is unconfirmed. Plan the apply for a quiet moment.
