# PicoPaaS infra — request 0001 (docs/requests/0001-core-infra.md)
# NOT APPLIED. Written for review only. See infra/README.md before running anything.

terraform {
  required_version = ">= 1.5"

  required_providers {
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = "~> 2.43"
    }
  }

  # State lives in a private DO Spaces bucket (S3-compatible), created ONCE, manually,
  # outside of Terraform (see infra/README.md "State bucket"). Do not add a
  # digitalocean_spaces_bucket resource for this bucket — Terraform must not own its
  # own backend.
  #
  # Credentials (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY set to the Spaces key, or
  # SPACES_ACCESS_KEY_ID / SPACES_SECRET_ACCESS_KEY mapped to those) come from the
  # environment — never hardcode them here.
  backend "s3" {
    bucket                      = "picopaas-tfstate"
    key                         = "picopaas/terraform.tfstate"
    region                      = "us-east-1" # dummy; DO Spaces ignores AWS regions, see README
    endpoint                    = "https://atl1.digitaloceanspaces.com"
    skip_credentials_validation = true
    skip_metadata_api_check     = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
    use_path_style              = true
  }
}

provider "digitalocean" {
  # DIGITALOCEAN_TOKEN read from env (GitHub secret DIGITALOCEAN_TOKEN in CI).
}
