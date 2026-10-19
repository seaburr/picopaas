# Managed MySQL (optional — see variables.use_managed_mysql). When false, MySQL runs in-cluster
# as a StatefulSet defined in deploy/ (not managed by this Terraform at all).
resource "digitalocean_database_cluster" "mysql" {
  count = var.use_managed_mysql ? 1 : 0

  name       = "picopaas-mysql-${var.region}"
  engine     = "mysql"
  version    = "8.4"          # DO only offers 8.4 (options API, 2026-10-19)
  size       = var.mysql_size # see variables.tf WARNING re: ATL1 Premium Intel only
  region     = var.region
  node_count = 1 # single node, no HA — matches the rest of the lean/managed trade-off

  private_network_uuid = digitalocean_vpc.main.id

  tags = ["picopaas"]
}

# Trust the k8s cluster's VPC so app pods can reach managed MySQL on 3306. DO database firewalls
# accept k8s cluster UUIDs directly as a rule type, which also covers node IP churn.
resource "digitalocean_database_firewall" "mysql" {
  count = var.use_managed_mysql ? 1 : 0

  cluster_id = digitalocean_database_cluster.mysql[0].id

  rule {
    type  = "k8s"
    value = digitalocean_kubernetes_cluster.picopaas.id
  }

  # The k8s rule didn't cover a node the autoscaler added (2026-10-19: pods on the new node timed out, pods on the
  # original node connected). Trust the private VPC range too: only this cluster's nodes live in it, and pod
  # traffic to the DB leaves via node IPs.
  rule {
    type  = "ip_addr"
    value = digitalocean_vpc.main.ip_range
  }
}
