# 0007: Persistent volumes for apps (status: PENDING, awaiting Chas)

Nothing here is built or provisioned. The feature ships disabled (`VOLUMES_ENABLED` unset), so merging the code costs
nothing; money is only spent after Chas approves and the flag is turned on. Design background:
`docs/research/wordpress-requirements.md` (REQ-1 to REQ-5, REQ-9).

1) **What is being deployed.** DigitalOcean Block Storage volumes, provisioned on demand through the DOKS CSI driver
   (already installed on DOKS) with storage class `do-block-storage`. One `ReadWriteOnce` PVC per app that asks for one
   (`resources.volume`), mounted into the app container. The Deployment for such an app uses `Recreate` and a single
   replica. The feature is gated by `VOLUMES_ENABLED` (default off).
2) **Why.** Stateful apps like WordPress need a filesystem that survives restarts and redeploys; the first user is
   Chas's blog.
3) **Cost.**
   - **Static: $0.** No volume exists until an app requests one.
   - **Per volume: $0.10 per GiB per month**, 1 GiB minimum, billed hourly **while the volume exists, even if the app is
     stopped or the volume is detached** (verified against DO's volume pricing page, https://docs.digitalocean.com/products/volumes/details/pricing/).
     A 1 GiB volume is $0.10/mo.
   - **Snapshots: $0.06 per GiB per month**, only if and when scheduled backups are added (REQ-9, not part of this request).
   - **Worst case with defaults:** 10 volumes (`VOLUMES_MAX_TOTAL=10`) at 1 GiB each is about **$1/mo**. Chas's blog is
     expected at 2 to 5 GiB (its site is about 0.2 GiB today), so **$0.20 to $0.50/mo**. Even 10 volumes at the 20 GiB
     admin cap would be $20/mo, which is why the total count and size caps exist. Baseline stays about $75.30/mo
     (`docs/deployment/infrastructure.md`).

## Constraints to know before approving

- **Node attach limit: 15 volumes per DOKS node** (raised from 7 on 2024-08-29; DO "Volume Limits" page). With
  `VOLUMES_MAX_TOTAL=10` we stay under it on one node. DO's idea tracker notes the limit is not linear with node size,
  so treat 15 as a ceiling, not a promise.
- **Account volume quota.** Community answers cite a default of 10 block-storage volumes per account (raisable via
  support); DO's docs now list much higher limits. If a create fails with a limit error, contact DO support. Any volumes
  already on the account (for example the DOKS node's own) count toward this.
- **A volume pins its pod to the node it is attached to.** If that node is drained, replaced or the autoscaler removes it,
  the pod reschedules and the volume detaches and reattaches, about 1 minute of downtime. An RWO volume cannot be
  surge-mounted, so volume apps use `Recreate`: **every deploy, restart and node drain is a short outage** (roughly
  30 to 90 seconds). Single replica only.
- **Pod/node placement.** A volume is created in the cluster's region (NYC3); pods can only attach it on nodes in that
  region (all of ours are).
- **StorageClass (from DO docs).** `do-block-storage` is the DOKS CSI default class. DO documents `Delete` reclaim for
  provisioned volumes (deleting the PVC deletes the DO volume, so deleting an app leaves no orphan and no ongoing charge).
  Volumes are grow-only; expansion works on current DOKS versions via PVC resize (DO cannot shrink). Block volumes
  support `ReadWriteOnce` only. **Not verified against the live cluster:** my kubectl context was not DOKS, so the
  implementation step must run `kubectl get storageclass do-block-storage -o yaml` (read-only) and confirm
  `reclaimPolicy: Delete` and `allowVolumeExpansion: true` before enabling.
- **Data is not backed up** by this request. Deleting the app deletes the data. Snapshots/backups (REQ-9) would be a
  follow-up request at $0.06/GiB/mo.

## Decision needed (Chas)

- [x] **Approve enabling on this instance.** Enabling sets `VOLUMES_ENABLED=1` in the platform pod env. Per
      `docs/deployment/ci-cd.md`, platform knobs are set at bootstrap into the `picopaas-env` Secret / deployment env (same
      place as `MAX_NODES`, `APP_DOMAIN`), so it is added to the bootstrap/deploy manifests and applied by the `bootstrap`
      and `deploy` workflows. It stays off until that line merges.
- [x] **Approve defaults:** ordinary users max **1 GiB** per volume, platform-admin override up to **20 GiB**, and
      **10 volumes total** (`VOLUMES_MAX_TOTAL`) across the platform.

Approval line (Chas fills in): `APPROVED: <variant> on <date>` / `DECLINED`.

APPROVED: enable with the defaults above on 2026-10-23 (Chas in chat: "Send it").

StorageClass verified live by Chas 2026-10-23: `do-block-storage` (default class, `dobs.csi.digitalocean.com`),
`reclaimPolicy: Delete`, `allowVolumeExpansion: true`, `volumeBindingMode: Immediate`.

2026-10-23 Chas lowered the admin ceiling default to 10 GiB (`OVERRIDE_VOLUME_MAX_GI`, env-overridable). Worst case with 10 volumes is now $10/mo.
