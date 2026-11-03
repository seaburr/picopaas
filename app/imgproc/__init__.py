"""Secret-less image unit for Kaisen uploads (docs/design/kaisen.md 16.8 R1).

`python -m app.imgproc` runs a tiny HTTP server (own pod, no service-account token, no DB, no secrets, no egress) that
decodes and re-encodes raster images and sanitizes SVG. Everything that parses untrusted image bytes lives in this
package; the API pods only ever see the structured result. Pure functions live in `core`, so tests run them in-process
through the fake provider (app/providers/fake.py)."""
