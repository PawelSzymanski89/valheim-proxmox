"""Server badge as an icon, and the icon into a Windows exe - no dependencies.

The panel has no image library and no fonts, and installing both for one badge
would be a poor trade. A 5x7 bitmap font drawn by hand covers A-Z and 0-9, which
is all an acronym needs, and pixel-art is what the original generator produced
anyway.
"""
import struct
import zlib

# 5x7, one string per row, '#' is ink. Only what an acronym can contain.
FONT = {
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "C": ["01111", "10000", "10000", "10000", "10000", "10000", "01111"],
    "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "F": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
    "G": ["01111", "10000", "10000", "10111", "10001", "10001", "01111"],
    "H": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
    "I": ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
    "J": ["00111", "00010", "00010", "00010", "00010", "10010", "01100"],
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "Q": ["01110", "10001", "10001", "10001", "10101", "10010", "01101"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "U": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    "V": ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
    "W": ["10001", "10001", "10001", "10101", "10101", "11011", "10001"],
    "X": ["10001", "10001", "01010", "00100", "01010", "10001", "10001"],
    "Y": ["10001", "10001", "01010", "00100", "00100", "00100", "00100"],
    "Z": ["11111", "00001", "00010", "00100", "01000", "10000", "11111"],
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00110", "01000", "10000", "11111"],
    "3": ["11111", "00010", "00100", "00010", "00001", "10001", "01110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
}

BG = (26, 20, 16, 255)        # soot, same as the launcher's own panels
GOLD = (212, 160, 23, 255)


def acronym(server_name):
    """Up to three letters standing in for the name: initials of the words,
    the capitals inside one word, or simply its first letters."""
    name = (server_name or "").strip()
    if not name:
        return "VH"
    words = [w for w in name.replace("_", " ").replace("-", " ").split() if w]
    if len(words) > 1:
        out = "".join(w[0] for w in words[:3]).upper()
    else:
        caps = [c for c in words[0] if c.isupper()]
        out = "".join(caps[:3]) if len(caps) >= 2 else words[0][:2].upper()
    out = "".join(c for c in out if c in FONT)
    return out or "VH"


def badge_png(server_name, size=256):
    text = acronym(server_name)
    px = [[BG] * size for _ in range(size)]

    def fill(x0, y0, x1, y1, colour):
        for y in range(max(0, y0), min(size, y1)):
            for x in range(max(0, x0), min(size, x1)):
                px[y][x] = colour

    # A frame, thick enough to read at 16 pixels in a taskbar.
    b = max(2, size // 22)
    inset = size // 12
    fill(inset, inset, size - inset, inset + b, GOLD)
    fill(inset, size - inset - b, size - inset, size - inset, GOLD)
    fill(inset, inset, inset + b, size - inset, GOLD)
    fill(size - inset - b, inset, size - inset, size - inset, GOLD)

    # The letters, scaled so two or three of them fill the frame evenly.
    gap = 1
    cols = len(text) * 5 + (len(text) - 1) * gap
    scale = max(1, int((size - 2 * inset - 4 * b) / cols))
    scale_y = max(1, int((size - 2 * inset - 4 * b) / 7))
    scale = min(scale, scale_y)
    w, h = cols * scale, 7 * scale
    ox, oy = (size - w) // 2, (size - h) // 2
    for i, ch in enumerate(text):
        rows = FONT[ch]
        cx = ox + i * (5 + gap) * scale
        for ry, row in enumerate(rows):
            for rx, cell in enumerate(row):
                if cell == "1":
                    fill(cx + rx * scale, oy + ry * scale,
                         cx + (rx + 1) * scale, oy + (ry + 1) * scale, GOLD)

    raw = b"".join(b"\x00" + b"".join(bytes(p) for p in row) for row in px)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def patch_exe_icon(exe: bytes, png: bytes) -> bytes:
    """Replaces the icon inside a Windows executable, in place.

    Only the bytes of the existing icon are overwritten and its recorded length
    adjusted, so nothing in the file moves - which is the only way to do this
    without a full PE rewriter. Returns the input unchanged when the new icon
    would not fit or the file is not shaped as expected."""
    try:
        pe = struct.unpack("<I", exe[0x3C:0x40])[0]
        if exe[pe:pe + 4] != b"PE\0\0":
            return exe
        n_sections = struct.unpack("<H", exe[pe + 6:pe + 8])[0]
        opt_size = struct.unpack("<H", exe[pe + 20:pe + 22])[0]
        sec_off = pe + 24 + opt_size
        rsrc = None
        for i in range(n_sections):
            s = sec_off + i * 40
            if exe[s:s + 8].rstrip(b"\0") == b".rsrc":
                rsrc = (struct.unpack("<I", exe[s + 12:s + 16])[0],   # VirtualAddress
                        struct.unpack("<I", exe[s + 20:s + 24])[0])   # PointerToRawData
        if not rsrc:
            return exe
        va, raw_ptr = rsrc

        def entries(dir_off):
            named, idd = struct.unpack("<HH", exe[raw_ptr + dir_off + 12:raw_ptr + dir_off + 16])
            out = []
            for i in range(named + idd):
                e = raw_ptr + dir_off + 16 + i * 8
                name, off = struct.unpack("<II", exe[e:e + 8])
                out.append((name, off))
            return out

        out = bytearray(exe)
        for type_id, type_off in entries(0):
            if type_id not in (3, 14):          # RT_ICON, RT_GROUP_ICON
                continue
            for _, name_off in entries(type_off & 0x7FFFFFFF):
                for _, lang_off in entries(name_off & 0x7FFFFFFF):
                    d = raw_ptr + (lang_off & 0x7FFFFFFF)
                    data_rva, data_size = struct.unpack("<II", exe[d:d + 8])
                    at = raw_ptr + data_rva - va
                    if type_id == 3:
                        if len(png) > data_size:
                            return exe          # would not fit, leave it alone
                        out[at:at + len(png)] = png
                        out[at + len(png):at + data_size] = b"\0" * (data_size - len(png))
                        struct.pack_into("<I", out, d + 4, len(png))
                    else:
                        # GRPICONDIR: keep the entry, correct the recorded length
                        count = struct.unpack("<H", exe[at + 4:at + 6])[0]
                        for i in range(count):
                            e = at + 6 + i * 14
                            struct.pack_into("<I", out, e + 8, len(png))
        return bytes(out)
    except Exception:
        return exe


if __name__ == "__main__":
    import sys
    name = sys.argv[1] if len(sys.argv) > 1 else "VintageSrv"
    open("/tmp/badge.png", "wb").write(badge_png(name))
    print("acronym:", acronym(name), "-> /tmp/badge.png",
          len(badge_png(name)), "bytes")
