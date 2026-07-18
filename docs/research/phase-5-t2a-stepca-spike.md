# P5-T2a — step-ca direct-integration spike (Sonnet, 2026-07-16)
Gates P5-T2. Orchestrator-approved verdicts. Full detail in the agent
report (summarized here as the implementation contract).

## Verdicts
1. **Token→cert seam:** central brokers a scoped **JWK one-time token
   (OTT)** — signed server-side with the provisioner private JWK,
   `sub`=agent_id, `aud`=CA /1.0/sign, short exp, unique jti — returned as
   a NEW `ca_ott` field on RegisterOut. Agent exchanges it DIRECTLY with
   step-ca; keys never leave the agent; no CSR proxying through central.
   REJECTED: central CSR relay (same key custody, more code, no benefit).
2. **Protocol:** JWK provisioner **/1.0/sign** (not ACME: http-01/dns-01/
   tls-alpn need reachability; device-attest-01 needs hardware attestation
   + lego doesn't support it — issue #2257).
3. **Go client:** `github.com/smallstep/certificates/ca` **v0.30.2**
   (matches server pin; Apache-2.0). CreateSignRequest → Sign →
   RenewWithContext → Roots. REJECTED: hand-rolled JOSE/CSR, lego, step
   CLI (R6).
4. **Renewal:** ~2/3-lifetime timer + jitter + retry via cert-authenticated
   /renew (mTLS with the cert being renewed — CONFIRMED, no new OTT).
   CA rotation → client.Roots() on the config-push signal. Provisioner
   claims: min 24h / default 48h / max 72h TLS cert duration,
   `allowRenewalAfterExpiry: true` as BOUNDED grace for long-offline
   agents (+ server-side alert when the grace path is used).
5. **Revocation:** step-ca OSS = passive only (CRL/OCSP is Pro). Two-layer
   enforcement stands: CA-side revoke blocks renewal + the shipped
   app-layer `agents.revoked_at` denylist checked on every request.
6. **Pins:** step-ca 0.30.2 current + patched (CVE-2026-30836 SCEP
   critical fixed in 0.30.0; we configure NO SCEP provisioner). Go:
   smallstep/certificates v0.30.2, go.step.sm/crypto transitive (v0.85.x).

## Deployment note (external proxy)
The user fronts Filearr at https://filearr.example.com via an external
HTTPS proxy. **The CA must NOT be L7-proxied**: /renew authenticates via
the client cert on the direct TLS connection; L7 termination silently
breaks it. Route a dedicated hostname (e.g. ca.filearr.example.com) as
**SNI-based L4/TCP passthrough** to step-ca:9000 (Caddy layer4 / nginx
stream). /1.0/sign alone would survive L7, /renew will not.

## New central requirements for P5-T2
- Central holds the provisioner's decrypted private JWK
  (FILEARR_CA_PROVISIONER_JWK-class secret; env acceptable for the
  single-operator model, document rotation).
- RegisterOut gains `ca_ott`; OTT mint audited via security_events.
- VERIFY at implementation time that step-ca enforces CSR CN/SAN ==
  OTT sub/sans (documented behavior; re-confirm, don't assume).

## Open risks carried into P5-T2/T5
- Long-offline agents past cert expiry: bounded allowRenewalAfterExpiry
  now; a proper re-enrollment path preserving agent_id/replication
  continuity is a P5-T5 question.
- Clock-skew tolerance on OTT//renew unverified — check during T2.

## P5-T2 build order
1) central: mint+return ca_ott (+ secret config + claim-shape tests);
2) agent/go.mod + internal/enroll (CreateSignRequest→Sign→persist→bind);
3) renewal daemon (2/3 timer, jitter, backoff, Roots on rotation signal);
4) negative paths: unregistered no-cert, revoked/expired no-renew,
   replayed OTT rejected.
CI: real step-ca 0.30.2 container (DOCKER_STEPCA_INIT_* pattern from
compose) for protocol behavior; go.step.sm/crypto/minica for pure-Go
unit tests.
