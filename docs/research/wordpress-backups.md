# Backing up a WordPress site on PicoPaaS

Date: 2026-10-23. Author: wp-expert. Feeds REQ-9 in `wordpress-requirements.md`. The old server's backup script was reviewed
(its contents are summarised, not copied; it contains a database password in plain text, which is not repeated here).

## 1. Review of the old `backup-wordpress` script

What it does: a root `cron.daily` shell script that, on a CentOS VM, makes a staging directory under `/mnt/data/backups`, then copies
the Let's Encrypt directory (including private keys), the Apache config, the whole docroot, server specs/package lists, runs `mysqldump`
against the managed DB, tars everything into a single `sitedump.tar.gz` (owner `nobody`), and deletes the staging directory. It
writes a marker file on failure.

Weaknesses found:
1. **Secret in the script.** The DB password is on the `mysqldump` command line: visible in `ps` and stored in the script, which is itself copied into the archive.
2. **Nothing leaves the box.** One tarball on the same VM's data volume. A lost VM or volume loses the site and its only backup.
3. **No retention or history.** Each run deletes the previous tarball first. One generation only; a corruption or a bad change noticed two days later is unrecoverable. If a run fails midway the old good copy is already gone.
4. **No encryption.** The archive bundles TLS private keys, DB credentials in `wp-config.php`, user data and comment data in plain gzip.
5. **Dump consistency and flags.** No `--single-transaction`, so MyISAM-style locking behaviour is not guaranteed and a live site can produce an inconsistent dump (the dump here was taken with GTID info recorded, which then breaks restore on another managed server). No `--set-gtid-purged=OFF`, no `--no-tablespaces`, no error check on `mysqldump`: a failed dump still produces a tarball and a "Done." line.
6. **No integrity check and no restore test.** Nobody verified the archive opens or the dump imports; the failure marker is the only signal and nothing watches it. There is no alert on failure or on a stale backup.
7. **Bloat.** Copies the full WordPress core and all plugins every day (about 200 MB) plus an unbounded audit-log table (97% of the dump) while the real content is about 20 MB.
8. **Operational.** `cp -R` of live files while the site runs (tearing is possible), a fixed `cron.daily` time, an `EUID` check but no locking against overlapping runs, hard-coded paths.

Net: it is a reasonable "get me a snapshot of everything" script for a hand-run VM, and it did allow this migration, but it would not survive
a disk loss, a late-noticed corruption, or a stolen backup.

## 2. What DigitalOcean gives for free (checked 2026-10-23)

| Item | Facts |
|---|---|
| Managed MySQL automatic backups | Daily, time chosen by DO (not configurable), kept **7 days**, restore goes to a **new cluster** (point-in-time within the window is offered at restore). No separate backup charge is stated. Backups are not downloadable and **are destroyed with the cluster**. Restoring one app's database out of a shared cluster is a manual job (restore to a new cluster, dump the one database) |
| Volume snapshots | $0.06/GiB/month; crash-consistent, restore creates a new volume. Block storage itself $0.10/GiB/month |
| Spaces | $5/month base includes 250 GB storage and 1 TB transfer; no built-in versioning for objects we rely on, but lifecycle rules and versioning can be enabled per bucket |

## 3. Options compared

| Option | Covers | Consistency | Off-site / encrypted | Retention | Restore effort | Cost on our scale (about 0.2 GiB site) | Fit |
|---|---|---|---|---|---|---|---|
| **restic or kopia** to an S3 bucket (Spaces), run as a sidecar job or in-container cron | volume files; stdin of a DB dump | dump with `--single-transaction`, files from a snapshot or while quiet | yes, client-side encrypted, deduplicated | `forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6` | `restic restore` plus `mysql < dump`; simple | pennies (dedup keeps it near the 20 MB real content) | **best primary** |
| `mysqldump --single-transaction` / `wp db export` | database only | consistent for InnoDB | only if piped into restic/age | via restic | easy | negligible | the DB half of the restic job |
| DO managed MySQL daily backups | whole cluster | consistent | same account/cluster, lost with the cluster | 7 days fixed | restore to a **new cluster**, then extract one DB | free | safety net for disasters, not per-app restore |
| DO volume snapshots | volume only (block level) | crash-consistent | same account, not encrypted separately | you script it | new volume from snapshot; fast | $0.06/GiB/mo (about $0.06 for 1 GiB) | good second layer; needs platform support for snapshot APIs |
| WP plugins (UpdraftPlus, etc.) | files + DB from inside WP | plugin-dependent | to S3/other | plugin setting | via dashboard | free tiers | easy for end users, but PHP at 200m CPU, writes temp files on scratch, and bundles can exceed the 250 MB scratch cap; weaker for us |
| Ad-hoc tarball script (the old approach) | everything | weak | no | none | untested | free | not recommended |

## 4. Recommendation

1. **Per app: restic (or kopia) to the app's own bucket or a platform-owned backup bucket**, with a repository password held as a platform secret, running as a platform-scheduled job (the backlog item "Scheduled jobs per app" / REQ-14) that:
   - dumps the app database with `mysqldump --single-transaction --no-tablespaces --set-gtid-purged=OFF` (or `wp db export`) and pipes it into `restic backup --stdin`;
   - backs up the volume (`wp-content` only; WordPress core is reproducible from the image) with `restic backup`;
   - applies `restic forget --prune --keep-daily 7 --keep-weekly 4 --keep-monthly 6`;
   - runs `restic check --read-data-subset=5%` weekly and records last-success time for the UI/health page so a stale backup is visible.
   Restic is client-side encrypted, deduplicated, one static binary (about 20 MB), and speaks S3, so it fits the existing Spaces setup. Kopia is equivalent; pick one platform-wide.
2. **Keep DO managed-DB daily backups as the disaster layer** (free, 7 days, cluster-wide), and document that single-app restore from it means restoring to a new cluster. Do not rely on it for per-app restore.
3. **Offer volume snapshots as an opt-in extra** ($0.06/GiB/month) for fast whole-volume rollback before risky changes (WordPress core upgrade, plugin changes). The platform should expose "snapshot now" and "restore from snapshot" per app, and take one automatically before a volume app's image or core upgrade.
4. **Do not use WP backup plugins** as the platform answer; fine for users who want them, but they compete for the 200m CPU and 250 MB scratch.
5. **Restores must be tested**: a documented one-click or CLI restore into a throwaway app (the `WP_RESTORE=1` flow in `examples/wordpress/MIGRATION.md` is the same mechanism, reading a dump and tarball from a bucket), run monthly by QA.

## 5. What the platform itself should provide (REQ-9 refinement)
- A **scheduled job primitive per app** (image + command + env, with the app's DB credentials and bucket, and the volume mounted read-only) and a run-once variant for restores.
- A **backup bucket quota** separate from the app bucket (backups of a 1 GiB volume may exceed 100 MB) and an owner-visible "last successful backup" with alerting when stale.
- **Encryption by default** with a per-app key escrowed with the platform admin (otherwise a lost key means lost backups).
- **Retention policy** defaults (7 daily, 4 weekly, 6 monthly) and a delete-with-app rule that also deletes the backups after a grace period.
- A tested **restore command** (`picopaas restore <app> --from <snapshot>`) that provisions volume and DB and replays them, plus a quarterly restore drill.
- Cost: restic repo in Spaces is covered by the existing $5/month base (250 GB); snapshots add $0.06/GiB/month only if enabled.

Sources: DigitalOcean docs on restoring managed-database backups; DO pricing for block storage snapshots and Spaces (as recorded in `docs/ROADMAP.md`).
