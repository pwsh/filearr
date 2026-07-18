# Filearr — custom Caddy image (OPS-T1 acme-dns rework / P5-T6 agent-plane mTLS).
#
# The stock caddy:2.11.4 image has neither the Cloudflare DNS-01 solver nor the
# layer4 (raw-TCP/SNI) app, both of which this deploy needs:
#   * caddy-dns/cloudflare — wildcard cert for *.<domain> via DNS-01 (no inbound
#     :80 / HTTP-01; works behind NAT, no public reachability on 80 required).
#   * mholt/caddy-l4        — SNI-based L4 passthrough so the CA hostname's TLS is
#     forwarded RAW to step-ca (it keeps its own TLS; mTLS /renew must NOT be
#     L7-terminated — docs/ops/agents.md §7.4). Coexists with the HTTP app on 443
#     via the `layer4` listener wrapper (see Caddyfile.acme).
#
# Pins (re-verify BOTH plugin versions AND the caddy CVE list on any bump, same
# discipline as the compose caddy/meili/step-ca pins):
#   caddy                        v2.11.4   (matches the runtime base image below)
#   github.com/caddy-dns/cloudflare  v0.2.4   (proxy.golang.org @latest 2026-07-17)
#   github.com/mholt/caddy-l4        v0.1.2   (proxy.golang.org @latest 2026-07-17)
FROM caddy:2.11.4-builder AS builder
RUN xcaddy build v2.11.4 \
    --with github.com/caddy-dns/cloudflare@v0.2.4 \
    --with github.com/mholt/caddy-l4@v0.1.2

FROM caddy:2.11.4
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
