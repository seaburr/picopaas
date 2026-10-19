# Request 0003: App and platform domain (status: AWAITING CHAS)

Decided by Chas 2026-10-19. Replaces seaburr.io in all plans.

## 1. What is being deployed
Use `raisehellpraisedale.xyz` for both the platform (`platform.raisehellpraisedale.xyz`) and apps
(`<pet>-<7char>.raisehellpraisedale.xyz`). Chas points the domain's nameservers at DigitalOcean at his
registrar (`ns1.digitalocean.com`, `ns2.digitalocean.com`, `ns3.digitalocean.com`) and adds the domain in the DO panel.
Terraform only reads the domain (data source); the wildcard cert and `*` / `picopaas` A records are created by cert-manager and dns-sync.

## 2. Why
Isolate seaburr.io's reputation from user-supplied content.

## 3. Cost
Static: $0 new (domain already owned; renewal is Chas's existing cost; DO DNS is free). Per app: $0.

## Checklist for Chas
- [ ] At the registrar, set nameservers to ns1/ns2/ns3.digitalocean.com
- [ ] In DO: Networking > Domains > add `raisehellpraisedale.xyz`
- [ ] Ensure the DO token used by cert-manager / dns-sync has DNS scope for this domain
