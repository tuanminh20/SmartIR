<p align="center">
  <a href="#"><img src="assets/smartir_fan.png" width="350" alt="SmartIR Media Player"></a>
</p>

For this platform to work, we need a .json file containing all the necessary IR or RF commands.
Find your device's brand code [here](FAN.md#available-codes-for-fan-devices) and add the number in the `device_code` field. The compoenent will download it to the correct folder. If your device is not working, you will need to learn your own codes and place the .json file in `smartir/codes/fan/` subfolders. Please note that the `device_code` field only accepts positive numbers. The .json extension is not required.

## Configuration variables

**name** (Optional): The name of the device<br />
**unique_id** (Optional): An ID that uniquely identifies this device. If two devices have the same unique ID, Home Assistant will raise an exception.<br />
**device_code** (Required): ...... (Accepts only positive numbers)<br />
**controller_data** (Required): The data required for the controller to function. Enter the entity_id of the Broadlink remote (must be an already configured device), or the entity id of the Xiaomi IR controller, or the MQTT topic on which to send commands.<br />
**delay** (Optional): Adjusts the delay in seconds between multiple commands. The default is 0.5 <br />
**power_sensor** (Optional): *entity_id* for a sensor that monitors whether your device is actually On or Off. This may be a power monitor sensor. (Accepts only on/off states)<br />

## Sync state from HAIR or a physical remote

After HAIR recognizes an **absolute** speed or off command, call
`smartir.sync_fan_state` targeting the existing SmartIR fan. This updates Home
Assistant state immediately without sending IR/RF or creating another entity.
Use this action in the automation triggered by your learned HAIR command:

```yaml
action: smartir.sync_fan_state
target:
  entity_id: fan.panasonic_ceiling_fan
data:
  speed: "3"
```

Use `speed: "off"` for off. `speed` is required and must be a string: either
`"off"` or an exact speed from the target device JSON (`"1"` through `"9"` for
Panasonic 1221). Numeric YAML values, percentages, `"on"`, and unsupported speed
names are rejected. Other models can use their own speed strings, such as
`"low"`. Validation is per entity; when targeting several fans, valid targets
can update even if another target rejects that speed. Prefer one fan per HAIR
automation. Toggle or speed-cycle remote commands require separate logic to
determine the actual resulting speed.

The action updates on/off state, percentage, and `last_on_speed`. Off preserves
the last running speed so a later normal `fan.turn_on` resumes it. Direction and
oscillation are preserved. Only SmartIR fan entities receive the action; fans
from other integrations are ignored by Home Assistant's entity service dispatch.
The action is registered when a SmartIR fan platform is successfully set up.

An observation cancels pending power-speed settling to prevent old feedback
from overwriting it. It does not change the sensors or configuration. The
observation is applied immediately, including when a sensor still reports an
older value; subsequent `power_sensor` and `power_speed_sensor` events retain
their existing behavior and can update the state again. Ordinary fan control
actions continue to transmit commands. No HAIR dependency is required in SmartIR.

## Optional speed feedback from measured power

**power_speed_sensor** (Optional): Entity ID of a numeric sensor reporting watts (W).
Requires `power_sensor` and `power_speed_map`.<br />
**power_speed_map** (Optional): A map from each speed string in the device JSON to
`[minimum_watts, maximum_watts]`. Quote speed keys such as `"1"`. All device
speeds must be included. Bounds must be finite, nonnegative, and minimum must be
less than maximum. Ranges must not overlap; gaps are allowed. The lower bound
is included and the upper bound is excluded.<br />
**power_speed_delay** (Optional): Seconds a mapped speed must remain stable before
it is applied. Default: `10`. Must be finite and nonnegative. `0` disables the
settling period. This is separate from the controller's `delay`.

Speed feedback only updates the fan entity's speed, percentage, and
`last_on_speed`. It never sends an IR/RF command. Normal fan control services
still send commands as before.

- The on/off `power_sensor` is authoritative for feedback: `off` immediately
  sets percentage to `0` and cancels pending speed updates. Watts cannot turn the
  fan on. `on` permits speed inference; a newly observed on state has an unknown
  percentage (`null`) until a speed is confirmed.
- Watt readings may fluctuate within one range without restarting the timer.
  Moving into another range restarts the settling period. Returning to the
  confirmed speed cancels a pending change.
- Missing, `unknown`, `unavailable`, nonnumeric, nonfinite, and unmapped watt
  readings cancel pending updates and preserve the last confirmed speed. An
  unknown/missing on/off sensor also blocks inference and preserves the last
  fan state. Recovery starts a new settling period.
- On startup, existing sensor states are read after restoring the fan state;
  no subsequent sensor event is required. A currently off sensor overrides a
  restored on state immediately. A currently on sensor applies measured speed
  after the settling period. Listeners and timers are removed with the entity.
- An explicit speed/turn-on/turn-off command cancels a pending speed update.
  Subsequent sensor changes can start a new settling period.

### Panasonic 1221 calibration template

The [Panasonic 1221 device file](../codes/fan/1221.json) supports F-60GDN and
F-60GDS with Broadlink and speed strings `"1"` through `"9"`. Its IR codes were
supplied by the user and have not been tested against hardware. If this file is
already installed in `custom_components/smartir/codes/fan/1221.json`, keep it;
speed feedback does not modify IR commands. For a new installation, copy
`codes/fan/1221.json` from this fork to
`custom_components/smartir/codes/fan/1221.json` manually. The automatic device
downloader still uses upstream SmartIR, where this file may not be available.

**The following is a template, not a runnable configuration.** Replace every
`MIN_*_W` and `MAX_*_W` with measured numeric watt limits before enabling speed
feedback. No Panasonic watt thresholds are assumed or provided. Choose ranges
from settled readings at each of the nine speeds, include normal variation,
and avoid overlaps. Gaps can be used to reject uncertain readings. If two
speeds have indistinguishable power consumption, watts alone cannot identify
them reliably.

```yaml
fan:
  - platform: smartir
    name: Panasonic Ceiling Fan
    unique_id: panasonic_ceiling_fan
    device_code: 1221
    controller_data: remote.living_room_broadlink_remote
    power_sensor: binary_sensor.living_room_quat_tran_s60_plug_quat_tran_running
    power_speed_sensor: sensor.sonoff_1002a3d31c_power
    power_speed_delay: 10
    power_speed_map:
      "1": [MIN_1_W, MAX_1_W]
      "2": [MIN_2_W, MAX_2_W]
      "3": [MIN_3_W, MAX_3_W]
      "4": [MIN_4_W, MAX_4_W]
      "5": [MIN_5_W, MAX_5_W]
      "6": [MIN_6_W, MAX_6_W]
      "7": [MIN_7_W, MAX_7_W]
      "8": [MIN_8_W, MAX_8_W]
      "9": [MIN_9_W, MAX_9_W]
```

Home Assistant's ordered speed conversion gives percentages `11, 22, 33, 44,
55, 66, 77, 88, 100` for these nine speeds. The mapping works with other fan
code files too, using their own exact speed strings.

When installing this fork, keep a backup of the existing device file and fan
configuration. The built-in `smartir.update_component` service downloads from
upstream SmartIR and can overwrite this fork's changes. Avoid that service
while testing this branch; `smartir: {check_updates: false}` disables automatic
update checks. Removing the three `power_speed_*` options returns to on/off-only
feedback, and the original `power_sensor` option remains supported.

## Example (using broadlink controller)

Add a Broadlink RM device named "Bedroom" via config flow (read the [docs](https://www.home-assistant.io/integrations/broadlink/)).

```yaml
smartir:

fan:
  - platform: smartir
    name: Bedroom fan
    unique_id: bedroom_fan
    device_code: 1000
    controller_data: remote.bedroom_remote
    power_sensor: binary_sensor.fan_power
```

## Example (using xiaomi controller)

```yaml
smartir:

remote:
  - platform: xiaomi_miio
    host: 192.168.10.10
    token: YOUR_TOKEN

fan:
  - platform: smartir
    name: Bedroom fan
    unique_id: bedroom_fan
    device_code: 2000
    controller_data: remote.xiaomi_miio_192_168_10_10
    power_sensor: binary_sensor.fan_power
```

## Example (using mqtt controller)

```yaml
smartir:

fan:
  - platform: smartir
    name: Bedroom fan
    unique_id: bedroom_fan
    device_code: 3000
    controller_data: home-assistant/bedroom-fan/command
    power_sensor: binary_sensor.fan_power
```

## Example (using LOOKin controller)

```yaml
smartir:

fan:
  - platform: smartir
    name: Bedroom fan
    unique_id: bedroom_fan
    device_code: 4000
    controller_data: 192.168.10.10
    power_sensor: binary_sensor.fan_power
```

## Example (using ESPHome)

ESPHome configuration example:

```yaml
esphome:
  name: my_espir
  platform: ESP8266
  board: esp01_1m

api:
  services:
    - service: send_raw_command
      variables:
        command: int[]
      then:
        - remote_transmitter.transmit_raw:
            code: !lambda 'return command;'

remote_transmitter:
  pin: GPIO14
  carrier_duty_percent: 50%
```

HA configuration.yaml:

```yaml
smartir:

fan:
  - platform: smartir
    name: Bedroom fan
    unique_id: bedroom_fan
    device_code: 4000
    controller_data: my_espir_send_raw_command
    power_sensor: binary_sensor.fan_power
```

## Available codes for Fan devices

The following are the code files created by the amazing people in the community. Before you start creating your own code file, try if one of them works for your device. **Please open an issue if your device is working and not included in the supported models.**
Contributing to your own code files is welcome. However, we do not accept incomplete files as well as files related to MQTT controllers.

#### Kaze

| Code | Supported Models | Controller |
| ------------- | -------------------------- | ------------- |
[1000](../codes/fan/1000.json)|Unknown|Broadlink

#### Acorn

| Code | Supported Models | Controller |
| ------------- | -------------------------- | ------------- |
[1020](../codes/fan/1020.json)|Unknown|Broadlink

#### Atomberg

| Code | Supported Models | Notes |Controller |
| ------------- | ----- | ----- | ------------- |
[1160](../codes/fan/1160.json)|Efficio||Broadlink
[1170](../codes/fan/1170.json)|Renesa|Speeds `1,2,3,4,5` is mapped to `2,3,4,5,Boost` on the remote|Broadlink

#### Lucci Air

| Code | Supported Models | Controller |
| ------------- | -------------------------- | ------------- |
[1040](../codes/fan/1040.json)|Aria|Broadlink
[1041](../codes/fan/1041.json)|Whitehaven DC|Broadlink
[7040](../codes/fan/7040.json)|Aria|ESPHome

#### Super Fan

| Code | Supported Models | Controller |
| ------------- | -------------------------- | ------------- |
[1060](../codes/fan/1060.json)|A1|Broadlink

#### Harbor Breeze

| Code | Supported Models | Controller |
| ------------- | -------------------------- | ------------- |
[1080](../codes/fan/1080.json)|A25-TX001-R1|Broadlink
[1081](../codes/fan/1081.json)|A25-TX025|Broadlink

#### Pacific

| Code | Supported Models | Controller |
| ------------- | -------------------------- | ------------- |
[1100](../codes/fan/1100.json)|Unknown|Broadlink

#### Europace

| Code | Supported Models | Controller |
| ------------- | -------------------------- | ------------- |
[1120](../codes/fan/1120.json)|Unknown|Broadlink

#### SMC

| Code | Supported Models | Controller |
| ------------- | -------------------------- | ------------- |
[1140](../codes/fan/1140.json)|SP486, SP483|Broadlink

#### Argo

| Code | Supported Models | Controller |
| ------------- | -------------------------- | ------------- |
[1180](../codes/fan/1180.json)|Standy|Broadlink

#### DCG

| Code | Supported Models | Controller |
| ------------- | -------------------------- | ------------- |
[1200](../codes/fan/1200.json)|Unknown|Broadlink

#### Mitsubishi

| Code | Supported Models | Controller |
| ------------- | -------------------------- | ------------- |
[1220](../codes/fan/1220.json)|C56-RW5|Broadlink

#### Panasonic

| Code | Supported Models | Controller |
| ------------- | -------------------------- | ------------- |
[1221](../codes/fan/1221.json)|F-60GDN, F-60GDS|Broadlink

#### Mallory

| Code | Supported Models | Controller |
| ------------- | -------------------------- | ------------- |
[1240](../codes/fan/1240.json)|Air Timer TS+|Broadlink
