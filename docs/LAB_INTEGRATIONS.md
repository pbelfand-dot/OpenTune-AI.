# Optional integrations

None of these is needed. The lab reports each one's status at `GET /api/integrations` and
in `/api/system`. If a tool is not available, the lab says so; it never substitutes its own
model while claiming an external tool produced a result.

## rusEFI simulator (read-only)

[rusEFI](https://github.com/rusefi/rusefi) is open-source engine-management firmware
(GPL-3.0 with additional terms). Its *simulator* runs the firmware on a PC and serves the
TunerStudio protocol on TCP port 29001.

OpenTune includes an independent, **read-only, loopback-only** client
(`opentune.lab.integrations.rusefi`). It sends only `S` (signature), `V` (firmware version) and
`O` (output channels). Write, burn, execute, I/O-test and every other command are refused
before anything is sent, and only 127.0.0.1/localhost may be contacted.

Building the simulator (Linux x86; it is a 32-bit build). This sequence worked on Ubuntu
24.04 in October 2026 (rusEFI commit `2670d5b`):

```bash
sudo apt install gcc-multilib g++-multilib xxd zip 7zip mtools dosfstools openjdk-17-jdk-headless
git clone --recursive https://github.com/rusefi/rusefi.git && cd rusefi/firmware
make docs-enums
cd ../simulator && make -j4 -r USE_CLANG=no
./build/rusefi_simulator 600        # runs for 600 s
```

Then probe it from the lab (`POST /api/integrations/rusefi/probe`), or let the lab start it:

```bash
export OPENTUNE_RUSEFI_SIMULATOR=/path/to/rusefi/simulator/build/rusefi_simulator
curl -X POST localhost:8765/api/integrations/rusefi/probe -H 'Content-Type: application/json' \
     -d '{"start_simulator_seconds": 30}'
```

The response contains the signature, firmware version, RPM, coolant and intake temperature.
Channel offsets were taken from rusEFI's generated `output_channels_generated.h` at commit
`2670d5b`; a warning is added if the simulator's signature differs, because offsets change
between firmware versions. A native macOS rusEFI simulator was not tested (the build uses
32-bit Linux toolchains).

## engine-sim (external process)

[engine-sim](https://github.com/ange-yaghi/engine-sim) by AngeTheGreat (MIT) is a
crank-angle-resolved engine simulator with sound synthesis. OpenTune builds a headless
driver for it and runs it as a subprocess:

```bash
sudo apt install git cmake clang flex bison libboost-filesystem-dev   # macOS: brew install cmake flex bison boost
scripts/build_enginesim_headless.sh ~/.cache/opentune/engine-sim
export OPENTUNE_ENGINESIM_BIN=~/.cache/opentune/engine-sim/build/headless_driver
export OPENTUNE_ENGINESIM_ASSETS=~/.cache/opentune/engine-sim/engine-sim/assets
curl -X POST localhost:8765/api/integrations/enginesim/run -H 'Content-Type: application/json' \
     -d '{"seconds": 5, "throttle": 1.0, "dyno_rpm": 3000}'
```

Results include the exact command, the binary's SHA-256, the raw output, a parsed time
series (rpm, MAP, AFR, dyno torque/power) and the synthesised audio
(`/api/integrations/enginesim/last.wav`). engine-sim has its own example engines (its default
is a Subaru EJ25 definition), no turbocharger, no knock model and no ECU, and it uses
unseeded randomness. See `third_party/engine-sim-headless/README.md` for pinned versions and
licences.

## MATLAB / Simulink

MATLAB is not required. From *ECU Tuning → Versions & compare* export:

* `export.mat` — every table as a struct with `data`, `bp1…bp3` breakpoints, names and units,
  plus all scalars (written with SciPy);
* `export.m` — a script that loads that file and builds `griddedInterpolant` objects with the
  lab's clamping behaviour, ready for scripts or Simulink MATLAB Function blocks.

If the official *MATLAB Engine API for Python* is installed with a licensed MATLAB,
`opentune.lab.integrations.matlab.run()` can execute MATLAB code; nothing in the lab depends
on it.
