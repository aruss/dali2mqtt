# Jung 1745 DSTE input controls

This integration uses `Jung -> DALI -> Tridonic USB -> DALI2MQTT -> MQTT ->
Home Assistant automation -> light`. Bridge events and device configuration
controls are generic. The supplied blueprint interprets Jung-specific rotary
codes. It does not program the device automatically.

Local software checks passed; physical operation and persistent storage still
need the acceptance checks below. Installing this change, deploying the bridge,
and configuring physical devices are separate operations.

## Add the knob to Home Assistant

In the add-on, **Scan devices**, select D1 and use **Identify device** to
confirm the physical knob. Under **Home Assistant MQTT profile**, select
**Jung 1745 DSTE** and click **Save profile**. This saves the assignment in
add-on data and publishes discovery. It sends no DALI commands. This works
with all four instances disabled and preserves direct wall control.

Open **Settings → Devices & services → MQTT → Devices → Jung 1745 DSTE D1**.
The **Action** entity and ten device triggers use button and rotation reports
from instances 0 and 2. To produce gestures, use the instance settings below.
Changing the Application Controller setting is a separate physical change.
Leave it at its original setting when testing direct wall control. When using
both HA automations and the controller, take care that one gesture does not
command the same lamp twice.

For **Rotary input**, choose **Jung 1745 DSTE with rotary input**. Record the
current instance enable flags and schemes. For an absolute-only trial, disable
instances 0, 1 and 2, set instance 3 to **Device + instance** (scheme 2), then
enable it. Reread D1 and wait for the configuration operation to finish before
turning the knob. The sensor shows the raw ten-bit event information as a
number, starting at **unknown** until an instance 3 report arrives. Calibration
on D1 is pending. Actual brightness and colour temperature are on A0/A1.

To return to gestures, disable instance 3, restore the gesture settings for
instances 0 and 2 below, leave instance 1 disabled, and select the basic Jung
profile. To restore the original wall configuration after a trial, restore
the recorded settings. Selecting **None** removes the MQTT entities and
device triggers for that address. Remove the old assignment before readdressing
a knob, then identify and assign its new address.

## Configure one control at a time for the gesture blueprint

1. Connect the Tridonic interface and check bridge MQTT connectivity. Open the
   DALI Config UI and scan **control devices**. Gear (lamp) addresses and control
   device addresses are separate namespaces; record each knob's device address
   `D0`–`D63`, not a lamp address `A0`–`A63`.
2. If a knob has no address, commission **only unaddressed control devices**
   (`readdress: false`). Existing device and gear addresses must remain intact.
   Do not choose readdress-all. The allocator skips occupied control-device
   addresses. Identify each device individually and record its unique address.
3. Select the device and **Read instances**. For a Jung 1745 DSTE, expect
   instance 0 (type 1, pushbutton), instance 1 (type 0, relative rotation),
   instance 2 (type 0, relative rotation with speed), and instance 3 (type 2,
   analogue absolute rotation). Stop if the hardware reports a different layout.
4. Enable instances **0 and 2**; disable **1 and 3**. Do not operate both type-0
   rotary instances simultaneously. Set event scheme **2 — Device + instance**
   for instances 0 and 2. Each write checks readback; reread all instances and
   verify the result before continuing.
5. Open **Button timers / filters** for instance 0. Enable `button_released`,
   `button_pressed`, and `short_press`; disable other filters for this blueprint.
   Those three filter bits give mask **7**. Filter bit positions are not event
   codes: MQTT release/press/short-press information values are **0/1/2**.
   Read the button settings back and verify those filters. Timer/filter controls
   apply only to Part 301 (type 1), not rotary instances.
6. Only after instance configuration and readback succeed, disable this device's
   **Application Controller**. Reread and confirm disabled. This prevents its
   application logic from also sending direct gear-control commands. If the
   controller is absent, always active, or unreadable, investigate that device
   rather than proceeding with an assumed state.
7. Click **Save device configuration** on this device. This sends addressed
   `SavePersistentVariables`, with send-twice handled by python-dali. A successful
   HTTP result confirms the command was sent and the device still responds;
   durable persistence remains unverified until a power cycle and reread.
8. Repeat for the other knob. Never use broadcast writes to configure these
   settings. Preserve existing gear behavior and addresses throughout.

The [Jung operating manual, pp. 12–15](https://downloads.jung.de/Downloads/Produktdownloads/Bedienungsanleitung/Licht-Management/en_174xDSTE_82408403_20240923.pdf)
documents the instance layout and warns against simultaneous type-0 instances.
Scheme 2 is this integration's choice for identifying independent controls.
Pressed rotation combines button state and rotary events in HA; no dedicated
Jung color-temperature event is assumed.

## Install the Home Assistant blueprint

Requires Home Assistant 2025.3 or later and its MQTT integration. Copy
[`jung_1745_dste.yaml`](../example/blueprints/automation/jung_1745_dste.yaml) to
`<HA config>/blueprints/automation/dali2mqtt/jung_1745_dste.yaml`, then reload
automations. Once this source file is published to GitHub, HA's **Import
Blueprint** dialog can also import its GitHub file URL. Local changes are not
available through that URL until publication.

Create **one automation per knob** from the blueprint. For each automation:

- Set the bridge MQTT base topic without a trailing slash, the knob's device
  short address, and one target `light` entity.
- Create two Toggle helpers (`input_boolean`): `pressed` and
  `rotated_while_pressed`. Create one Timer helper for recovery. Use three
  distinct helpers per knob; never share any of them between automations.
- Keep the recovery timer available and enabled. The blueprint restarts it at
  30 seconds on press or rotary activity, regardless of its configured duration.
- Adjust step inputs as needed. Default brightness fast/slow/increment steps
  are 10%/5%/2%; temperature steps are 200/100/50 K. Both apply per received
  event. Rotary cadence and stop behavior must be measured on physical hardware
  before adding continuous-repeat behavior.

Example automation after copying the file:

```yaml
alias: Living room Jung
use_blueprint:
  path: dali2mqtt/jung_1745_dste.yaml
  input:
    mqtt_base_topic: dali2mqtt
    device_address: 0
    target_light: light.living_room
    pressed: input_boolean.jung_living_pressed
    rotated_while_pressed: input_boolean.jung_living_rotated
    recovery_timer: timer.jung_living_recovery
```

Short press toggles. Clockwise rotation increases brightness; counterclockwise
decreases it. While pressed, clockwise increases Kelvin (cooler white), and
counterclockwise decreases Kelvin. Temperature is clamped to the light's
`min_color_temp_kelvin` / `max_color_temp_kelvin` attributes, using the midpoint
if current temperature is absent. Lights without `color_temp` support receive
no temperature action. Pressed rotation still suppresses the following toggle.

Press starts a new gesture and clears rotation suppression. Release clears
pressed state but retains suppression until the next press or recovery reset.
The automation processes events in a queue per knob. Helpers clear after
30 seconds without press/rotation activity, at HA startup, and when the bridge
publishes `offline` on `<mqtt_base_topic>/status` (the existing retained MQTT
availability/will signal; available payload is `online`). This signal reports
bridge MQTT availability, not an independent USB health probe. A missed release
therefore recovers via the timer even when MQTT stays connected.

## MQTT event contract

Subscribe to `<mqtt_base_topic>/bus_event`. Existing gear payloads remain
compatible. An input event adds source identifiers and raw frame information:

```json
{
  "event": true,
  "type": "ButtonPressed",
  "command": "ButtonPressed(short_address=0, instance_number=0)",
  "address": null,
  "destination_kind": null,
  "destination": "",
  "response": null,
  "source_address": 0,
  "instance_number": 0,
  "instance_type": 1,
  "device_group": null,
  "instance_group": null,
  "event_info": 1,
  "raw_frame": 32769
}
```

Missing source identifiers are `null`. Destination fields never hold source
identity. `event_info` is the frame's low ten bits; `raw_frame` is the full frame
integer. Unknown generic/analogue events remain visible as `UnknownEvent`;
unmapped device/instance events remain visible as `AmbiguousInstanceType` with
null instance type. The shared Tridonic type map uses addressed queries at
startup and rebuilds after device commissioning/address changes, dropping stale
entries. It does not use state-changing broadcast autodiscovery. The bridge
skips events while configuration owns the bus; do not operate knobs during setup.

The blueprint matches `event`, `source_address`, and `instance_number`, then
uses these information values:

| Instance | Information | Meaning |
|---|---|---|
| 0 | 0 / 1 / 2 | Released / pressed / short press |
| 2 | 0 | Stop; no light action |
| 2 | 1 / 2 / 3 | Clockwise fast / slow / increment |
| 2 | 4 / 5 / 6 | Counterclockwise fast / slow / increment |

The legacy `switch_map` handles destination-addressed gear commands; it cannot
replace source/instance matching for these input events.

## Configuration API

`GET /api/device/{addr}` returns `application_controller` with `present`,
`always_active`, and `enabled`, plus each instance's `type`, `enabled`, and
`scheme`. Unavailable capability/state values are null or explicit failures.
These addressed POST routes share the existing configuration guard:

| Route under `/api/device/{addr}` | JSON body |
|---|---|
| `/application_controller` | `{"enabled": false}` |
| `/instance/{instance}/enabled` | `{"enabled": true}` |
| `/instance/{instance}/scheme` | `{"scheme": 2}` |
| `/save` | `{}` |

Addresses must be 0–63, instances must exist, enabled values must be JSON
booleans, and schemes must be integers 0–4. Invalid input yields HTTP 400
without configuration writes; missing devices/readback failures yield HTTP 502.
Save returns `persistence_verified: false` until physical acceptance is performed.

## Local validation and hardware acceptance

Run the complete Python suite from the repository root:

```sh
python -m pytest dali2mqtt/tests -q
```

Run the offline HA checks using the actual HA schema, templates, and queued
action engine (Docker; no broker or hardware access):

```sh
docker run --rm --network none --mount "type=bind,source=$(pwd),target=/repo,readonly" \
  --entrypoint python ghcr.io/home-assistant/home-assistant:2025.3.4 \
  /repo/dali2mqtt/tests/validate_jung_blueprint.py
```

On PowerShell, replace `$(pwd)` with the repository's absolute Windows path and
put the command on one line. Tests stub light/helper services; they validate
generated actions and state transitions, not real light integration behavior.

After authorized installation and physical configuration, still verify:

- Tridonic/MQTT visibility and discovery without readdressing existing devices.
- Two controls with distinct device addresses and no cross-control effects.
- Application Controllers inactive; input gestures emit input events without
  direct gear-control commands.
- Toggle, both brightness directions, pressed temperature, release ordering,
  suppressed accidental toggles, and missed-release recovery.
- Physical rotary traces/cadence and stop behavior; tune steps if needed.
- Power-cycle controls; reread mode, instance enable/filter/scheme settings,
  confirm decoder refresh and independent automations still work.

Local checks cannot complete these hardware checks.
