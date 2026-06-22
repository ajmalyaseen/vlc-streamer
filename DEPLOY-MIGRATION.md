# Migrating the bot to a new GCP region

GCP can't change a running VM's region, so you create a **new instance** in the
target region and move the deployment to it. Everything is in Docker + git, so
the migration is mostly clone + copy `.env` + bring up.

Recommended region for an India audience pulling mostly DC4 (Amsterdam) content:
- **`me-central1`** (Doha) — balanced DC4/DC5 fetch, close to India. Best all-rounder.
- **`asia-south1`** (Mumbai) — best for users; DC4 fetch a bit slower than Doha.

> Tip: test both with the speed test (see end) before committing.

---

## 1. Create the new VM

Console: Compute Engine → Create instance
- Region: `me-central1` (or `asia-south1`)
- Machine type: same as current (match your existing VM)
- Boot disk: Ubuntu 24.04 LTS
- Firewall: check **Allow HTTP** and **Allow HTTPS**
- (Recommended) Network → reserve a **static external IP** so DNS stays stable

Or via gcloud (adjust name/type/zone):
```bash
gcloud compute instances create alaska-stream-eu \
  --zone=me-central1-a \
  --machine-type=e2-small \
  --image-family=ubuntu-2404-lts-amd64 \
  --image-project=ubuntu-os-cloud \
  --tags=http-server,https-server
```

## 2. Install Docker on the new VM

SSH in, then:
```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-plugin git
sudo usermod -aG docker $USER
# log out and back in so the docker group applies
```

## 3. Get the code + config

```bash
git clone https://github.com/ajmalyaseen/vlc-streamer.git
cd vlc-streamer
```

Copy your `.env` from the OLD VM to the new one. Easiest: open the old `.env`,
copy its contents, and paste into a new `.env` here:
```bash
nano .env   # paste your existing values, save
```
`.env` is gitignored, so it is NOT in the repo — you must move it manually.

## 4. Point your domain at the new VM

- Update your DNS **A record** for your domain to the new VM's external IP.
- `BASE_URL` and `DOMAIN` in `.env` stay the same (same domain).
- Wait for DNS to propagate (a few minutes). Caddy will auto-issue a fresh TLS
  cert for the domain on the new VM.

## 5. Bring it up

```bash
docker compose up -d --build
docker compose logs -f bot
```

Confirm:
```bash
docker compose exec bot python -c "import pyrogram; print(pyrogram.__version__)"
```

## 6. Verify speed improved

Stream a DC4 file and check the fetch speed:
```bash
docker compose logs --tail=300 bot | grep tg-fetch
```
DC4 lines should now show much higher MiB/s and lower first-chunk ms than on Singapore.

## 7. Decommission the old VM

Once the new VM is serving traffic correctly and DNS has switched:
```bash
# from the old VM region
gcloud compute instances delete <old-instance-name> --zone=<old-zone>
```
(Release the old static IP too, if you had one, to avoid charges.)

---

## Optional: quick A/B speed test before fully migrating

On each candidate VM (after step 5), stream the **same** DC4 and DC5 files and
compare:
```bash
docker compose logs --tail=300 bot | grep tg-fetch
```
Pick the region with the best DC4 MiB/s + lowest first-chunk, since DC4 is ~78%
of your traffic.

## Notes
- The MongoDB (`DATABASE_URL`) is external (Atlas), so user/payment data follows
  automatically — nothing to migrate there. If you were on in-memory storage,
  data resets on the new VM (set `DATABASE_URL` to persist).
- The weekly backup keeps working; it just needs `LOG_CHANNEL` set as before.
- No code changes are needed to move regions — only the VM + DNS.
