# Only needed when there's no load balancer: Traefik runs on node hostPorts, so the nodes
# themselves must accept 80/443 from the internet. When a load balancer is used, DO's LB sits in
# front and the nodes don't need public 80/443 (the LB forwards over the VPC).
resource "digitalocean_firewall" "ingress_hostports" {
  count = var.use_load_balancer ? 0 : 1

  name = "picopaas-ingress-hostports"

  tags = ["picopaas-node"]

  inbound_rule {
    protocol         = "tcp"
    port_range       = "80"
    source_addresses = ["0.0.0.0/0", "::/0"]
  }

  inbound_rule {
    protocol         = "tcp"
    port_range       = "443"
    source_addresses = ["0.0.0.0/0", "::/0"]
  }

  # SSH left to DO's default droplet firewall / DOKS management; not opened here.

  outbound_rule {
    protocol              = "tcp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }

  outbound_rule {
    protocol              = "udp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }
}
