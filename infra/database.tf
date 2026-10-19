# Managed MySQL (optional — see variables.use_managed_mysql). When false, MySQL runs in-cluster
# as a StatefulSet defined in deploy/ (not managed by this Terraform at all).
resource "digitalocean_database_cluster" "mysql" {
  count = var.use_managed_mysql ? 1 : 0

  name       = "picopaas-mysql-${var.region}"
  engine     = "mysql"
  version    = "8"
  size       = var.mysql_size # see variables.tf WARNING re: ATL1 Premium Intel only
  region     = var.region
  node_count = 1 # single node, no HA — matches the rest of the lean/managed trade-off

  private_network_uuid = digitalocean_vpc.picopaas.id

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
}
