# Licensing

*This page describes the licences of the components OpenTune uses or interoperates with.
It is not legal advice.*

## OpenTune itself

The repository owner has not chosen an open-source licence yet ("private for now"). Until a
`LICENSE` file is added, the default applies: the code is visible but **all rights are
reserved** by the copyright holder. Choosing a licence is recommended before accepting
outside contributions. Things to weigh:

* MIT / Apache-2.0 keep reuse simple and are compatible with every dependency below.
* The lab only *talks to* the rusEFI simulator over a socket and builds engine-sim as a
  separate program, so neither forces a particular licence on OpenTune (see below).
* PySide6 is available under LGPL-3.0: fine for an application that uses it as an unmodified
  library.

## Runtime dependencies

| Component | Licence | How it is used |
|---|---|---|
| NumPy, SciPy, pandas | BSD-3-Clause (NumPy bundles some MIT/0BSD/Zlib parts) | numerics, optimisation, `.mat` export |
| FastAPI, Pydantic | MIT | local web API |
| Starlette, Uvicorn, httpx | BSD-3-Clause | web framework core, server, test client |
| Plotly (Python) / plotly.js | MIT | charts; plotly.js is served from the installed Python package |
| React, React DOM | MIT | browser UI (bundled into `src/opentune/lab/web/dist`) |
| Vite, @vitejs/plugin-react, TypeScript | MIT / Apache-2.0 | build tools only (not shipped) |
| PySide6 (Qt for Python) | LGPL-3.0 (or GPL-2.0/GPL-3.0) | v0.1 desktop GUI (optional extra) |
| pyserial | BSD-3-Clause | v0.1 ELM327 adapter support (optional extra; not used by the lab) |
| pint | BSD-3-Clause | unit checks in tests |
| Playwright | Apache-2.0 | optional browser test |

## Optional external tools (not bundled)

| Tool | Licence | Relationship |
|---|---|---|
| **engine-sim** by AngeTheGreat (Ange Yaghi), <https://github.com/ange-yaghi/engine-sim> | MIT | Built from its official repository by `scripts/build_enginesim_headless.sh` and run as a **separate process**. OpenTune contains only its own headless driver, two empty stub headers and a small portability patch (with engine-sim code excerpts, MIT). |
| piranha, csv-io, simple-2d-constraint-solver, delta-studio (engine-sim dependencies) | MIT | piranha is pinned at `004fa153`, the commit that added its MIT licence (code identical to the commit engine-sim pins). delta-studio is replaced by stubs. |
| googletest (piranha build dependency) | BSD-3-Clause | fetched at build time |
| **rusEFI** <https://github.com/rusefi/rusefi> | GPL-3.0 with additional terms in its repository | **Not bundled, linked or copied.** OpenTune implements its own client for the TunerStudio framing that the rusEFI simulator speaks and exchanges data with it over a loopback socket. Output-channel offsets are facts read from rusEFI's generated header and are cited in `integrations/rusefi.py`. If you distribute rusEFI binaries yourself, its licence obligations apply to them. |
| MATLAB / MATLAB Engine API for Python | Proprietary (MathWorks) | Optional detection only; nothing from MathWorks is copied or required. `.mat` files are written with SciPy and `.m` scripts use documented public MATLAB functions. |
| Ollama / llama.cpp / LM Studio and models | Various (model licences vary) | Optional local language model reached over HTTP on localhost. The user installs and is responsible for model licences. |

## Data and standards

* Engine specifications come from public press releases, encyclopaedic sources and
  manufacturer pressrooms; each is cited next to the value. Facts are not copyrightable, but
  wording is not copied.
* DTC descriptions are the generic SAE J2012 code texts used across the industry.
* SAE J1349, DIN 70020 and ISO 1585 are copyrighted standards. OpenTune does not reproduce
  them; it implements commonly published correction formulas, labelled as coming from
  secondary references.
* Trademarks (BMW, B58, B48, DME, MATLAB, Simulink, TunerStudio, …) belong to their owners
  and are used only to describe compatibility or inspiration. OpenTune is not affiliated with
  or endorsed by any of them.
