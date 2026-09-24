#!/usr/bin/env python3
"""Sign release files: writes FILE.sig (base64 ed25519 over the file's bytes) next to each.

The private key never goes near GitHub - it lives on the maintainer's machine
(~/.config/valheim-proxmox/release-signing.key, or $VH_SIGNING_KEY). Installs, the panel
and the launcher carry the public half and refuse a release whose files do not verify, so
a stolen GitHub account can publish a release but cannot make anyone install it.

    scripts/sign-release.py dist/valheim-proxmox-v1.23.0.tar.gz
    scripts/sign-release.py --verify FILE        # check a .sig against the public key
"""
import base64
import os
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

KEY = os.environ.get("VH_SIGNING_KEY", os.path.expanduser("~/.config/valheim-proxmox/release-signing.key"))
PUB = "WwQ2bZrUDQpTQhWzJgT4ojDUo5DXnHi8DuXvTRBZgX0="   # the same key is in panel-update.sh, app.py and the launcher


def main(args):
    if args and args[0] == "--verify":
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(PUB))
        for f in args[1:]:
            pub.verify(base64.b64decode(open(f + ".sig", "rb").read()), open(f, "rb").read())
            print("ok", f)
        return
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(open(KEY, "rb").read()))
    for f in args:
        sig = base64.b64encode(key.sign(open(f, "rb").read()))
        with open(f + ".sig", "wb") as out:
            out.write(sig + b"\n")
        print("signed", f)


if __name__ == "__main__":
    main(sys.argv[1:])
