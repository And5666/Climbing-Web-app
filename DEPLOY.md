# Deploying summit_map

One container on your laptop, SQLite + avatars in a Docker volume,
Cloudflare Tunnel in front at `https://summit.andw.net`. No ports are
exposed to your LAN or the internet — the tunnel dials out to
Cloudflare and serves the site from there.

## 1. Prereqs (on the laptop)

- Docker Engine + the Compose plugin (`docker compose version` works).
- `cloudflared` logged in (`cloudflared tunnel login`) with `andw.net`
  on your account.

## 2. Configure

```sh
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

Put the generated string in `.env` as `DJANGO_SECRET_KEY`. The
defaults already point at `summit.andw.net` and keep the database at
`/data/db.sqlite3` inside the `summit-data` volume.

## 3. Build and start

```sh
docker compose build
docker compose up -d
docker compose logs -f   # Ctrl-C once you see "Booting worker"
```

Migrations run automatically on every start. Then create your login
(the live database starts empty):

```sh
docker compose exec web python manage.py createsuperuser
```

Use username `andre`. Check `http://localhost:8000` on the laptop.

## 4. Tunnel + DNS

Create the tunnel once:

```sh
cloudflared tunnel create summit-laptop
```

Route DNS to it:

```sh
cloudflared tunnel route dns summit-laptop summit.andw.net
```

Point the tunnel at the container (`~/.cloudflared/config.yml`):

```yaml
tunnel: summit-laptop
credentials-file: /home/andre/.cloudflared/summit-laptop.json
ingress:
  - hostname: summit.andw.net
    service: http://localhost:8000
  - service: http_status:404
```

Run it (and keep it running — a systemd user service is ideal):

```sh
cloudflared tunnel run summit-laptop
```

In the Cloudflare dashboard for `andw.net`: SSL/TLS mode **Full**
(not Strict — there is no origin certificate; the tunnel itself is
encrypted), and leave "Always Use HTTPS" on. The app already sends
session cookies secure-only and trusts the tunnel's forwarded proto.

## 5. Verify

- `https://summit.andw.net` loads, no CSRF errors when logging in
  (a 403 here means `DJANGO_CSRF_ORIGINS` doesn't match the URL).
- Log in as `andre`, upload a picture, log a send.
- Sanity check Django itself passes:
  `docker compose exec web python manage.py check --deploy`

## 6. Updating

```sh
git pull
docker compose build
docker compose up -d
```

Migrations apply on start; the volume keeps every climb and picture.

## 7. Backup and restore

Back up (database + avatars, one file):

```sh
mkdir -p backups
docker compose exec web tar -czf /tmp/summit-backup.tgz -C /data .
docker cp "$(docker compose ps -q web):/tmp/summit-backup.tgz" \
  "backups/summit-$(date +%F).tgz"
```

Restore:

```sh
docker compose down
docker volume rm summit_map_summit-data
docker volume create summit_map_summit-data
docker run --rm -v summit_map_summit-data:/data \
  -v "$(pwd)/backups:/b" alpine \
  tar -xzf "/b/summit-<date>.tgz" -C /data
docker compose up -d
```

(If your volume is named differently, `docker volume ls` will show it.)

## 8. Troubleshooting

- `DisallowedHost` → `DJANGO_ALLOWED_HOSTS` in `.env` must contain
  the host you open (after edits: `docker compose up -d` to recreate).
- Login form 403 "CSRF verification failed" → you opened the site over
  plain http, or `DJANGO_CSRF_ORIGINS` lacks the exact `https://` URL.
- Tunnel serves a Cloudflare error page → `cloudflared` isn't running
  or the container isn't (`docker compose ps`, `docker compose logs`).
- Registration is open to anyone with the URL. If that ever bothers
  you, front the hostname with Cloudflare Access (Zero Trust) and only
  your email gets in.
