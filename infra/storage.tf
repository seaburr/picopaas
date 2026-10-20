# Platform-level Spaces bucket (Terraform state lives in Terraform Cloud, not here). For the platform's own use — MySQL
# backups (nightly mysqldump CronJob per ARCHITECTURE.md) and any platform-level artifacts. It is
# private; per-app buckets (`pp-<id>`) are created at runtime by the platform API via the DO API,
# not by this Terraform.
#
# Chas created this bucket by hand (2026-10-19) when making the Spaces key; adopt it rather than create another.
import {
  to = digitalocean_spaces_bucket.platform
  id = "atl1,pico-paas-s3-a"
}

resource "digitalocean_spaces_bucket" "platform" {
  name   = "pico-paas-s3-a"
  region = "atl1" # Spaces stays in ATL1 even though compute moved to NYC3; changing it would replace the bucket
  acl    = "private"

  force_destroy = false

  lifecycle {
    prevent_destroy = true
  }
}
