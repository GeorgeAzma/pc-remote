"""Renders web/icon.svg into packaging/icon.ico (every size Windows asks for).
Needs Google Chrome; the .ico is committed, so builds don't."""
import os
import struct
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SVG = os.path.join(HERE, "..", "web", "icon.svg")
SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def render(size, out, tmp):
    page = os.path.join(tmp, "icon.html")
    with open(page, "w", encoding="utf-8") as f:
        f.write(f'<body style="margin:0;background:transparent"><img src="file:///{os.path.abspath(SVG)}" '
                f'width="{size}" height="{size}" style="display:block"></body>')
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--default-background-color=00000000",
                    f"--user-data-dir={tmp}\\chrome", f"--window-size={size},{size}", f"--screenshot={out}",
                    f"file:///{page}"], capture_output=True, check=True)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        pngs = []
        for s in SIZES:
            out = os.path.join(tmp, f"{s}.png")
            render(s, out, tmp)
            with open(out, "rb") as f:
                pngs.append((s, f.read()))
    # ICO: header, one directory entry per image, then the PNGs themselves
    head = struct.pack("<HHH", 0, 1, len(pngs))
    offset, entries, blobs = 6 + 16 * len(pngs), b"", b""
    for s, data in pngs:
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    with open(os.path.join(HERE, "icon.ico"), "wb") as f:
        f.write(head + entries + blobs)
    print("icon.ico:", ", ".join(str(s) for s, _ in pngs))


if __name__ == "__main__":
    main()
