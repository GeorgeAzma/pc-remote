"""Builds the Windows app and its installer.

    python packaging/build.py            -> dist/PC-Remote-Setup-<version>.exe

1. version from git (the tag, e.g. v1.2.0, else the commit)
2. ffmpeg: an LGPL build (BtbN, with NVENC / QSV / AMF / desktop capture),
   downloaded once into packaging/cache
3. PyInstaller: dist/PC Remote/ (PC Remote.exe, Python, packages, web/)
4. signing, if a certificate is configured (see sign())
5. Inno Setup: the installer (installer.iss)

Needs: the packages in requirements.txt, pyinstaller, Inno Setup 6."""
import glob
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CACHE = os.path.join(HERE, "cache")
BUILD = os.path.join(ROOT, "build")
DIST = os.path.join(ROOT, "dist")
APP = os.path.join(DIST, "PC Remote")
FFMPEG_ZIP = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-win64-lgpl-shared-8.1.zip"


def version() -> str:
    try:
        v = subprocess.run(["git", "describe", "--tags", "--always", "--dirty"], cwd=ROOT,
                           capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        v = "0.0.0"
    return v[1:] if v.startswith("v") else v


def numeric(v: str) -> tuple:
    """'1.2.0-3-gabc' -> (1, 2, 0, 3) for the .exe's file version."""
    nums = [int(n) for n in re.findall(r"\d+", v.split("-g")[0])][:4]
    return tuple(nums + [0] * (4 - len(nums))) if re.match(r"\d", v) else (0, 0, 0, 0)


def ffmpeg() -> str:
    """Folder with ffmpeg.exe and its DLLs (not ffplay / ffprobe) plus the licence."""
    out = os.path.join(CACHE, "ffmpeg")
    if os.path.isfile(os.path.join(out, "ffmpeg.exe")):
        return out
    os.makedirs(CACHE, exist_ok=True)
    z = os.path.join(CACHE, "ffmpeg.zip")
    if not os.path.isfile(z):
        print("downloading ffmpeg …")
        urllib.request.urlretrieve(FFMPEG_ZIP, z)
    os.makedirs(out, exist_ok=True)
    with zipfile.ZipFile(z) as zf:
        for n in zf.namelist():
            base = n.split("/")[-1]
            if ("/bin/" in n and base and base not in ("ffplay.exe", "ffprobe.exe")) or base.upper() == "LICENSE.TXT":
                with open(os.path.join(out, base), "wb") as f:
                    f.write(zf.read(n))
    return out


def version_resource(v: str) -> str:
    """The .exe's Properties → Details (publisher, product, version)."""
    n = numeric(v)
    path = os.path.join(BUILD, "version_info.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={n}, prodvers={n}),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'GeorgeAzma'),
    StringStruct('FileDescription', 'PC Remote'),
    StringStruct('FileVersion', '{v}'),
    StringStruct('ProductName', 'PC Remote'),
    StringStruct('ProductVersion', '{v}'),
    StringStruct('OriginalFilename', 'PC Remote.exe'),
    StringStruct('LegalCopyright', 'GeorgeAzma')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])])
""")
    return path


def pyinstaller(v: str):
    os.makedirs(BUILD, exist_ok=True)
    with open(os.path.join(BUILD, "version.txt"), "w", encoding="utf-8") as f:
        f.write(v)
    sep = os.pathsep
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
                    "--name", "PC Remote", "--icon", os.path.join(HERE, "icon.ico"),
                    "--version-file", version_resource(v),
                    "--distpath", DIST, "--workpath", os.path.join(BUILD, "pyinstaller"), "--specpath", BUILD,
                    "--add-data", f"{os.path.join(ROOT, 'web')}{sep}web",
                    "--add-data", f"{os.path.join(BUILD, 'version.txt')}{sep}.",
                    "--collect-all", "winrt", "--exclude-module", "tkinter",
                    os.path.join(ROOT, "main.py")], check=True)
    dst = os.path.join(APP, "ffmpeg")
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(ffmpeg(), dst)


def signtool() -> str | None:
    found = sorted(glob.glob(r"C:\Program Files (x86)\Windows Kits\10\bin\*\x64\signtool.exe"))
    return found[-1] if found else shutil.which("signtool")


def sign_args() -> list | None:
    """A certificate to sign with, if configured:
    PC_SIGN_PFX (+ PC_SIGN_PASSWORD) = a .pfx file, or
    PC_SIGN_THUMBPRINT = a certificate in your certificate store.
    (Release builds on GitHub are signed by SignPath instead: see the workflow.)"""
    ts = ["/fd", "sha256", "/tr", "http://timestamp.digicert.com", "/td", "sha256"]
    if os.environ.get("PC_SIGN_PFX"):
        return ["sign", *ts, "/f", os.environ["PC_SIGN_PFX"], "/p", os.environ.get("PC_SIGN_PASSWORD", "")]
    if os.environ.get("PC_SIGN_THUMBPRINT"):
        return ["sign", *ts, "/sha1", os.environ["PC_SIGN_THUMBPRINT"]]
    return None


def sign(*files):
    args = sign_args()
    if args and files:
        subprocess.run([signtool(), *args, *files], check=True)


def inno(v: str) -> str:
    iscc = next((p for p in (os.path.expandvars(r"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"),
                             r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe", r"C:\Program Files\Inno Setup 6\ISCC.exe")
                 if os.path.isfile(p)), None) or shutil.which("iscc")
    if not iscc:
        sys.exit("Inno Setup 6 not found (winget install JRSoftware.InnoSetup)")
    cmd = [iscc, f"/DAppVersion={v}", f"/DSourceDir={APP}", f"/DOutputDir={DIST}"]
    if sign_args():  # the uninstaller inside the installer gets signed too
        cmd += ["/DSign=1", "/Ssigntool=" + subprocess.list2cmdline([signtool(), *sign_args()]) + " $f"]
    subprocess.run(cmd + [os.path.join(HERE, "installer.iss")], check=True)
    return os.path.join(DIST, f"PC-Remote-Setup-{v}.exe")


def main():
    """--app-only: just dist/PC Remote/; --installer-only: the installer around an
    existing dist/PC Remote/ (the release workflow signs the app in between)."""
    v = version()
    print("version", v)
    if "--installer-only" not in sys.argv:
        pyinstaller(v)
        sign(os.path.join(APP, "PC Remote.exe"))
    if "--app-only" in sys.argv:
        return
    setup = inno(v)
    sign(setup)
    print("built", setup, f"({os.path.getsize(setup) / 2**20:.0f} MB)")


if __name__ == "__main__":
    main()
