#!/usr/bin/env python3
"""Sign release files: writes FILE.sig (base64 ed25519 over the file's bytes) next to each.

The private key never goes near GitHub - it lives on the maintainer's machine
(~/.config/valheim-proxmox/release-signing.key, or $VH_SIGNING_KEY). Installs, the panel
and the launcher carry the public half and refuse a release whose files do not verify, so
a stolen GitHub account can publish a release but cannot make anyone install it.

    scripts/sign-release.py dist/valheim-proxmox-v1.23.0.tar.gz
    scripts/sign-release.py --verify FILE        # check a .sig against the release keys
    VH_SIGNING_KEY=/path/backup.key scripts/sign-release.py FILE   # sign with the backup key
"""
import base64
import os
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

KEY = os.environ.get("VH_SIGNING_KEY", os.path.expanduser("~/.config/valheim-proxmox/release-signing.key"))
# the working key and the offline backup - the same pair as panel-update.sh, app.py and the launcher
PUBS = ("WwQ2bZrUDQpTQhWzJgT4ojDUo5DXnHi8DuXvTRBZgX0=", "649uL/TAv45znSgfclQMBTS3IhUV45Fh3ax2vsYaRDA=")


def main(args):
    if args and args[0] == "--verify":
        for f in args[1:]:
            sig, data = base64.b64decode(open(f + ".sig", "rb").read()), open(f, "rb").read()
            for i, k in enumerate(PUBS):
                try:
                    Ed25519PublicKey.from_public_bytes(base64.b64decode(k)).verify(sig, data)
                    print("ok", f, "(working key)" if i == 0 else "(backup key)")
                    break
                except Exception:
                    continue
            else:
                raise SystemExit(f"NOT signed by a release key: {f}")
        return
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(open(KEY, "rb").read()))
    for f in args:
        sig = base64.b64encode(key.sign(open(f, "rb").read()))
        with open(f + ".sig", "wb") as out:
            out.write(sig + b"\n")
        print("signed", f)


if __name__ == "__main__":
    main(sys.argv[1:])
