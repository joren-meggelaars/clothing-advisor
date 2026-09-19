# Deploying Clothing Advisor

Target: the Docker VM (same stack style as the other services), reverse-proxied by Nginx Proxy Manager.

## 1. On the Docker VM

```bash
git clone https://github.com/joren-meggelaars/clothing-advisor.git
cd clothing-advisor
cp .env.example .env
mkdir -p data && chown "$(id -u):$(id -g)" data     # PUID/PGID in .env must match this user
openssl rand -hex 24                                # -> CA_ACCESS_TOKEN
nano .env                                           # fill in the required values
docker compose up -d --build
docker compose logs -f
curl -s http://localhost:8080/healthz               # {"ok":true}
```

Set `BIND_ADDRESS` to the VM IP (or `0.0.0.0`) so Nginx Proxy Manager, on another VM, can reach port 8080.
Remember to write every literal `$` in `.env` values as `$$`.

## 2. Nginx Proxy Manager

Proxy Host `ca.<your domain>` -> scheme **http**, forward host = VM IP, port 8080, SSL certificate with "Force SSL".
No websockets needed. Set `PUBLIC_BASE_URL=https://ca.<your domain>` in `.env` and `HA_ORIGIN` to your HA https origin.
Note: the proxy's access log records URLs including `?t=<token>`; after the first load the app uses a cookie instead.

## 3. Home Assistant

1. Create a long-lived access token (profile -> Security) and put it in `.env` as `HA_TOKEN`, plus `HA_URL`.
2. Find the weather entity (Developer tools -> States, filter `weather.`) and set `HA_WEATHER_ENTITY` or enter it in
   the app under Settings, then press "Test weather".
3. Find a notify service for your phone (Developer tools -> Actions, search `notify.mobile_app`) and set
   `HA_NOTIFY_SERVICE`, then press "Send test notification" on the Costs page.
4. Add [ha/configuration.yaml](ha/configuration.yaml) (REST sensor + recorder exclude) and the secrets it mentions.
5. Add the cards: [ha/phone-card.yaml](ha/phone-card.yaml) on the phone view, [ha/tv-card.yaml](ha/tv-card.yaml) in the TV
   kiosk's sections view.

## 4. First use

Open the app from the phone card -> *Add*: pick photos (camera or gallery) -> confirm the cost estimate -> wait for the
queue -> *Review*, correct and approve -> *Advice*. To load many photos at once, copy them to `data/inbox/` on the VM
and press "Import inbox".

## Operations

- Update: `git pull && docker compose up -d --build`.
- Back up `data/` (SQLite `clothing.db` plus photos). Stop the container first, or copy while it is idle.
- Rotate the access token: change `CA_ACCESS_TOKEN`, recreate the container, update the two HA places.
- Photos are sent to the Anthropic API when they are catalogued; advice requests send text only.
