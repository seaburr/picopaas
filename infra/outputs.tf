output "cluster_id" {
  description = "DOKS cluster ID."
  value       = digitalocean_kubernetes_cluster.picopaas.id
}

output "cluster_endpoint" {
  description = "DOKS cluster API endpoint."
  value       = digitalocean_kubernetes_cluster.picopaas.endpoint
}

output "mysql_host" {
  description = "Managed MySQL host (empty string when use_managed_mysql = false)."
  value       = var.use_managed_mysql ? digitalocean_database_cluster.mysql[0].host : ""
  sensitive   = true
}

output "platform_bucket_name" {
  description = "Platform Spaces bucket name (backups, platform artifacts)."
  value       = digitalocean_spaces_bucket.platform.name
}

# Managed PostgreSQL (request 0006). All sensitive; empty strings when use_managed_postgres = false.
output "postgres_host" {
  description = "Managed PostgreSQL private host (VPC-internal; what pods use)."
  value       = var.use_managed_postgres ? digitalocean_database_cluster.postgres[0].private_host : ""
  sensitive   = true
}

output "postgres_port" {
  description = "Managed PostgreSQL port (DO assigns a non-standard port, e.g. 25060)."
  value       = var.use_managed_postgres ? digitalocean_database_cluster.postgres[0].port : 0
  sensitive   = true
}

output "postgres_admin_user" {
  description = "Managed PostgreSQL admin user (doadmin)."
  value       = var.use_managed_postgres ? digitalocean_database_cluster.postgres[0].user : ""
  sensitive   = true
}

output "postgres_admin_password" {
  description = "Managed PostgreSQL admin password. Feed into BOOTSTRAP_PG_PASSWORD / k8s Secret; never commit."
  value       = var.use_managed_postgres ? digitalocean_database_cluster.postgres[0].password : ""
  sensitive   = true
}

output "postgres_private_uri" {
  description = "Managed PostgreSQL admin connection URI over the private network."
  value       = var.use_managed_postgres ? digitalocean_database_cluster.postgres[0].private_uri : ""
  sensitive   = true
}

output "postgres_ca_cert" {
  description = "Managed PostgreSQL CA certificate (PEM) for sslmode=verify-ca."
  value       = var.use_managed_postgres ? data.digitalocean_database_ca.postgres[0].certificate : ""
  sensitive   = true
}
