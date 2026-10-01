# Third-party software

PC Remote itself is MIT-licensed (see `LICENSE`). The Windows installer also
ships the components below, each under its own licence. Their licence texts
are in the installed app's `licenses\` folder, and FFmpeg's in `ffmpeg\LICENSE.txt`.

| Component | Licence | Source |
| --- | --- | --- |
| [FFmpeg](https://ffmpeg.org) 8.1 (`ffmpeg\`, an LGPL build by [BtbN](https://github.com/BtbN/FFmpeg-Builds)) | LGPL-2.1-or-later | [ffmpeg.org/download](https://ffmpeg.org/download.html), build scripts at [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds) |
| [Python](https://www.python.org) 3.12 | PSF License | [python.org](https://www.python.org/downloads/source/) |
| [PyInstaller](https://pyinstaller.org) bootloader | GPL-2.0-or-later with the bootloader exception (allows any licence for the bundled app) | [pyinstaller/pyinstaller](https://github.com/pyinstaller/pyinstaller) |
| [cryptography](https://cryptography.io), with OpenSSL | Apache-2.0 or BSD-3-Clause; OpenSSL: Apache-2.0 | [pyca/cryptography](https://github.com/pyca/cryptography) |
| [NumPy](https://numpy.org) | BSD-3-Clause (and bundled parts under 0BSD, MIT, Zlib, CC0-1.0) | [numpy/numpy](https://github.com/numpy/numpy) |
| [simplejpeg](https://gitlab.com/jfolz/simplejpeg), with libjpeg-turbo | MIT; libjpeg-turbo: IJG, BSD-3-Clause, Zlib | [jfolz/simplejpeg](https://gitlab.com/jfolz/simplejpeg) |
| [PyWinRT](https://github.com/pywinrt/pywinrt) (`winrt-*`) | MIT | [pywinrt/pywinrt](https://github.com/pywinrt/pywinrt) |

FFmpeg is used unmodified, as separate DLLs next to `ffmpeg.exe`, so it can be
replaced with another build of the same version.
