# Request 0002: Should stolen credentials be useless across apps? (status: DECIDED: A, plus no public infra)

## Decision (Chas, 2026-10-18)
**Option A.** Credential theft is out of scope. On top of that, **infra must not be reachable from the
public internet**, so the attack surface is limited to PicoPaaS itself and DigitalOcean. See
ARCHITECTURE.md "Public exposure". We'll still ship a credential-rotation action.

## The question
Network isolation between apps is enforceable either way (NetworkPolicy, tested by PENTEST.md 1–7).
Chas also wants app 1 to be unable to use app 2's resources **even with app 2's credentials**.
How far we can go depends on the hosting variant in request 0001.

| Resource | In-cluster MySQL (lean) | Managed MySQL |
|---|---|---|
| DB with stolen creds | **Achievable.** MySQL sees real pod IPs, so the reconciler binds each user to its pod IP (`'app_2'@'10.244.x.y'`) and updates it when the pod restarts. Cost: complexity, plus a few seconds of DB refusal after each restart while the binding updates. | **Not achievable without a proxy.** Pod traffic leaving the cluster for the VPC is (to be verified in dev) SNATed to the node IP, so every app on a node looks identical to the DB. Option: a platform DB proxy that authenticates by namespace (significant build). |

| | Any variant |
|---|---|
| Spaces with stolen keys | Keys work from anywhere on the internet, and bucket IP policies don't apply to per-bucket keys. Achieving this needs an in-cluster storage proxy that apps use instead of Spaces directly. That's a significant build and also changes the S3 endpoint apps see. |

## Options
A. **Accept credential theft as out of scope.** A stolen credential means the victim app is already
   compromised. Rely on network isolation, per-app credentials, and easy rotation ("regenerate DB password"
   button). Cheapest.
B. **Bind DB users to pod IPs** (needs in-cluster MySQL, which tilts request 0001 toward the lean or LB-only
   variant). Treat Spaces as option A.
C. **B + a storage proxy.** Full "even with creds" isolation. Most work, best answer to the pentest.

## Recommendation
B. It gives real defense for the DB, the highest-value target, at modest cost, and it keeps the cheaper
in-cluster MySQL. Add a credential-rotation button either way.

## Cost
$0 infra for A/B. C adds no paid infra, but the proxy uses ~50m CPU / 64Mi RAM of node capacity.
