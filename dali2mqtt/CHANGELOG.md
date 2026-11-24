# Changelog

## 1.20.2

- Validate required lamp replies before changing state or publishing MQTT
  updates. Preserve previous state when replies are missing or invalid.
- Continue refreshing healthy lamps and preserve affected groups during
  partial discovery, without duplicating lamp associations.
- Report incomplete entity refreshes in the API and persistent UI warnings.
  Keep successful commissioning separate from subsequent refresh failures.

## 1.20.1

- Require valid status replies during control-device scans, reject framing
  errors, and report transport failures without returning partial results.
- Clear controls for devices missing from a successful scan while retaining
  saved MQTT profiles and Home Assistant discovery. Failed scans retain
  previous results and selections.

## 1.20.0

- Assign a Jung 1745 DSTE MQTT profile from the control-device page. Each
  configured knob has an Action entity, ten gesture triggers, and an optional
  numeric Rotary input sensor for raw instance 3 reports.
- Restore discovery on reconnect and keep a single DALI event listener across
  MQTT reconnects. Profile assignment preserves physical device settings.

## 1.19.1

- Stop delayed DT8 and other query notifications from triggering a continuous
  lamp-read feedback loop. Coalesce bursts of bus changes per lamp, retaining
  a follow-up read when another change arrives during a refresh.
- Cache successful capability detection until lamp reinitialization instead
  of rediscovering capabilities on every state refresh.
- Bound Tridonic USB response waits, reconnect on a stalled response, and
  clear outstanding sequences on cancellation so one silent request cannot
  indefinitely hold the bus lock.
- Include exception types in color warnings so timeouts are visible.

## 1.19.0

- Detect DT8 capabilities, including multi-type gear, and add temperature and
  RGB controls to existing MQTT light entities where supported.
- Publish actual color state and active mode on startup, commands, polling,
  and bus-triggered refresh; preserve existing discovery IDs and MQTT topics.
- Validate temperature limits and RGB payloads; scale MQTT RGB 0-255 to DALI
  0-254 so full intensity never becomes a MASK command.
- Keep physical-minimum brightness distinct from OFF and allow failed
  brightness writes to be retried without publishing false success state.
- Bound color operations so stalled queries do not indefinitely delay
  brightness initialization or polling.
- Prepare this release locally. Publication and live hardware acceptance
  remain separate actions.

## 1.18.1

- Capture Tridonic USB reports continuously from startup in a shared, bounded
  10,000-record buffer, retaining repeats, replies, adapter status, and unknown
  or malformed reports alongside separate decoded transaction summaries.
- Decode frame destinations and DALI-2 event sources with the shared instance
  mapper and observed device-type context. Record connection transitions and
  configuration-operation outcomes without inferring sender ownership.
- Add paginated capture reads with reset/gap metadata and complete JSONL/text
  snapshot downloads while preserving the legacy monitor and learning API.
- Replace the plain log with an eight-column monitor, expandable details,
  combined Hex/Addr/Cmd find controls, filters, paging, pause/follow, and local
  clear controls. Keep exports independent of display filters.
- Document capture limits and driver capabilities. Prepare this release locally;
  publication, installation, and physical acceptance remain separate actions.

## 1.17.0

- Publish DALI-2 input events to MQTT with source address, instance identity,
  and raw frame information, including unknown events.
- Add shared Tridonic instance-type decoding with addressed discovery at startup
  and refresh after device commissioning or address changes.
- Add Application Controller, instance enable/disable, event scheme, and Save
  controls to the configuration API and UI, with validation and readback checks.
- Preserve occupied control-device addresses during commissioning and reject
  address changes to occupied destinations.
- Add a Jung 1745 DSTE Home Assistant blueprint for short-press toggle,
  rotary brightness, and pressed-rotation color temperature with state recovery.
- Document Jung setup and add Python tests and offline Home Assistant blueprint
  validation. Physical hardware acceptance and power-cycle persistence remain
  pending.

## 1.16.1

- Target amd64 Home Assistant hosts with the Debian Bookworm base and Python 3.11.
- Clear inherited APT indexes and fail the build on any index-refresh error,
  retrying downloads before installing the existing system dependencies.
- Prepare installation from the aruss/dali2mqtt fork, retaining upstream credits.
- Add amd64 container-build and dependency smoke checks to CI.

## 1.15.0

- **DALI command console** (⌘ Commands): send curated commands (Off, Recall
  Max/Min, Up/Down, Step, DAPC level, Go to scene, …) to a short address, group
  or broadcast — like Cockpit's DALI Commands.
- **Colour control (DT8)**: set colour temperature (mireds) and RGB dim levels,
  read colour status.
- **Emergency (DT1)**: read status/mode/battery/rated duration; start function
  and duration tests, stop test.
- **Writable typed fields**: editable OEM/luminaire (and other RW) fields in the
  typed Device info page.

## 1.14.0

- New: typed **Device info** page (Cockpit-style) on each control gear — decodes
  the standard memory banks into labelled fields: device info (GTIN, firmware,
  hardware, Part 101/102/103 versions, identification), OEM/luminaire data,
  energy (active/apparent), diagnostics (operating time, supply voltage,
  temperature, thermal derating, light-source on-time, open/short circuit) and
  maintenance. Uses spec-accurate python-dali memory definitions.

## 1.13.1

- Switch configurator: guard against the ~20s bridge-startup window — the editor
  now shows "still starting" instead of an empty list, and saving is refused
  until the bridge is ready, so a mistimed save can never wipe the mapping.

## 1.13.0

- Safety: **Re-address all** is hidden behind an "..." advanced toggle and
  requires typing `READDRESS` (irreversible: every device loses its address and
  the bus is re-indexed).
- New: **Scan Lunatone** dumps the Lunatone config bank (bank 3) from every gear
  to locate switches and their target addresses.

## 1.12.0

- New: **Switch mapping configurator** in the DALI Config web UI (“⚙ Switches”).
  View/add/edit/delete physical-switch → HA-entity mappings, with a **Learn**
  button (press the switch, it captures the address from the bus) and HA entity
  autocomplete. Saves to the add-on `switch_map` option and applies live (no
  restart needed).

## 1.11.0

- Brightness-aware mirroring: when a mapped entity is a `light`, RecallMaxLevel
  sets it to 100%, and an arc-power level (DAPC) on a non-lamp switch address is
  mirrored as a brightness percentage (`light.turn_on brightness_pct`). Plain
  switches and DALI-lamp addresses are unchanged (DAPC on lamp addresses stays
  ignored to avoid feedback loops).

## 1.10.0

- Switch mirror now follows the switch's **explicit** intent: `RecallMaxLevel`
  turns the mapped HA entity **on**, `Off` turns it **off** (previously it only
  matched `Off` and did a *toggle*, so a switch that alternates on/off commands
  needed two presses). `DAPC` (the bridge's own control command) is not matched,
  so the mirror never reacts to bridge traffic (no feedback loop).

## 1.9.0

- Republish lamps/groups when Home Assistant restarts: subscribe to the HA
  birth topic `<discovery_prefix>/status` and re-run discovery on `online`
  (ported from upstream dgomes/dali2mqtt). Entities/states recover after an HA
  restart without waiting for the next poll.

## 1.8.1

- Config API: `POST /api/simulate_broadcast/{on|off}` triggers the broadcast
  all-off/all-on mirror without a physical switch (remote testing).

## 1.8.0

- New: a DALI **broadcast all-off / all-on** (e.g. a bedside "everything off"
  switch) now also switches the mapped Home Assistant entities in `switch_map`
  (turn_off / turn_on). Previously only DALI lamps reacted to a broadcast.

## 1.7.1

- Added a unit/module **test suite** (58 tests, pytest) covering DALI config
  operations, memory bank read/write, control devices, the HA client/mirror,
  bus-event publishing and the web API. CI runs them on every push.
- Fixed `change_address` verification (it used QueryShortAddress incorrectly and
  raised TypeError); it now verifies via QueryControlGearPresent at the new
  address and returns `verified`.

## 1.7.0

- Config API: `POST /api/simulate_button/{addr}` triggers the HA mirror for a
  mapped address (entity resolve + toggle) without a physical DALI press -
  handy for testing switch_map mappings remotely.

## 1.6.2

- HA entity mirror (physical DALI switch -> HA entity toggle) now works out of
  the box via the Supervisor proxy (no host/token needed) and runs its HTTP
  calls off the event loop. Configure via the `switch_map` add-on option.

## 1.6.0

- **Bus events to MQTT**: the bridge now publishes observed external DALI bus
  commands (physical button/switch presses, DALI-2 input events) to
  `<base_topic>/bus_event` as JSON. Home Assistant automations can trigger on
  this to control ANY entity (e.g. toggle an eWeLink lamp) from a DALI button.
- Config UI: added DALI-2 push-button instance config (Part 301) — timers
  (short/double/repeat/stuck) and emitted-event filter.

## 1.5.0

- Config UI: added a **Memory Bank Tool** (like DALI Cockpit) — read any memory
  bank of a control gear or control device as a hex/ASCII dump, and write a
  single byte to a location. Writes are targeted to one device via the addressed
  ENABLE WRITE MEMORY command and unlock/relock the bank automatically.

## 1.4.0

- Config UI: added a **Bus Monitor** — a live view of DALI bus traffic. DALI-1
  switches/sensors have no address and cannot be scanned, but pressing one
  broadcasts a command that now shows up here (scene/level/off + target group),
  so you can identify and document what each physical button does.

## 1.3.0

- Config UI: added DALI-2 **control device** (button/sensor) support — scan,
  identify, read instances, and change short address. Requires python-dali
  0.11. (Lunatone button-to-action mapping lives in proprietary device config
  and is not exposed by the generic DALI-2 protocol.)

## 1.2.1

- Config UI: pause the bridge's bus-traffic read-back queue during config
  operations (scan/commission/etc.) to avoid a DALI re-read storm that made
  scanning extremely slow.

## 1.2.0

- New: built-in **DALI Config** web UI (a small DALI-cockpit), served in the
  Home Assistant sidebar via Ingress. Scan the bus, identify/blink gear, set
  level, MIN/MAX/power-on/system-failure levels and fade time/rate, edit group
  membership and scene levels, change a gear's short address, and run DALI
  commissioning (assign new addresses or re-address all). Shares the bridge's
  DALI driver (no bus contention).

## 1.1.0

- Upgraded python-dali 0.9 -> 0.11 (adds DALI-2 control device / push-button
  support, and asyncio compatibility with Python 3.10+).
- build_driver adapted to the 0.11 driver API (tridonic/hasseb no longer take
  glob/loop kwargs).

## 1.0.0

- Repackaged as an installable Home Assistant add-on.
- Removed all hard-coded configuration (MQTT credentials, Home Assistant IP and
  long-lived token, DALI address→entity map, device paths).
- MQTT broker auto-discovered from the Home Assistant MQTT service, with manual
  override.
- Physical DALI switch → Home Assistant entity mirroring is now configuration
  driven (`switch_map`) and uses the Supervisor proxy + token.
- Bug fixes: blocking `time.sleep` in async lamp flash, missing `asyncio`
  import in groups, inverted `Config.__contains__`, broadcast handler iterating
  dict items instead of addresses.
- Dropped the `fuzzywuzzy` dependency (uses the standard library instead).
