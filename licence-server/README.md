# Licence server

Issues and revokes commercial licences for `valheim-proxmox`. One file, one
container, no database — a signing key, a list of what was issued and a list of
what stopped being valid.

**It is not a gatekeeper.** A panel with no licence runs exactly like a panel
with one; the key only decides whether the install calls itself noncommercial or
names the company that paid. There is deliberately no kill switch — the reasoning
is at the bottom of this file.

---

## What a licence is

Two parts separated by a dot: the readable claim, and an Ed25519 signature over it.

```
eyJleHBpcmVzIjoxODE2ODQzNTY1LCJob2xkZXIiOiJGaXJtYSB6IG8uby4iLCJpZCI6ImxpY18…  .  T3gW0Wa01lcLuz9kRWcDDl1ZPe_NdQFVXJBC7AmIdFVYfeTABusXsD8PX9lNhuQxJM5_HA8JnA…
└──────────────────────── what it says ────────────────────────┘   └──────── proof the author said it ────────┘
```

The readable half carries `id`, `holder`, `scope`, `issued`, `expires` and
`product`. Anyone can decode it — there is nothing secret in a licence. What
cannot be done without the private key is producing a *different* claim that
still verifies.

The panel checks the signature against a public key **compiled into
`panel/licence.py`**, using an Ed25519 implementation written out in plain Python
so no install has to carry a crypto dependency for one check a day. No activation
call, no phone-home: a panel with no internet verifies a licence just as well.

## Endpoints

| Method | Path | Auth | What it does |
|---|---|---|---|
| `GET` | `/pubkey` | — | the public key panels verify against |
| `GET` | `/revoked` | — | ids that stopped counting as licensed |
| `GET` | `/health` | — | liveness, plus how many issued and revoked |
| `POST` | `/issue` | admin | signs a new licence, returns the key |
| `GET` | `/issued` | admin | everything ever signed (without the keys) |
| `POST` | `/revoke` | admin | adds an id to the revocation list |
| `GET` | `/` | — | the page used to issue and revoke |

Admin calls carry `Authorization: Bearer <ADMIN_TOKEN>`. The token lives in
`admin.env` next to the app.

```bash
curl -s -X POST https://lic.example.com/issue \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"holder":"Firma z o.o.","months":12,"note":"umowa 2026/03"}'
```

`months: 0` issues a perpetual licence.

## Install

Anywhere with Python 3.11+ — a 1 GB container is plenty.

```bash
apt-get install -y python3-venv curl
mkdir -p /opt/licence && cd /opt/licence
python3 -m venv .venv
.venv/bin/pip install fastapi uvicorn cryptography
cp app.py /opt/licence/app.py
printf 'ADMIN_TOKEN=%s\n' "$(openssl rand -hex 24)" > admin.env && chmod 600 admin.env
```

```ini
# /etc/systemd/system/licence.service
[Unit]
Description=Licence server
After=network.target

[Service]
WorkingDirectory=/opt/licence
ExecStart=/opt/licence/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 2470
Restart=always
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/licence
ProtectHome=true

[Install]
WantedBy=multi-user.target
```

`systemctl enable --now licence`. The signing key is generated on first use;
`curl localhost:2470/pubkey` prints the public half — **put that value into
`panel/licence.py` as `PUBLIC_KEY_HEX`** before shipping panels, or nothing you
sign will verify.

Put it behind a reverse proxy restricted to your own network. Customers never
talk to this service.

## Files

| Path | Contents |
|---|---|
| `signing.key` | the private key (600). **The only irreplaceable file here** |
| `admin.env` | `ADMIN_TOKEN=…` (600) |
| `issued.json` | every licence ever signed, keys included |
| `revoked.json` | revoked ids, as this server knows them |

Back the first two up somewhere that is not this machine. Losing `signing.key`
does not break licences already issued — panels verify those with the public key —
but no new one can ever be signed again, and every deployed panel would need a new
public key baked in.

## Revoking, and how it reaches a customer

Revoking here writes to `revoked.json` **on this server**, which a customer's
panel cannot read: this box sits on a private network, as it should. Publish the
list somewhere public and point panels at it — this project uses a static file on
its GitHub Pages site, referenced by `REVOKED_URL` in `panel/licence.py`.

So revoking is two steps: the button on this server, and adding the same id to the
published file.

Panels fetch that list at most once a day and cache it. **A failed fetch changes
nothing** — a licence server outage, a GitHub outage or a customer's broken DNS
must never turn a paying customer into an unlicensed one.

## Why there is no remote kill switch

The panel is source-available Python running on the customer's own machine.
Anyone intent on using it commercially without paying would delete the check in
minutes — so a kill switch would only ever fire on the honest, while the intended
target removes it. Worse, any check that fails *closed* eventually takes down a
paying customer's game server because of an outage on the author's side.

The enforcement here is legal, not technical: PolyForm Noncommercial makes
unlicensed commercial use an infringement, with 32 days to come into compliance
after written notice. A signed key makes "we are licensed" a checkable claim
instead of a sentence, and that is all it is meant to do.

---

Contact: **pawel@howtodev.it** ·
[project page](https://pawelszymanski89.github.io/valheim-proxmox/)
