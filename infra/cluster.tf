# Latest available Kubernetes version in this region, rather than a hardcoded version string
# that will eventually be retired by DO.
data "digitalocean_kubernetes_versions" "latest" {}

resource "digitalocean_kubernetes_cluster" "picopaas" {
  name     = "picopaas-${var.region}"
  region   = var.region
  version  = data.digitalocean_kubernetes_versions.latest.latest_version
  vpc_uuid = digitalocean_vpc.picopaas.id

  # Non-HA control plane (default/free tier) per ARCHITECTURE.md — this is a hobby/throwaway
  # platform and we say so in the UI. Do not set ha = true (adds $40/mo).
  ha = false

  auto_upgrade  = true
  surge_upgrade = true

  # Optional Kubernetes API allowlist. Off by default: Chas OK'd a public API for now
  # (2026-10-18). The platform's reconciler runs in-cluster and never needs an entry.
  control_plane_firewall {
    enabled           = length(var.control_plane_allowed_cidrs) > 0
    allowed_addresses = var.control_plane_allowed_cidrs
  }

  maintenance_policy {
    # Low-traffic window; node recycling during upgrade causes a brief blip (acceptable, see
    # ARCHITECTURE.md trade-offs).
    start_time = "04:00"
    day        = "sunday"
  }

  node_pool {
    name       = "picopaas-pool-default"
    size       = var.node_size
    node_count = var.node_count
    # Cluster autoscaler. max_nodes is the compute spending ceiling (ARCHITECTURE.md §3).
    auto_scale = var.max_nodes > var.node_count
    min_nodes  = var.max_nodes > var.node_count ? var.node_count : null
    max_nodes  = var.max_nodes > var.node_count ? var.max_nodes : null

    tags = ["picopaas", "picopaas-node"]
  }

  tags = ["picopaas"]
}
