variable "region" {
  description = "DigitalOcean region. ATL1 preferred per Chas (2026-10-18); NYC3 is the fallback."
  type        = string
  default     = "atl1"
}

variable "node_size" {
  description = "Droplet size slug for the DOKS worker node pool. s-2vcpu-2gb ($18): ATL1 had no capacity for any s-2vcpu-4gb variant (Chas chose smaller nodes, 2026-10-19). ~1 GB usable per node."
  type        = string
  default     = "s-2vcpu-2gb"
}

variable "node_count" {
  description = "Number of worker nodes in the default node pool. Single node per ARCHITECTURE.md (no HA)."
  type        = number
  default     = 1
}

variable "max_nodes" {
  description = "Autoscaler ceiling. Each node above node_count can add $24/mo. Equal to node_count disables autoscaling."
  type        = number
  default     = 2
}

variable "use_managed_mysql" {
  description = <<-EOT
    true  = provision a DO managed MySQL cluster (Chas is leaning this way as of 2026-10-18).
    false = skip it; in-cluster MySQL StatefulSet is deployed via deploy/ manifests instead (not this Terraform).
  EOT
  type        = bool
  default     = true
}

variable "mysql_size" {
  description = <<-EOT
    DO managed MySQL size slug. Chas chose Premium AMD 1 vCPU / 1 GiB in ATL1 ($19.30/mo, 2026-10-19).
    The terraform workflow checks this slug against GET /v2/databases/options before planning.
  EOT
  type        = string
  default     = "db-amd-1vcpu-1gb"
}

variable "use_load_balancer" {
  description = <<-EOT
    true  = a DO Load Balancer fronts the cluster (provisioned implicitly by the Traefik
            Service of type LoadBalancer in deploy/, NOT by this Terraform — DO creates it
            automatically when that Service is applied). This file only creates the DNS wiring
            for it; see the wildcard_lb_ip variable below.
    false = no load balancer. Traefik runs on node hostPorts (80/443), and a
            digitalocean_firewall resource here opens those ports to the node pool by tag.
            DNS is kept fresh by a dns-sync CronJob (TODO: deploy/dns-sync, not yet written)
            rather than a static Terraform record.
  EOT
  type        = bool
  default     = true
}

variable "domain" {
  description = "Existing DO-managed domain. Looked up via data source only — never created/destroyed here."
  type        = string
  default     = "raisehellpraisedale.xyz"
}

variable "wildcard_lb_ip" {
  description = <<-EOT
    TODO: the public IP of the load balancer that Traefik's Service creates, needed to wire
    up the wildcard A record when use_load_balancer = true. DO allocates this IP only after the
    Traefik Service is applied (chicken-and-egg: this Terraform runs before deploy/ manifests
    exist). Options to resolve later:
      1. Leave this var empty on first apply, install deploy/traefik, read the LB IP with
         `kubectl -n traefik get svc traefik`, then re-apply this Terraform with the IP filled in.
      2. Pre-allocate a DO reserved IP in Terraform and assign it to the LB via a
         digitalocean_loadbalancer resource managed here instead of letting the Service create one
         (bigger change — not done in this chunk).
    Until this var is set, no wildcard A record is created even when use_load_balancer = true (see
    the record's `count` in dns.tf) — the TODO is intentionally left unresolved rather than guessed.
  EOT
  type        = string
  default     = ""
}

variable "control_plane_allowed_cidrs" {
  description = "CIDRs allowed to reach the Kubernetes API (e.g. Chas's IP as x.x.x.x/32). Empty (default) leaves the API public, which Chas OK'd for now."
  type        = list(string)
  default     = []
}
