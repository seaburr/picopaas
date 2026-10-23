<?php
/**
 * PicoPaaS-managed wp-config.php. Contains NO secrets: everything comes from the environment.
 * The entrypoint rewrites this file on every boot (set WP_CONFIG_MANAGED=false to take it over).
 */
function picopaas_env( $key, $default = null ) {
	$v = getenv( $key );
	return ( false === $v || '' === $v ) ? $default : $v;
}

define( 'DB_NAME', picopaas_env( 'WORDPRESS_DB_NAME', 'wordpress' ) );
define( 'DB_USER', picopaas_env( 'WORDPRESS_DB_USER', 'wordpress' ) );
define( 'DB_PASSWORD', picopaas_env( 'WORDPRESS_DB_PASSWORD', '' ) );
// ${{ db.host }} and ${{ db.port }} are separate refs (a ref cannot be embedded in a longer string).
$picopaas_host = picopaas_env( 'WORDPRESS_DB_HOST', 'localhost' );
$picopaas_port = picopaas_env( 'WORDPRESS_DB_PORT' );
define( 'DB_HOST', $picopaas_port ? $picopaas_host . ':' . $picopaas_port : $picopaas_host );
define( 'DB_CHARSET', 'utf8mb4' );
define( 'DB_COLLATE', '' );
// Managed MySQL requires TLS (${{ db.tls }} = required). MYSQLI_CLIENT_SSL encrypts the connection; core WP has no hook
// to pass a CA file, so the server certificate is not verified.
if ( 'required' === picopaas_env( 'WORDPRESS_DB_TLS', 'off' ) ) {
	define( 'MYSQL_CLIENT_FLAGS', MYSQLI_CLIENT_SSL );
}
$table_prefix = picopaas_env( 'WORDPRESS_TABLE_PREFIX', 'wp_' );

// Auth keys/salts: eight secrets, all required (the entrypoint refuses to start without them).
foreach ( array( 'AUTH_KEY', 'SECURE_AUTH_KEY', 'LOGGED_IN_KEY', 'NONCE_KEY', 'AUTH_SALT', 'SECURE_AUTH_SALT', 'LOGGED_IN_SALT', 'NONCE_SALT' ) as $picopaas_name ) {
	$picopaas_val = picopaas_env( 'WORDPRESS_' . $picopaas_name );
	if ( null === $picopaas_val ) {
		http_response_code( 503 );
		die( 'WordPress is not configured: missing WORDPRESS_' . $picopaas_name );
	}
	define( $picopaas_name, $picopaas_val );
}

// TLS terminates at Traefik, which sends X-Forwarded-Proto.
if ( isset( $_SERVER['HTTP_X_FORWARDED_PROTO'] ) && 'https' === strtolower( $_SERVER['HTTP_X_FORWARDED_PROTO'] ) ) {
	$_SERVER['HTTPS'] = 'on';
}
// CLI (wp-cron loop) has no Host header.
if ( 'cli' === PHP_SAPI && empty( $_SERVER['HTTP_HOST'] ) ) {
	$_SERVER['HTTP_HOST'] = picopaas_env( 'WP_URL' ) ? (string) parse_url( picopaas_env( 'WP_URL' ), PHP_URL_HOST ) : 'localhost';
}
// Pin the public URL (WP_URL = ${{ app.url }} or the custom domain).
if ( picopaas_env( 'WP_URL' ) ) {
	define( 'WP_HOME', rtrim( picopaas_env( 'WP_URL' ), '/' ) );
	define( 'WP_SITEURL', rtrim( picopaas_env( 'WP_URL' ), '/' ) );
}

// wp-cron runs from a CLI loop in the entrypoint, not on page loads.
define( 'DISABLE_WP_CRON', true );
define( 'DISALLOW_FILE_EDIT', true );
define( 'FS_METHOD', 'direct' );
// Background updates (core/plugins/themes via cron) are off by default: a restart in the middle of one leaves .maintenance behind
// (visitors get 'Briefly unavailable' for up to 10 minutes). Update from the dashboard, or set WORDPRESS_AUTOMATIC_UPDATES=true.
define( 'AUTOMATIC_UPDATER_DISABLED', 'true' !== picopaas_env( 'WORDPRESS_AUTOMATIC_UPDATES', 'false' ) );
define( 'WP_AUTO_UPDATE_CORE', 'true' === picopaas_env( 'WORDPRESS_AUTO_UPDATE_CORE', 'false' ) ? true : false );
define( 'WP_TEMP_DIR', '/tmp' );
define( 'WP_DEBUG', 'true' === picopaas_env( 'WORDPRESS_DEBUG', 'false' ) );
define( 'WP_DEBUG_LOG', false );
define( 'WP_DEBUG_DISPLAY', false );
define( 'WP_MEMORY_LIMIT', '128M' );
define( 'WP_MAX_MEMORY_LIMIT', '192M' );
define( 'EMPTY_TRASH_DAYS', 14 );
define( 'WP_POST_REVISIONS', 10 );

if ( ! defined( 'ABSPATH' ) ) {
	define( 'ABSPATH', __DIR__ . '/' );
}
require_once ABSPATH . 'wp-settings.php';
