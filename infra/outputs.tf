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

output "tfstate_bucket_name" {
  description = "Terraform state bucket name (created manually, not by this config — see README)."
  value       = "picopaas-tfstate"
}
