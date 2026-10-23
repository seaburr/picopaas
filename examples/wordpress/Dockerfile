# WordPress for PicoPaaS: nginx + php-fpm in ONE non-root container (uid 10001), port 8080.
# PHP is pinned to a release that carries the 21 Oct 2026 fixes for CVE-2026-109618 / CVE-2026-109619.
# Tag-pinned (patch version), NOT digest-pinned: the weekly rebuild then picks up Alpine/OpenSSL/libc patches that Docker Hub
# republishes under the same tag. Bump the PHP patch version here manually when PHP releases. (8.5.12 index digest on
# 2026-10-22: sha256:f09d9172dd28b8696aef729ca2c2081a132c1f8d581218c99497d552c82a92df)
ARG PHP_IMAGE=php:8.5.12-fpm-alpine
FROM ${PHP_IMAGE}

ARG WP_VERSION=7.1.3
ARG WP_SHA1=491a78c7676b15dffcc69130c6054b8f49228a91
ARG WPCLI_VERSION=2.12.0
ARG WPCLI_SHA512=be928f6b8ca1e8dfb9d2f4b75a13aa4aee0896f8a9a0a1c45cd5d2c98605e6172e6d014dda2e27f88c98befc16c040cbb2bd1bfa121510ea5cdf5f6a30fe8832
# WITH_S3 is passed by the shared examples workflow; it has no effect here.
ARG WITH_S3=0

RUN set -eux; \
    apk add --no-cache nginx tini curl libpng libjpeg-turbo libwebp freetype libzip icu-libs libxml2; \
    apk add --no-cache --virtual .build-deps $PHPIZE_DEPS libpng-dev libjpeg-turbo-dev libwebp-dev freetype-dev libzip-dev icu-dev; \
    docker-php-ext-configure gd --with-freetype --with-jpeg --with-webp; \
    docker-php-ext-install -j"$(nproc)" gd mysqli exif zip intl bcmath; \
    apk del .build-deps; \
    rm -rf /usr/local/etc/php-fpm.d /usr/local/etc/php-fpm.conf.default /tmp/*

# WordPress core, checksum-verified, kept at /usr/src/wordpress as the seed.
RUN set -eux; \
    curl -fsSL -o /tmp/wp.tgz "https://wordpress.org/wordpress-${WP_VERSION}.tar.gz"; \
    echo "${WP_SHA1}  /tmp/wp.tgz" | sha1sum -c -; \
    mkdir -p /usr/src; tar -xzf /tmp/wp.tgz -C /usr/src; rm /tmp/wp.tgz; \
    rm -f /usr/src/wordpress/wp-config-sample.php; \
    mkdir -p /usr/src/wordpress/wp-content/uploads

RUN set -eux; \
    curl -fsSL -o /usr/local/bin/wp "https://github.com/wp-cli/wp-cli/releases/download/v${WPCLI_VERSION}/wp-cli-${WPCLI_VERSION}.phar"; \
    echo "${WPCLI_SHA512}  /usr/local/bin/wp" | sha512sum -c -; \
    chmod 0755 /usr/local/bin/wp

COPY wp-config.php /usr/src/wordpress/wp-config.php
COPY php-fpm.conf /usr/local/etc/php-fpm.conf
COPY php.ini /usr/local/etc/php/conf.d/zz-picopaas.ini
COPY nginx.conf /etc/nginx/nginx.conf
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

# Numeric non-root user; the platform checks USER is a number.
RUN set -eux; \
    addgroup -g 10001 -S wp; adduser -u 10001 -S -G wp -h /var/www -s /sbin/nologin wp; \
    mkdir -p /var/www/html; \
    chown -R 10001:10001 /var/www /usr/src/wordpress; \
    chmod 0755 /usr/local/bin/entrypoint.sh

USER 10001:10001
WORKDIR /var/www/html
EXPOSE 8080
ENTRYPOINT ["/sbin/tini", "--", "/usr/local/bin/entrypoint.sh"]
