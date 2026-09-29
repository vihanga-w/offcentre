# Third-party software

offcentre's own code is MIT-licensed (see [LICENSE](LICENSE)). It relies on the open-source software below. **None of it is copied into this repository or into offcentre's releases:** Python packages are installed separately by pip or Homebrew, and the 3D viewer's libraries are loaded by your browser from jsDelivr. Each project's own licence applies to it, and its copyright notices travel with the copy you install.

## Python packages (installed as dependencies)

| Package | Licence | Project |
|---|---|---|
| numpy | BSD-3-Clause (with components under 0BSD, MIT, Zlib and CC0-1.0) | https://numpy.org |
| sounddevice | MIT | https://github.com/spatialaudio/python-sounddevice |
| cffi (used by sounddevice) | MIT-0 | https://cffi.readthedocs.io |
| pycparser (used by cffi) | BSD-3-Clause | https://github.com/eliben/pycparser |

## PortAudio

sounddevice uses [PortAudio](https://www.portaudio.com), under the PortAudio licence (MIT-style), Copyright (c) 1999-2011 Ross Bencina and Phil Burk. The macOS wheels of sounddevice bundle it; the Homebrew install uses Homebrew's `portaudio` package.

## JavaScript, loaded at runtime (3D scan view only)

| Library | Version | Licence | Project |
|---|---|---|---|
| three.js | 0.180.0 | MIT | https://threejs.org |
| Spark (`@sparkjsdev/spark`) | 2.2.0 | MIT | https://sparkjs.dev |

Both are fetched from `cdn.jsdelivr.net` when the 3D scan view is first opened, and are not modified.

## Optional, not bundled

[BlackHole](https://github.com/ExistentialAudio/BlackHole) (GPL-3.0) is mentioned as an optional alternative input for older macOS versions (`--source blackhole`). offcentre doesn't include or link to it; it's a separate audio driver you'd install yourself.

## Platform frameworks

The audio tap helper uses Apple's Core Audio framework, part of macOS.

## Brand assets

The offcentre logotype is drawn from geometric primitives in code (`assets/brand/logotype.py`), not derived from a typeface. Banner text is set in whatever system font the viewer has; no fonts are distributed.
