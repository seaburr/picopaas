resource "digitalocean_vpc" "picopaas" {
  name     = "picopaas-${var.region}"
  region   = var.region
  ip_range = "10.10.0.0/24"
}
