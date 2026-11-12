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
ARG NGINX_VERSION=1.30.6

FROM nginx:${NGINX_VERSION}-alpine AS builder

ARG NGINX_VERSION
ARG VTS_VERSION=v0.2.7

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
# Answers `nginx -t` and `nginx -s reload` for the control plane, so that the
# management container does not need the Docker socket to reach Nginx. See the
# script for why that mattered.
# Deliberately no `apk upgrade` here, unlike the management image.
#
# The traffic-status module is compiled in the builder stage against this
# exact Nginx version. An upgrade that moves Nginx even a patch release
# makes the module binary-incompatible, and Nginx refuses to start with
# "module is not binary compatible" -- the gateway, not the control plane.
#
# So base-image CVEs here are fixed by raising NGINX_VERSION, which rebuilds
# the module against the same source. CI scans this image on every run,
# which is how we find out one is needed.
COPY reload-watcher.sh /usr/local/bin/cloudbalancer-reload-watcher.sh
COPY docker-entrypoint.d/40-cloudbalancer-reload-watcher.sh \
     /docker-entrypoint.d/40-cloudbalancer-reload-watcher.sh

# The control directory is a volume shared with the management plane, which
# runs as uid 10001 and has to write requests into it. A named volume takes
# its ownership from whichever image first provides the path -- if that is
# this one, the other side gets a directory it cannot write. So it is created
# owned by the side that cannot simply override permissions; Nginx reads and
# writes it as root either way.
RUN chmod +x /usr/local/bin/cloudbalancer-reload-watcher.sh \
             /docker-entrypoint.d/40-cloudbalancer-reload-watcher.sh \
    && mkdir -p /var/www/acme /var/cache/nginx /etc/nginx/ssl /etc/letsencrypt \
                /var/run/cloudbalancer/requests /var/run/cloudbalancer/results \
    && chown -R nginx:nginx /var/www/acme /var/cache/nginx \
    && chown -R 10001:10001 /var/run/cloudbalancer

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD nginx -t || exit 1
