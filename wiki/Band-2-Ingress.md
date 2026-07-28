« [Home](Home.md)

# Band 2 — Ingress, DDoS, TLS

**Function:** absorb hostile/volumetric traffic and terminate client TLS before anything else sees the
request. Performed internally by [agentgateway](agentgateway.md) (its "frontend policies" stage) plus one
component with no software equivalent.

## Components

### Cloudflare / Fastly

GeoDNS + anycast, volumetric DDoS scrubbing, WAF, bot management, client TLS.

- ⚠ **Disable buffering and compression on `text/event-stream`.** Getting this wrong silently breaks
  streaming for every customer behind it — see
  [Non-Negotiable Constraints](Non-Negotiable-Constraints.md).
- ⚠ **Cannot inspect streaming response bodies.** Output guardrails must happen in
  [Band 4 / agentgateway](agentgateway.md), not here. Cloudflare protects the portal/webhooks/docs
  surface normally.
- **This is the one part of the stack with no self-host answer.** You cannot absorb 500 Gbps on your own
  transit — this is why [D7](Decision-Log-Index.md#d7) keeps Cloudflare even while dropping the dedicated
  Envoy edge tier.

### MetalLB + BGP

*(Apache 2.0)* Nodes advertise a VIP; datacenter routers ECMP-hash flows across them. No physical load
balancer required — the routers *are* the balancer.

- ⚠ **Critical gotcha:** ECMP re-hashes flows whenever the set of advertising nodes changes, which kills live streams. **Ask the network team for resilient/consistent hashing on the top-of-rack switches.**
  Without it, every edge deploy is a customer-visible incident. See
  [D8](Decision-Log-Index.md#d8).
- **Rejected:** hardware LB appliance (F5, etc.) — licence cost, throughput ceiling, separate change
  process. Only justified if we already own F5s or compliance mandates a certified appliance.
- **Rejected:** MetalLB L2 mode — all traffic lands on one node.
- **Alternative:** Cilium BGP control plane if Cilium is our CNI (one fewer component), or kube-vip.

## Related

- [agentgateway](agentgateway.md) — performs this band's function internally as "frontend policies"
- [D7 — Drop the dedicated Envoy edge tier](Decision-Log-Index.md#d7)
- [D8 — Router-based load balancing](Decision-Log-Index.md#d8)
