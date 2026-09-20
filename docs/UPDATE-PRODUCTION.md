# Updating the live VPS after pushing to `ayush-bhai-branch`

This is the exact routine to make changes you've **pushed from your PC** go **live
on the VPS** (`oialgo.tech`, IP `187.127.206.41`). It's tailored to this server:
repo at `/root/nifty-oi`, Docker Compose stack, Caddy providing HTTPS on the host.

---

## The workflow in one sentence

**On your PC:** edit → commit → push to `ayush-bhai-branch`.
**On the VPS:** pull → rebuild. Done.

---

## 0. One-time setup (run ONCE, ever — skip if you've already done it)

Your server has **one file customized for this machine**: `docker/docker-compose.yml`
(the frontend is remapped to `127.0.0.1:8080` so Caddy can own ports 80/443 for
HTTPS). Tell git to leave that local file alone, so pulls never conflict with it:

```bash
cd /root/nifty-oi
git update-index --skip-worktree docker/docker-compose.yml
```

You only ever run this once. After it, `git pull` will never complain about that file.

---

## 1. On your PC — push your change (you already know this)

```powershell
cd "D:\trading algo (ayush bhai)\trading algo (ayush bhai)"
git add -A
git commit -m "describe your change"
git push origin ayush-bhai-branch
```

---

## 2. On the VPS — pull + rebuild (the part you paste each time)

Connect:
```bash
ssh root@187.127.206.41
```
Then paste this **one block** — it pulls your pushed code, rebuilds the images,
restarts only the changed containers, and auto-runs any database migrations:
```bash
cd /root/nifty-oi && \
git pull origin ayush-bhai-branch && \
docker compose --env-file .env -f docker/docker-compose.yml up -d --build
```

That's the whole update. Caddy / HTTPS / your `.env` are all untouched.

> **Faster option** when you changed only ONE side:
> - Backend only: append ` backend` → `... up -d --build backend`
> - Frontend only: append ` frontend` → `... up -d --build frontend`

---

## 3. Verify it went live

```bash
curl -s http://localhost/api/health
docker compose --env-file .env -f docker/docker-compose.yml ps
```
- `authenticated: true` and (during market hours) `feed_connected: true`.
- All three containers `Up` / `healthy`.

Then open **https://oialgo.tech** and **hard-refresh** with `Ctrl+Shift+R` (bypasses
the browser cache so you actually see the new frontend).

---

## 4. If something breaks — roll back in 20 seconds

```bash
cd /root/nifty-oi
git log --oneline -5                 # find the previous good commit hash
git reset --hard <that-hash>
docker compose --env-file .env -f docker/docker-compose.yml up -d --build
```

---

## Notes & gotchas

- **`.env` is never touched by a pull** (it's git-ignored). Your broker keys,
  `API_CORS_ORIGINS`, and `STRIKE_WINDOW=11` stay exactly as you set them.
- **DB migrations run automatically** on backend startup (the compose `command`
  runs `alembic upgrade head`), so there's no separate migration step.
- **Keep the PC stack stopped** while the VPS runs — the broker allows only one
  market-data session per appKey. (`docker compose ... down` on the PC.)
- **Brief blip:** rebuilding the backend re-logs in to the broker and reconnects
  the feed (~30s gap). Harmless; it self-restores.
- **`git pull` says "local changes to docker-compose.yml would be overwritten"?**
  That means you either skipped step 0, or an update genuinely changed that file.
  Fix:
  ```bash
  git stash
  git pull origin ayush-bhai-branch
  git stash pop
  ```
  Then confirm the frontend still reads `127.0.0.1:8080:80`:
  ```bash
  grep -A2 "frontend:" docker/docker-compose.yml | grep 8080 || nano docker/docker-compose.yml
  ```
  (If the `8080` line is gone, re-add it under the frontend service's `ports:` and
  recreate: `docker compose --env-file .env -f docker/docker-compose.yml up -d --force-recreate frontend`.)
- **You do NOT need to touch Caddy** for code updates — it just proxies. Only revisit
  Caddy if you change the domain.

---

## TL;DR card (pin this)

```bash
# PC:  git add -A && git commit -m "..." && git push origin ayush-bhai-branch
# VPS:
ssh root@187.127.206.41
cd /root/nifty-oi && git pull origin ayush-bhai-branch && \
docker compose --env-file .env -f docker/docker-compose.yml up -d --build
# then hard-refresh https://oialgo.tech  (Ctrl+Shift+R)
```
