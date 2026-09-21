# =============================================================================
# Nginx + the virtual host traffic status module.
#
# The stock image cannot report a cache hit ratio. `stub_status` exposes
# connection counts and a request total and nothing else -- no cache statuses,
# no per-vhost breakdown, no upstream timing. Since a cache hit ratio is the
# number this gateway exists to show, the module is compiled in here and
# Prometheus scrapes Nginx directly.
#
# Built with --with-compat so the module loads into the stock binary; only the
# module is compiled, not Nginx itself.
# =============================================================================
ARG NGINX_VERSION=1.27.4

FROM nginx:${NGINX_VERSION}-alpine AS builder

ARG NGINX_VERSION
ARG VTS_VERSION=v0.2.4

RUN apk add --no-cache \
        gcc libc-dev make linux-headers \
        openssl-dev pcre2-dev zlib-dev \
        git curl

WORKDIR /build

RUN curl -fsSL "https://nginx.org/download/nginx-${NGINX_VERSION}.tar.gz" | tar -xz \
    && git clone --depth 1 --branch "${VTS_VERSION}" \
        https://github.com/vozlt/nginx-module-vts.git vts

RUN cd "nginx-${NGINX_VERSION}" \
    && ./configure --with-compat --add-dynamic-module=../vts \
    && make modules

FROM nginx:${NGINX_VERSION}-alpine

COPY --from=builder \
    /build/nginx-*/objs/ngx_http_vhost_traffic_status_module.so \
    /etc/nginx/modules/

# Directories shared with the management container: the ACME challenge webroot,
# the cache the control panel purges from, and certbot's output.
RUN mkdir -p /var/www/acme /var/cache/nginx /etc/nginx/ssl /etc/letsencrypt \
    && chown -R nginx:nginx /var/www/acme /var/cache/nginx

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD nginx -t || exit 1
