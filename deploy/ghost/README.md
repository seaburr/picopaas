# Ghost blog branding (blog.picopaas.xyz)

`code-injection.head.html` is the source of truth for the blog's Ghost **Code Injection (site header)**: a
theme-agnostic `<style>` block using the PicoPaaS palette (see `frontend/src/style.css`, `public/landing.html`).
It is **not** auto-deployed; it is applied to the live blog through the Ghost Admin API.

Re-apply: load `GHOST_API_URL` and `PICOPAAS_GHOST_BLOGGER_TOKEN` (`id:secret`) from the environment, mint an
HS256 JWT (`kid`=id, key=`bytes.fromhex(secret)`, `aud=/admin/`, 5 min), then
`PUT $GHOST_API_URL/ghost/api/admin/settings/` with header `Authorization: Ghost <jwt>`, `Accept-Version: v5.0`
and body `{"settings":[{"key":"codeinjection_head","value":"<file contents>"}]}`. Never log the token or JWT.
Verify: `curl -s https://blog.picopaas.xyz/ | grep -c picopaas-brand`.
Rollback: PUT an empty `codeinjection_head`.
