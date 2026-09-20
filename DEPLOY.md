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
No websockets needed. Under the host's **Advanced** tab add:

```
client_max_body_size 30m;   # phone photos; without it uploads fail with "Load failed" / HTTP 413
proxy_read_timeout 180s;    # an advice request with thinking can take over a minute
proxy_send_timeout 180s;
```

 Set `PUBLIC_BASE_URL=https://ca.<your domain>` in `.env` and `HA_ORIGIN` to your HA https origin.
Note: the proxy's access log records URLs including `?t=<token>`; after the first load the app uses a cookie instead.

**Home only.** The app is meant to be reachable from the home network only:

1. NPM -> Access Lists -> new list "Home": *Satisfy Any*, **no** username/password (Basic Auth uses the same
   `Authorization` header as the app's Bearer token and would break the Home Assistant sensor), and under Rules
   *allow* the subnets of your phone, tv and pc (for example `10.0.10.0/24`) followed by *deny all*.
2. Assign the list to the Proxy Host.
3. Check which client IP NPM sees for your devices in `/data/logs/proxy-host-<n>_access.log` on the NPM VM (a LAN
   address when internal DNS points `ca.<domain>` at NPM, otherwise your public IP or the router) and allow that.
4. Test: phone on wifi works, phone on mobile data gets 403.

The Home Assistant sensor does not go through the proxy at all (see step 3.4).

## 3. Home Assistant

1. Create a long-lived access token (profile -> Security) and put it in `.env` as `HA_TOKEN`, plus `HA_URL`.
2. Find the weather entity (Developer tools -> States, filter `weather.`) and set `HA_WEATHER_ENTITY` or enter it in
   the app under Settings, then press "Test weather".
3. Find a notify service for your phone (Developer tools -> Actions, search `notify.mobile_app`) and set
   `HA_NOTIFY_SERVICE`, then press "Send test notification" on the Costs page.
4. Add [ha/configuration.yaml](ha/configuration.yaml) (REST sensor + recorder exclude) and the secrets it mentions.
   Point the sensor at the VM directly (`http://<VM-IP>:8080/api/tv`): server to server, no https and no proxy
   rules involved. Only the browsers (phone iframe, tv images) use the https name.
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
