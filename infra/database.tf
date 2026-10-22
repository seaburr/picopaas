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

  lifecycle {
    prevent_destroy = true # holds all tenant data
  }
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

# Managed PostgreSQL (request 0006). Same shape as MySQL above: nyc3, same VPC, single node, no HA/standby.
resource "digitalocean_database_cluster" "postgres" {
  count = var.use_managed_postgres ? 1 : 0

  name       = "picopaas-pg-${var.region}"
  engine     = "pg"
  version    = var.postgres_version
  size       = var.postgres_size
  region     = var.region
  node_count = 1 # single node, no standby

  private_network_uuid = digitalocean_vpc.main.id

  tags = ["picopaas"]

  lifecycle {
    prevent_destroy = true # will hold tenant data
  }
}

# Same trusted sources as MySQL: the k8s cluster, plus the private VPC range (the k8s rule alone missed autoscaled nodes).
resource "digitalocean_database_firewall" "postgres" {
  count = var.use_managed_postgres ? 1 : 0

  cluster_id = digitalocean_database_cluster.postgres[0].id

  rule {
    type  = "k8s"
    value = digitalocean_kubernetes_cluster.picopaas.id
  }

  rule {
    type  = "ip_addr"
    value = digitalocean_vpc.main.ip_range
  }
}

# CA certificate for sslmode=verify-ca (consumed later by the DbServer.ca_cert bootstrap).
data "digitalocean_database_ca" "postgres" {
  count = var.use_managed_postgres ? 1 : 0

  cluster_id = digitalocean_database_cluster.postgres[0].id
}
