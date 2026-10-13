# Research notes (verified October 2026)

Reference facts used while building OpenTune AI v0.1, with sources. **How these were
checked:** most pages could not be fetched directly from the build environment, so
figures were cross-checked against search-engine extracts of the linked pages.
Treat prices as snapshots and re-verify anything before relying on it commercially.

## 1. OBD-II protocol facts used in code

| Fact | Used in | Source |
|---|---|---|
| Mode 01 PID formulas (0x04–0x5E set in `obd/pids.py`) | `obd/pids.py` | Wikipedia "OBD-II PIDs"; python-OBD command tables |
| PID 0x23 is 10·(256A+B) kPa (one site shows ×10/128, which looks like a mix-up with PID 0x22) | `obd/pids.py` | Wikipedia "OBD-II PIDs" |
| Supported-PID bitmasks read MSB-first; bit 7 of A = base+1 | `decode_supported` | Wikipedia "OBD-II PIDs" (one outlier site shows reverse order) |
| PID 0x0B saturates at 255 kPa absolute (≈22 psi boost at sea level) | analyzer note, docs | Derived from the PID's 1-byte range |
| DTC 2-byte encoding: A7–6 = P/C/B/U, A5–4 first digit, then three hex nibbles | `obd/dtc.py` | x-engineer.org Mode $03; Wikipedia |
| CAN Mode 03/07/0A replies carry a DTC-count byte; legacy protocols don't (00 00 padding) | `parse_dtc_payload` | x-engineer.org; python-OBD CAN tests; ELM327 datasheet example `43 01 33 00 00 00 00` |
| Mode 04 cannot clear permanent DTCs (Mode 0A) | simulator monitors | Wikipedia "OBD-II PIDs" |
| ELM327: ATZ/ATE0/ATL0/ATS0/ATH0/ATSP0/ATDPN/ATRV semantics, `>` prompt, error strings, 38400/9600 default baud | `obd/elm327.py`, emulator | ELM327 datasheet (elmelectronics.com / SparkFun mirror) |
| Generic DTC wording (P0101…P0601) | `DESCRIPTIONS` | Secondary sources (SAE J2012 itself is paywalled); older wording variants exist |

## 2. Engine facts

**B58 (B58B30)** — <https://en.wikipedia.org/wiki/BMW_B58>,
<https://press.bmwgroup.com/usa/article/detail/T0286917EN_US>, Toyota Pressroom
- 2,998 cc, 82.0 × 94.6 mm, single twin-scroll turbo, published redline 7,000 rpm.
- Gen1 compression 11.0:1. High-output TU 10.2:1. Some ~340 PS TU variants reportedly keep 11.0:1.
- TU (2018+): injection pressure raised 200 → 350 bar.
- Outputs:
  - F30 340i (US): 320 hp / 330 lb-ft. Wards says 332 lb-ft.
  - G20 M340i (US): 382 hp / 369 lb-ft.
  - GR Supra MY2021+ (US): 382 hp / 368 lb-ft.
  - GR Supra MY2020 (US): 335 hp / 365 lb-ft.
- Engine-code names (M0 / M1 / O1, "TU", "Gen2") are used inconsistently across sources.
- **No reputable published "stock boost" figure.** The DME targets load/torque, so the
  simulator's boost map is a model, not a claim.
- **ECU family:** vendor listings say the F-series B58 uses Bosch **MG1CS003**, not
  MEVD17 (MEVD17 is N20/N26/N55). G-series and Supra use MG1CS003 / MG1CS201 /
  MG1CS024 variants, according to vendors. Confirm by DME part number per car.
  OpenTune does not hard-code ECU families for this reason.

**B48 (B48B20)** — <https://en.wikipedia.org/wiki/BMW_B48>,
<https://www.press.bmwgroup.com/usa/article/detail/T0285572EN_US>
- 1,998 cc, 82.0 × 94.6 mm (same 500 cc/cylinder modular design), twin-scroll turbo.
- TU: injection pressure 200 → 350 bar.
- Outputs:
  - G20 330i (US): 255 hp / 295 lb-ft.
  - GR Supra 2.0 (US): 255 hp / 295 lb-ft.
- Compression ratio varies by variant and sources disagree (9.5–11.0:1).
- Rev limit is not consistently published. The simulator's 6,500 rpm is an assumption.
- Some US F30 330i cars were reportedly coded B46. Use the car's own identification.

## 3. Competitive landscape (snapshot; prices in USD unless noted)

| Product | Model | Price (approx.) | Source |
|---|---|---|---|
| MHD (B58 F/G) | Flasher license per VIN, maps sold separately | Flasher ~$319–331; map pack ~$125–130; Super License ~$570–592 | mhdtuning.com product pages |
| bootmod3 (B58) | One-time, VIN-linked, 1 OTS map, unlimited reflashes | $595 (+$50 OTS bundle) | bootmod3.com |
| MG Flasher (B58) | Per VIN | Flasher $299; maps $179; Ultimate $549 | shop.mgflasher.com |
| JB4 (piggyback) | Hardware, no flash | Gen1/Gen2 B58 $599; Gen3 (2025+) ~$699–778 | burgertuning.com |
| xHP | ZF 8HP transmission flash | ~€349.99 | xautomotive.com |
| BimmerLink / BimmerCode | One-time app purchase | $39.99 / $49.99 (third-party tracker) | chassissignal.com |
| Carly | Subscription | ~$69–110 / year (sources conflict) | mycarly.com |
| Cobb Accessport | — | **No Supra/B58 product found** | cobbtuning.com |

**Key insight:** the main B58 flash tools already sell **one-time, per-VIN
licenses**, not subscriptions. "Subscription-free" alone does not set OpenTune
apart from them; it only sets it apart from subscription apps like Carly.
Realistic differentiators to explore:
- open source
- macOS-native
- multi-car licensing
- transparent analysis and AI explanations
- logging and diagnostics without flashing

**Free / open tools:**
- EdiabasLib (open-source BMW diagnostics library, C#): <https://github.com/uholeschak/ediabaslib>
- RFTX Flasher (free, claims MG1 support): disputed and unproven
- RomRaider: older BMWs only
- TunerPro: generic map editor

**Technical barrier — ECU unlocks.**
- Many B58 DMEs need a one-time bench unlock before OBD flashing.
  - The MHD Unlocker device costs ~$299–400.
  - Unlock services cost ~$215–250.
- DMEs built from 06/2020 need a mail-in unlock costing ~$800–2,000 (MHD product
  page; Kies Motorsports).
- 2025+ "Gen3" B58: reported as not unlockable at the time of research.
- Any real flashing roadmap therefore depends on third-party unlocks or
  piggyback-style approaches.

## 4. Legal and regulatory (US focus)

- **COBB Tuning — EPA settlement, September 2024.**
  - $2.914M civil penalty, reduced for inability to pay.
  - About 90,000 defeat devices sold since 2015.
  - COBB may now sell only tunes that have a CARB Executive Order.
  - <https://www.epa.gov/enforcement/cobb-tuning-products-llc-clean-air-act-settlement>
- **Derive Systems (2018):** $300k penalty plus ~$6.25M compliance cost.
- **Punch It (2020):** $850k.
- **Sinister Diesel (2023):** $1M (criminal).
- **EPA totals:** FY2020–23, 172 cases and $55.5M in penalties.
- **January 2026:** DOJ ended *criminal* prosecution of OBD tampering. **Civil**
  enforcement by EPA and states continues.
- **CARB EOs:** JB4 cites EO D-744-4 (2016–2018 B58 only; vendor claim). No EO was
  found for MHD or bootmod3 B58 tunes.
- **EU / UK:**
  - Euro 7 (Reg. 2024/1257) requires anti-tampering protection.
  - Germany: an unapproved tune voids the operating permit (§19 StVZO).
  - UK: consulted in 2026 on a new emissions-modification offence.
- **Implication:** the highest-risk path is *selling* calibration flashes without a
  CARB EO. Diagnostics, logging, education and simulation carry far less regulatory
  risk. This is consistent with v0.1's design.

## 5. Founder cost references

| Item | Cost | Source |
|---|---|---|
| Apple Developer Program (signing/notarization) | $99/yr | developer.apple.com/programs |
| OBDLink EX (USB) / MX+ (Bluetooth) | $69.95 / $139.95 | obdlink.com |
| Generic ENET cable | ~$11–13 | retail listings |
| Used B58 DME for bench work | ~$300–475 (asking prices) | eBay listings |
| Replacing a bricked DME | ~$5,000+ (vendor estimate) | kiesmotorsports.com |
| General / product liability insurance (generic small business) | ~$538 / ~$1,192 per year; expect higher quotes for tuning | techinsurance.com, embroker.com |
| US accessories & performance aftermarket (SEMA) | $52.9B (2025) | SEMA market research via trade press |
