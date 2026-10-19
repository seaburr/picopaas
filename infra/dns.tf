# The raisehellpraisedale.xyz domain already exists in the DO control panel and is managed by hand / by Chas.
# This is a DATA SOURCE ONLY — Terraform must never create or destroy the domain itself, and must
# NEVER touch TXT records (general rule; see request 0003 and
# ARCHITECTURE.md §3). Only the wildcard A record below is managed here, and only conditionally.
data "digitalocean_domain" "domain" {
  name = var.domain
}

# NOTE: .github/workflows/bootstrap.yml owns the `*` and `picopaas` A records (it upserts them to the
# Traefik LB IP after the LB exists). Leave var.wildcard_lb_ip EMPTY so this resource stays
# disabled; setting it would make Terraform and bootstrap.yml fight over the `*` record.
#
# Wildcard A record, only when a load balancer is in play AND its IP is known.
#
# TODO (see variables.wildcard_lb_ip): the LB is created implicitly by Traefik's
# `Service.type: LoadBalancer` in deploy/, which doesn't exist until after this Terraform's first
# apply and a kubectl/helm deploy step. Until wildcard_lb_ip is supplied (non-empty), this resource
# is skipped entirely — no record is created or guessed.
resource "digitalocean_record" "wildcard_lb" {
  count = var.use_load_balancer && var.wildcard_lb_ip != "" ? 1 : 0

  domain = data.digitalocean_domain.domain.name
  type   = "A"
  name   = "*"
  value  = var.wildcard_lb_ip
  ttl    = 300
}

# When use_load_balancer = false, DNS is NOT managed here at all. A `dns-sync` CronJob (TODO:
# deploy/dns-sync/, not yet written — see ARCHITECTURE.md trade-offs) is expected to repoint the
# wildcard A record at the current node's public IP every ~2 minutes via the DO API, because node
# IPs change on recycle/upgrade. Do not add a static digitalocean_record for the no-LB case — it
# would immediately go stale.
