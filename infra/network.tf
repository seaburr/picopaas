resource "digitalocean_vpc" "main" {
  name     = "picopaas-${var.region}"
  region   = var.region
  ip_range = "10.11.0.0/24" # 10.10.0.0/24 is still held by the old ATL1 default VPC
}

# The first VPC (ATL1, before the 2026-10-19 move to NYC3) became the account's default VPC there, and DO refuses
# to delete default VPCs. Forget it instead of destroying it. It's free and harmless, and can be deleted by hand
# once another ATL1 VPC is made default.
removed {
  from = digitalocean_vpc.picopaas
  lifecycle {
    destroy = false
  }
}
