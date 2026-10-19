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
    auto_scale = false

    tags = ["picopaas", "picopaas-node"]
  }

  tags = ["picopaas"]
}
