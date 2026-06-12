# VPS provider pricing (research snapshot)

**Purpose:** Compare **monthly compute** cost for hosting this project’s **single-VPS + Docker Compose** stack (TimescaleDB + FastAPI + nginx frontend on one machine).

**Research date:** 13 May 2026 (web and vendor marketing pages). **Prices change** with promotions, VAT, currency, and region—always confirm on the live pricing page before you buy.

**Currency:** USD unless noted. EUR → USD uses an **illustrative** ~**1.09** FX rate for quick comparison only.

---

## Minimum specs to compare apples to apples

For this repo’s default compose profile, compare plans with roughly:

| Spec | Target |
|------|--------|
| RAM | **≥ 4 GB** |
| vCPU | **≥ 1** (2 preferred) |
| Disk | **≥ 40 GB** SSD/NVMe |
| Traffic | **≥ 1 TB/mo** (usually fine; XTS feed + users drive egress) |

---

## Hostinger (KVM VPS)

Official pricing hub: [Hostinger VPS hosting pricing](https://www.hostinger.com/pricing/vps-hosting)

Marketing copy on the VPS product page (May 2026 crawl) listed **KVM 1** as **1 vCPU, 4 GB RAM, 50 GB NVMe, 4 TB bandwidth**, with:

| Billing framing | KVM 1 (USD/mo) | KVM 2 (2 vCPU, 8 GB, 100 GB) |
|-----------------|----------------|-------------------------------|
| Promotional (e.g. longer-term intro) | **from ~6.49** | **from ~8.99** |
| Stated renewal (example: 2-year renewal text on same page) | **renews ~11.99** | **renews ~14.99** |

Higher tiers on the same page (for reference only—overkill for a first deploy):

| Plan | Stated renewal (USD/mo, from same source) |
|------|-------------------------------------------|
| KVM 4 | ~28.99 |
| KVM 8 | ~49.99 |

**Included value often advertised:** DDoS protection, snapshots/backups features vary by plan—read the **exact** plan footnotes for whether backups cover **Docker volumes** the way you need.

**Fit for this project:** **KVM 1** matches the 4 GB RAM floor; **KVM 2** adds comfort if you see OOM during heavy days.

---

## DigitalOcean (Droplets)

Product: [DigitalOcean Droplets](https://www.digitalocean.com/products/droplets) — calculator: [Pricing calculator](https://www.digitalocean.com/pricing/calculator)

For a **Basic shared-CPU** droplet in the **~4 GB RAM** class, third-party pricing mirrors and 2026 summaries commonly cite **~USD 24–27/mo** for **2 vCPU / 4 GB / ~80 GB SSD** with **4 TB** outbound included (verify exact slug on DO’s site).

**Fit:** Strong docs ecosystem; **Bangalore/Mumbai** regions exist if you want Indian egress proximity.

---

## Hetzner Cloud (developer-budget EU)

Product: [Hetzner Cloud](https://www.hetzner.com/cloud/) — cost-optimized line: [Cost-optimized servers](https://www.hetzner.com/cloud/cost-optimized)

Hetzner announced **price adjustments effective 1 April 2026** (press/docs). Post-adjustment references for **CX23-class** (2 shared vCPU, **4 GB** RAM, **40 GB** disk) often cite **~€3.99/mo** list; **primary IPv4** is sometimes billed **~€0.50/mo** extra depending on how you configure networking.

**Illustrative USD (not a quote):** €3.99 × 1.09 ≈ **USD 4.35/mo** + possible IPv4 fee.

**Caveat:** Datacenters are **primarily EU** (latency to India may be higher than DO Mumbai); check **compliance and payment** options for your jurisdiction.

---

## AWS Lightsail (simplified AWS)

Pricing hub: [Amazon Lightsail pricing](https://aws.amazon.com/lightsail/pricing/)

Commonly cited bundle for **4 GB RAM / 2 vCPU / ~80 GB / 4 TB transfer**: **~USD 24/mo** with public IPv4. Some summaries mention **~USD 20/mo** for **IPv6-only** bundles—only choose that if every client path supports IPv6.

**Fit:** Predictable bundle billing inside AWS; good if you already live in AWS.

---

## Summary table (ballpark monthly)

| Provider | Plan / class | Specs (typical) | Ballpark monthly |
|----------|----------------|-----------------|-------------------|
| **Hostinger** | KVM 1 | 1 vCPU, 4 GB, 50 GB | **~USD 6–12** (intro vs renewal) |
| **Hostinger** | KVM 2 | 2 vCPU, 8 GB, 100 GB | **~USD 9–15** (intro vs renewal) |
| **Hetzner** | CX23-class | 2 vCPU, 4 GB, 40 GB | **~USD 4–6** (EUR list + IPv4 extras) |
| **DigitalOcean** | Basic ~4 GB | 2 vCPU, 4 GB, ~80 GB | **~USD 24–27** |
| **AWS Lightsail** | ~4 GB bundle | 2 vCPU, 4 GB, ~80 GB | **~USD 20–24** |

---

## Total cost of ownership (beyond VPS)

| Item | Typical range |
|------|----------------|
| Domain (optional) | **~USD 10–15 / year** |
| Backup object storage | **~USD 0–5 / month** at small volume |
| Extra snapshots (some hosts bill per snapshot GB) | **~USD 0–5 / month** |
| Monitoring (e.g. HTTP health checks) | Often **USD 0** on free tiers |

### Using the VPS without a domain

Yes. You can use **only the server’s public IP**: open `http://YOUR_IP` in the browser; the bundled Docker nginx still proxies `/api/` and `/ws/` to the backend, so the app works the same from a URL perspective.

Trade-offs:

- **HTTPS:** free certificates (e.g. Let’s Encrypt) usually need a **hostname**, so IP-only setups often stay on **HTTP** for private/testing use, or you use a provider-managed load balancer / paid IP certificate (less common for hobby setups).
- **CORS:** set `API_CORS_ORIGINS` to include your IP origin, e.g. `http://203.0.113.10` (and `http://203.0.113.10:80` if you ever expose a non-default port), or tighten later when you add a domain.

A domain is a polish and security convenience, not a requirement for the VPS bill or for running the stack.

---

## Suggested decision

- **Lowest headline monthly:** compare **Hetzner** vs **Hostinger KVM 1** using **renewal** price, not intro-only.
- **Lowest friction in India + English docs:** compare **DigitalOcean** vs **Lightsail** using the **same RAM/disk** row.
- **Always:** confirm **backup scope**, **snapshot** pricing, and whether **port 25** / SMTP is blocked (irrelevant for this app but common on VPS).

---

## Next step

Follow [`VPS-DEPLOY-PLAN.md`](VPS-DEPLOY-PLAN.md) after you purchase the VPS.
