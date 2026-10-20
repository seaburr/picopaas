#!/bin/sh
# Maps PicoPaaS's UPPER_SNAKE env (its env-ref keys must match ^[A-Z_][A-Z0-9_]*$) onto Ghost's
# lowercase, __-nested config keys. These lowercase vars are created INSIDE the container at runtime;
# PicoPaaS never sets them. Then exec Ghost as the current (non-root) UID.
set -e

: "${PORT:=2368}"
export server__host="0.0.0.0"
export server__port="$PORT"
export NODE_ENV="production"

# Public site URL. Set SITE_URL to the PicoPaaS app-url ref: SITE_URL=${{ app.url }} (resolves to the app's
# generated https URL at deploy time, so this is a single-step deploy). Ghost refuses to start without a url.
if [ -z "$SITE_URL" ]; then
  echo "ghost-entrypoint: SITE_URL is not set. Set it to SITE_URL=\${{ app.url }} (or the app's https URL) and redeploy." >&2
  exit 1
fi
export url="$SITE_URL"

# Database: PicoPaaS MySQL (content lives here, so it survives restarts).
export database__client="mysql"
export database__connection__host="$DB_HOST"
export database__connection__port="${DB_PORT:-3306}"
export database__connection__user="$DB_USER"
export database__connection__password="$DB_PASSWORD"
export database__connection__database="$DB_NAME"
# PicoPaaS caps each app's MySQL user to a small number of concurrent connections (shared DB server). Keep
# Ghost's knex pool under that cap, or requests fail with "exceeded the max_user_connections resource".
export database__pool__min="1"
export database__pool__max="6"
# TLS to DO managed MySQL. mysql2 accepts the CA as a PEM string. Node uses OpenSSL (not Python's
# VERIFY_X509_STRICT), so DO's CA verifies normally.
if [ -n "$DB_CA_CERT" ]; then
  export database__connection__ssl__ca="$DB_CA_CERT"
  export database__connection__ssl__rejectUnauthorized="true"
fi

# Uploaded images -> the PicoPaaS S3 bucket (so they survive restarts too). Without a bucket, Ghost
# would write to the ephemeral local disk and lose images on restart.
if [ -n "$S3_BUCKET" ]; then
  export storage__active="s3"
  export storage__s3__accessKeyId="$S3_ACCESS_KEY"
  export storage__s3__secretAccessKey="$S3_SECRET_KEY"
  export storage__s3__bucket="$S3_BUCKET"
  export storage__s3__region="${S3_REGION:-us-east-1}"
  export storage__s3__endpoint="$S3_ENDPOINT"
  export storage__s3__forcePathStyle="true"
  # The adapter's default public URL is AWS-style (s3-<region>.amazonaws.com), which is wrong for DO Spaces.
  # Point assetHost at the PUBLIC Spaces URL for this bucket so Ghost emits working <img> links. (Uploads use
  # the possibly-VPC-private S3_ENDPOINT above; the public asset host is derived from the bucket + region.)
  export storage__s3__assetHost="https://${S3_BUCKET}.${S3_REGION}.digitaloceanspaces.com"
fi

# PicoPaaS blocks outbound SMTP, so Ghost's staff sign-in device-verification email can't be delivered and
# would lock admins out after the first login ("Failed to send email"). Disable it so the admin stays usable.
# Trade-off: this also removes the optional email-2FA. Configure a real mail provider to re-enable both.
export security__staffDeviceVerification="false"

export logging__transports='["stdout"]'

exec node current/index.js
