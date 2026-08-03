"""Licence server for valheim-proxmox.

Issues signed licence keys and publishes what has been revoked. It never blocks
anything by itself: a panel with no key runs exactly like a panel with one, it
just says "noncommercial" instead of naming the company that paid. The signature
is what makes a claim of "we have a licence" checkable, and that is the whole
job here.

The private key lives on this machine and nowhere else. Panels only ever need
the public key, which is why /pubkey and /revoked are open while issuing is not.
"""
import json
import os
import secrets
import time
from base64 import urlsafe_b64encode
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)
from cryptography.hazmat.primitives import serialization
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

HERE = Path("/opt/licence")
KEY = HERE / "signing.key"          # private, 600, never leaves this box
ISSUED = HERE / "issued.json"       # every licence ever signed
REVOKED = HERE / "revoked.json"     # ids that must stop counting as licensed
ADMIN_ENV = HERE / "admin.env"      # ADMIN_TOKEN=...

app = FastAPI(title="valheim-proxmox licences", docs_url=None, redoc_url=None)


def _priv() -> Ed25519PrivateKey:
    if not KEY.exists():
        k = Ed25519PrivateKey.generate()
        KEY.write_bytes(k.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption()))
        KEY.chmod(0o600)
    return Ed25519PrivateKey.from_private_bytes(KEY.read_bytes())


def _pub_hex() -> str:
    return _priv().public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw).hex()


def _load(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _admin(request: Request):
    want = ""
    try:
        for line in ADMIN_ENV.read_text().splitlines():
            if line.startswith("ADMIN_TOKEN="):
                want = line.split("=", 1)[1].strip().strip("'\"")
    except Exception:
        pass
    got = (request.headers.get("authorization", "")
           .removeprefix("Bearer ").strip())
    if not want or not secrets.compare_digest(got, want):
        raise HTTPException(401, "Bad or missing admin token")


def _sign(payload: dict) -> str:
    """A licence is its own proof: the readable part and a signature over it,
    so a panel can check it with nothing but the public key - no network, no
    call home, works on a box with no internet at all."""
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    sig = _priv().sign(body)
    return (urlsafe_b64encode(body).decode().rstrip("=") + "."
            + urlsafe_b64encode(sig).decode().rstrip("="))


@app.get("/pubkey", response_class=PlainTextResponse)
def pubkey():
    """The key every panel needs to check a licence. Public on purpose."""
    return _pub_hex() + "\n"


@app.get("/revoked")
def revoked():
    """Licences that stopped being valid - a customer who stopped paying, or a
    key that leaked. A panel reads this at most once a day and, when it cannot,
    keeps the licence it has: a licence server being down must never turn a
    paying customer's install into an unlicensed one."""
    return {"revoked": _load(REVOKED, [])}


@app.post("/issue")
def issue(request: Request, body: dict = Body(...)):
    _admin(request)
    holder = str(body.get("holder", "")).strip()
    if not holder:
        raise HTTPException(400, "holder is required")
    months = int(body.get("months", 12))
    lic = {
        "id": "lic_" + secrets.token_hex(6),
        "holder": holder,
        "scope": str(body.get("scope", "commercial")),
        "note": str(body.get("note", ""))[:200],
        "issued": int(time.time()),
        "expires": int(time.time()) + months * 30 * 86400 if months else 0,
        "product": "valheim-proxmox",
    }
    token = _sign(lic)
    issued = _load(ISSUED, [])
    issued.append({**lic, "token": token})
    ISSUED.write_text(json.dumps(issued, indent=1))
    return {**lic, "token": token}


@app.get("/issued")
def issued(request: Request):
    _admin(request)
    return {"issued": [{k: v for k, v in x.items() if k != "token"}
                       for x in _load(ISSUED, [])]}


@app.post("/revoke")
def revoke(request: Request, body: dict = Body(...)):
    _admin(request)
    lic_id = str(body.get("id", "")).strip()
    if not lic_id:
        raise HTTPException(400, "id is required")
    rev = _load(REVOKED, [])
    if lic_id not in rev:
        rev.append(lic_id)
        REVOKED.write_text(json.dumps(rev, indent=1))
    return {"revoked": rev}


@app.get("/", response_class=HTMLResponse)
def index():
    """A page for the person selling licences, not for customers. The admin token
    is remembered in the browser rather than typed each time - this sits behind
    the guard anyway, and re-pasting 48 characters before every click produced
    exactly one thing: a 401 and a puzzled admin."""
    return """<!doctype html><meta charset=utf-8>
<title>Licencje — valheim-proxmox</title>
<style>
 body{background:#14110d;color:#e8e2d4;font:15px/1.6 system-ui,sans-serif;margin:0;padding:32px}
 .wrap{max-width:760px;margin:0 auto}
 h1{color:#e3c05a;font-size:22px} h2{color:#e3c05a;font-size:16px;margin-top:28px}
 input,textarea,button{font:inherit;background:#1b1712;color:#e8e2d4;border:1px solid #2f2820;
  border-radius:4px;padding:8px 10px}
 button{border-color:#c9a227;color:#e3c05a;cursor:pointer}
 .row{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}
 pre{background:#0f0d0a;border:1px solid #2f2820;border-radius:4px;padding:12px;
  white-space:pre-wrap;word-break:break-all;font-size:12px}
 .muted{color:#b9ae95;font-size:13px}
</style>
<div class=wrap>
<h1>Licencje komercyjne</h1>
<p class=muted>Klucz podpisany tutaj potwierdza, że ktoś kupił licencję. Panel go
sprawdza sam, bez łączenia się z tym serwerem — ta strona służy tylko do wydawania.</p>

<h2>Token administratora</h2>
<div class=row><input id=tok type=password placeholder="ADMIN_TOKEN" style=flex:1></div>

<h2>Wydaj licencję</h2>
<div class=row>
 <input id=holder placeholder="Dla kogo (firma, osoba)" style=flex:1>
 <input id=months type=number value=12 title="miesiące, 0 = bezterminowa" style=width:110px>
</div>
<div class=row><input id=note placeholder="Notatka (opcjonalnie)" style=flex:1></div>
<div class=row><button onclick=issue()>Wydaj i pokaż klucz</button></div>
<pre id=out>—</pre>

<h2>Unieważnij</h2>
<div class=row><input id=rid placeholder="lic_xxxxxxxxxxxx" style=flex:1>
 <button onclick=revoke()>Unieważnij</button></div>

<h2>Wydane</h2>
<div class=row><button onclick=list()>Odśwież listę</button></div>
<pre id=lst>—</pre>
</div>
<script>
// Token pamietany w przegladarce - ta strona i tak jest za guardem, a wklejanie
// 48 znakow przed kazdym klknieciem konczylo sie bledem 401 i zdziwieniem.
tok.value = localStorage.getItem('lic_admin_token') || '';
tok.oninput = () => localStorage.setItem('lic_admin_token', tok.value.trim());
function haveToken(where){
  if(tok.value.trim()) return true;
  where.textContent = 'Najpierw wklej token administratora w polu wyzej '
    + '(jest w /opt/licence/admin.env i w kopii na dysku).';
  return false;
}
const h=()=>({'Content-Type':'application/json','Authorization':'Bearer '+tok.value.trim()});
async function issue(){
 if(!haveToken(out)) return;
 const r=await fetch('/issue',{method:'POST',headers:h(),body:JSON.stringify(
  {holder:holder.value,months:+months.value,note:note.value})});
 const d=await r.json();
 out.textContent = r.ok
  ? 'Klucz do wysłania klientowi (wkleja go w panelu, zakładka Ustawienia):\\n\\n'+d.token
    +'\\n\\nid: '+d.id+'   dla: '+d.holder
  : 'Błąd: '+(d.detail||r.status);
}
async function revoke(){
 if(!haveToken(out)) return;
 const r=await fetch('/revoke',{method:'POST',headers:h(),body:JSON.stringify({id:rid.value})});
 out.textContent = r.ok ? 'Unieważnione: '+rid.value : 'Błąd: '+r.status;
}
async function list(){
 if(!haveToken(lst)) return;
 const r=await fetch('/issued',{headers:h()});
 const d=await r.json();
 lst.textContent = r.ok ? (d.issued||[]).map(x=>x.id+'  '+x.holder+'  do '+
  (x.expires?new Date(x.expires*1000).toISOString().slice(0,10):'bezterminowo')).join('\\n')||'(brak)'
  : (r.status===401 ? 'Token odrzucony — sprawdz, czy wklejony w calosci.' : 'Błąd: '+r.status);
}
// Gdy token juz jest zapamietany, lista laduje sie sama.
if(tok.value.trim()) list();
</script>
"""


@app.get("/health")
def health():
    return JSONResponse({"ok": True, "issued": len(_load(ISSUED, [])),
                         "revoked": len(_load(REVOKED, []))})
