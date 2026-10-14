# engine-sim headless driver (optional)

A small command-line driver that runs [engine-sim](https://github.com/ange-yaghi/engine-sim)
by AngeTheGreat (Ange Yaghi) without its GUI. It lets OpenTune run engine-sim as an
**external process** through `opentune.lab.integrations.enginesim`. Nothing in OpenTune
needs it.

## What is here

| File | Origin |
|---|---|
| `driver.cpp` | Written for OpenTune. Compiles engine-sim's `.mr` engine script, runs its simulator with a dyno and throttle setting, prints a time series and writes the synthesised audio to `headless_out.wav`. Includes a minimal PCM WAV reader because engine-sim normally loads impulse responses through delta-studio's Windows-only reader. |
| `CMakeLists.txt` | Written for OpenTune. Builds engine-sim's simulation sources, its scripting compiler (piranha) and the driver. Excludes the GUI. |
| `headless.patch` | Small portability fixes to engine-sim sources (MSVC-only constructs such as `extern constexpr` and missing standard includes) so they compile with Clang/GCC on Linux and macOS. The patch contains excerpts of engine-sim code (MIT). |
| `stubs/delta-studio/...` | Two empty headers standing in for delta-studio (engine-sim's GUI/audio engine), which the simulation sources include but do not use. |

## Build

```bash
scripts/build_enginesim_headless.sh ~/.cache/opentune/engine-sim
export OPENTUNE_ENGINESIM_BIN=~/.cache/opentune/engine-sim/build/headless_driver
export OPENTUNE_ENGINESIM_ASSETS=~/.cache/opentune/engine-sim/engine-sim/assets
```

Requirements: `git cmake clang flex bison` and Boost.Filesystem
(`apt install libboost-filesystem-dev` / `brew install boost`).

The script was verified from a clean clone on Ubuntu 24.04 with Clang 18 (October 2026).
It has not been tested on macOS.

## Pinned versions and licences

| Component | Commit | Licence |
|---|---|---|
| engine-sim | `85f7c3b959a908ed5232ede4f1a4ac7eafe6b630` | MIT, © 2022 AngeTheGreat (Ange Yaghi) |
| piranha | `004fa153` | MIT, © 2022 Ange Yaghi |
| csv-io | submodule commit pinned by engine-sim | MIT |
| simple-2d-constraint-solver | submodule commit pinned by engine-sim | MIT |
| googletest (build-time dependency of piranha) | `609281088cfefc76f9d0ce82e1ff6c30cc3591e5` | BSD-3-Clause |

engine-sim pins piranha at `432f0b1`, a commit **without** a licence file. The next
commit, `004fa153` ("Added license"), adds only the MIT `LICENSE` file; the code is
identical. The build script therefore uses `004fa153`, so every piece of source is
under an explicit licence. No engine-sim, piranha or delta-studio source is committed
to this repository apart from the patch excerpts above; the build script fetches the
sources from their official repositories.

## Output format (parsed by OpenTune)

```
engine=<name> cylinders=<n> displacement_L=<l> sim_freq=<Hz>
t=<s>s steps=<n> rpm=<rpm> map_kPa=<kPa> afr=<afr> dyno_torque_Nm=<Nm> step_avg_dyno_Nm=<Nm> dyno_power_kW=<kW> latency=<s> audio_samples=<n>
```

## Caveats

* engine-sim's included engines are its own examples (the default `main.mr` is a Subaru
  EJ25 definition). There is no B58/B48 definition, no turbocharger, no knock model and
  no ECU in engine-sim.
* engine-sim uses unseeded random numbers, so repeated runs differ slightly.
* Torque sign and filtering follow engine-sim's dynamometer conventions; OpenTune
  reports them unchanged and labels them as engine-sim output.
