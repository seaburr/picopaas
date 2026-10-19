# Platform-level Spaces bucket: Terraform state lives in a SEPARATE, manually-created bucket
# ("picopaas-tfstate", see infra/README.md). This bucket is for the platform's own use — MySQL
# backups (nightly mysqldump CronJob per ARCHITECTURE.md) and any platform-level artifacts. It is
# private; per-app buckets (`pp-<id>`) are created at runtime by the platform API via the DO API,
# not by this Terraform.
resource "digitalocean_spaces_bucket" "platform" {
  name   = "picopaas-platform"
  region = var.region
  acl    = "private"

  force_destroy = false
}
