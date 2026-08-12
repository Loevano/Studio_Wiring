# Rack Editor

The Rack Editor provides a physical-layout view of studio devices without changing routing or patch behavior. Open **Rack Editor** from the shell navigation.

## Device data

Devices may include these optional fields:

```json
{
  "rack_mountable": true,
  "location": "Rack",
  "rack_units": 2,
  "rack_position": {
    "rack": 1,
    "start_u": 5
  }
}
```

- `rack_mountable` must be the boolean `true` for the device to appear in the Rack Editor. A missing value means `false`.
- `location` is `"Desk"` or `"Rack"`. A missing value means `"Desk"` for backward compatibility. A device cannot use `"Rack"` unless it is rack mountable.
- `rack_units` is an integer from 1 through 16. A missing value means `1`.
- `rack_position` is absent, `null`, or an object with `rack` from 1 through 4 and `start_u` from 1 through 16.
- `start_u` is the lowest occupied rack unit. A device of height `rack_units` occupies `start_u` through `start_u + rack_units - 1`.

Rack-mountable devices may be unplaced by leaving `rack_position` absent or `null`. Desk devices cannot have a rack position.

## Placement rules

The editor displays four 16U racks. Each rack is shown conventionally with U16 at the top and U1 at the bottom. A placed device appears as one block spanning its complete occupied range.

A placement is valid when:

- the device is assigned to `"Rack"`;
- the device has `"rack_mountable": true`;
- its complete occupied range fits within U1–U16;
- it does not overlap another device in the same rack.

Invalid moves are rejected without changing the model. Changing a placed device to `"Desk"` removes its rack position. Marking a device as rack mountable does not place it automatically; it remains in the unplaced list until a valid position is chosen. A successful drag into a rack assigns its location to `"Rack"`.

## Using the editor

The Rack Editor only lists devices explicitly marked `Rack mountable` in **Devices & Ports**. It does not infer eligibility from a device name, type, or location, so speakers, S1 control surfaces, and other unmarked gear stay out of its selector, unplaced list, and rack views. A rack-mountable device may still have location `Desk`; it appears as unplaced until it is dropped into a rack or placed with the form.

The unplaced Rack list shows rack devices that do not yet have a position. The four rack views show placed devices and their occupied unit ranges. With a pointer, drag an unplaced or already placed device onto the desired rack unit. A green rack outline marks a valid target; a red outline marks an overlap or out-of-bounds target. Invalid drops leave the saved placement unchanged.

Every placement operation is also available through labelled form controls:

1. Select a rack-mountable device.
2. Select Rack 1–4.
3. Select the lowest occupied U position.
4. Apply the placement.

Use the same controls to move a placed device. Use the remove/unplace action to return it to the unplaced Rack list. The controls do not require drag-and-drop and remain usable with a keyboard and assistive technology.

Rack edits use the same model dirty-state and save workflow as other device edits. Save the device configuration to persist them.

## Existing projects

Existing device JSON remains loadable, but devices without `rack_mountable: true` do not appear in the Rack Editor. Any legacy rack position stays dormant until the device is explicitly marked; this prevents an old `location: "Rack"` value from admitting speakers or controllers. In **Devices & Ports**, check `Rack mountable` once for each piece of eligible equipment and save the device configuration. Legacy devices without location or height still default to `Desk` and 1U.
