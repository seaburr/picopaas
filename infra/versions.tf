# PicoPaaS infra — request 0001 (docs/requests/0001-core-infra.md)
# Applied via the manual "terraform" GitHub workflow (plan + apply in Terraform Cloud). See infra/README.md.

terraform {
  required_version = ">= 1.7"

  required_providers {
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = "~> 2.43"
    }
  }

  # State and runs live in Terraform Cloud, same as Chas's other projects (e.g. tribal).
  # Auth: TF_TOKEN_app_terraform_io (GitHub secret TF_API_TOKEN in CI).
  cloud {
    organization = "seaburr"
    workspaces {
      name = "picopaas"
    }
  }
}

provider "digitalocean" {
  # DIGITALOCEAN_TOKEN read from env (GitHub secret DIGITALOCEAN_TOKEN in CI).
}
