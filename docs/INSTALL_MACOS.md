# Installing and running OpenTune AI on macOS

## Requirements

| Item | Requirement |
|---|---|
| macOS | **14 Sonoma or newer** for the latest Qt (PySide6 6.12). macOS 12–13 also work: pip automatically picks an older PySide6 (6.8.x) that supports them. macOS 11 can use PySide6 6.6–6.7. |
| Mac | Apple Silicon or Intel (PySide6 ships universal2 wheels) |
| Python | **3.10 or newer** (3.12 recommended) |
| Disk | ~400 MB for the virtual environment |
| Car (optional) | ELM327-compatible USB or Bluetooth Classic adapter for read-only diagnostics |

The macOS minimum versions above come from the platform tags of the published
PySide6-Essentials wheels (`macosx_14_0` for 6.12.0, `macosx_12_0` for 6.8.3,
`macosx_11_0` for 6.6.3).

## 1. Install Python

Check what you have:

```bash
python3 --version
```

If it's older than 3.10, install a current Python. The `python3` that comes with
Apple's Command Line Tools is usually too old. Pick one:

- **python.org installer:** download the macOS installer from
  <https://www.python.org/downloads/macos/> and run it.
- **Homebrew:** `brew install python@3.12`

## 2. Get the code

```bash
git clone https://github.com/pbelfand-dot/OpenTune-AI..git OpenTune-AI
cd OpenTune-AI
```

## 3. Create the environment

Run the setup script:

```bash
./scripts/setup_macos.sh
```

Or do it by hand:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"     # app + serial support + test tools
```

For the app only, without test tools, use `pip install -e ".[gui,serial]"`.

## 4. Launch

```bash
source .venv/bin/activate
opentune-gui
```

Other ways to start it:

- `python -m opentune gui`
- `opentune-gui --scenario wot_pull`: start the simulator doing full-throttle pulls
- `opentune-gui --page diagnostics`: open a specific page

The app starts connected to the **B58 simulator** in city driving.

- Use **Simulator controls** on the right of the Dashboard to change scenario, drive
  manually with the pedal slider, or inject faults.
- Switch to the **B48** on the **Connection** page.

### Where your data lives

`~/Library/Application Support/OpenTune AI/` holds:

- `logs/`: CSV datalogs
- `calibrations/`: calibration library and its index
- `reports/`: saved analysis reports
- `settings.json`

Set `OPENTUNE_DATA_DIR=/some/folder` to use a different location.

## 5. Verify the install

```bash
opentune selftest      # end-to-end check, ~2 s
pytest -q              # full test suite
```

## Connecting to a real car (read-only)

1. Plug the adapter into the OBD-II port, then turn the ignition on (engine may run).
2. Find the adapter's port:
   - `opentune scan --list-ports`
   - USB adapters usually appear as `/dev/cu.usbserial-*`.
   - Bluetooth Classic (SPP) adapters must be paired in System Settings first. They
     appear as `/dev/cu.<adapter-name>`.
   - Bluetooth LE-only adapters are not supported in v0.1.
3. In the app, open **Connection**:
   - Choose *ELM327 / OBDLink adapter*.
   - Pick the port.
   - Keep 38400 baud unless your adapter's manual says otherwise. ELM327 chips
     default to 38400 or 9600 depending on how the adapter is wired.
   - Click **Connect**.
4. You get live data from generic OBD-II PIDs, DTCs and VIN.
   - Generic OBD-II exposes fewer channels than BMW-specific logging: there is no
     boost target, knock data or gear.
   - The manifold-pressure PID saturates at 255 kPa absolute (≈22 psi boost at sea
     level).

From the terminal:

```bash
opentune scan --port /dev/cu.usbserial-XXXX
opentune dtc --port /dev/cu.usbserial-XXXX
```

The second command reads DTCs. Add `--clear` to clear them; it asks for confirmation.

> BMW F/G-series flashing tools use an ENET (Ethernet) cable rather than ELM327
> adapters. OpenTune v0.1 does not use ENET and never writes to the car.

## Optional: local AI narratives

The analysis page works fully offline without any model. For plain-language
explanations, run a free local model server:

```bash
brew install ollama        # or download from https://ollama.com
ollama serve &
ollama pull llama3.2
```

Then, on **AI Analysis**:

1. Tick *Generate a plain-language narrative*.
2. Click **Test connection**.
3. Run an analysis.

You can also use the CLI: `opentune analyze <log> --local-model ollama:llama3.2`.

Any OpenAI-compatible local server also works, for example LM Studio or llama.cpp's
`llama-server`. OpenTune only allows `localhost` URLs, so your logs never leave the Mac.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `zsh: command not found: opentune-gui` | Activate the venv: `source .venv/bin/activate` |
| `GUI unavailable (No module named 'PySide6')` | `pip install -e ".[gui]"` |
| pip can't find a PySide6 version | Check your Python is ≥ 3.10 and macOS ≥ 11 |
| `Cannot open /dev/cu…` | Close other apps using the adapter; re-pair Bluetooth; replug USB |
| `UNABLE TO CONNECT` | Ignition must be on; check the adapter LED; try 9600 baud |
| Local model "Not available" | Start `ollama serve`, check the URL/port, `ollama list` to confirm the model name |
