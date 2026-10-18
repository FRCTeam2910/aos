# Calibrating camera exposure for the red and blue alliances

FRC field lighting is asymmetric. The red alliance end has arrays of red LEDs
behind/around the goals that dump a lot of light directly into camera sensors
(and the cameras are typically running close to monochrome for AprilTag
detection via `apriltag_detector ... --image_format=MONO8`). The result is
that a single fixed exposure value overexposes the red side and/or
underexposes the blue side. The `field_side_exposure_adjuster` app exists
specifically to apply different exposures depending on which alliance FMS
reports.

This document describes how to find the two correct exposure values at an
event and persist them.

## Background: where exposure comes from

There are three places an exposure value can originate, in order of
precedence (highest wins):

1. **`--exposure=N` flag on each `camera_readerN`** (see `camera_reader_main.cc`).
   `-1` (the default) defers to the constants file; `0` enables auto; `>0`
   is manual exposure in 100 µs units. This is set at `camera_readerN`
   startup and is what the camera uses until a `CameraStreamSettings`
   broadcast overrides it.
2. **`default_camera_stream_settings.exposure_100us`** in the per-robot entry
   of `constants.json` (built from `constants.jinja2.json`). This is the
   "no alliance known yet" baseline — applied at `camera_readerN` startup if
   no `--exposure` flag is set, and re-applied whenever `vision_constants_sender`
   is restarted. Because it has no alliance awareness, it cannot differentiate
   red from blue.
3. **`field_side_exposure_adjuster` broadcast** — sends `CameraStreamSettings`
   with `exposure_100us` once per second on `/camera0`, `/camera1`,
   `/camera2`, `/camera3` based on the `alliance` field of the `JoystickState`
   message on `/frc`. Each `camera_readerN` watches its own `/cameraN` channel
   and calls `v4l2_reader.SetExposure()` (which writes `V4L2_CID_EXPOSURE_ABSOLUTE`)
   when the value changes. This is the only path that selects per-alliance.

The adjuster has two relevant flags (`frc/vision/field_side_exposure_adjuster.cc`):
`--red_exposure` (C++ default 15) and `--blue_exposure` (C++ default 20), both
in units of 100 µs. So `15` = 1.5 ms.

**Important gate:** the adjuster returns early when `alliance == kInvalid`
(no FMS alliance available — common in the pit before practice matches). In
that state it broadcasts **nothing**, so the cameras fall back to whatever
`default_camera_stream_settings.exposure_100us` provides in `constants.json`.
This matters during pit tuning (see workaround below).

## Procedure: calibrating both values at an event

### Required setup

- USB connection between your laptop and the Orin on the same network
  (default Orin address is `10.TE.AM.1`, e.g. `10.29.10.1`).
- Foxglove (desktop app recommended for events) connected to the Orin's
  Foxglove WebSocket on port 8765, with the layout from
  `frc/vision/foxglove_camera_feeds.json` imported.
- The robot placed on the actual field, plugged into FMS so the driver
  station can report an alliance. **Without FMS in the loop, `alliance`
  will be `kInvalid` and the adjuster will not broadcast**, which makes
  tuning via the adjuster impossible — see "Pit-tuning workaround" below
  for the alternative.

### Step 1 — Stop the adjuster so you start from a clean baseline

SSH onto the Orin:

```
ssh pi@10.29.10.1
```

Stop the adjuster so it is not actively broadcasting any exposure that
would mask your tuning:

```
aos_starter stop field_side_exposure_adjuster
```

With the adjuster stopped, the cameras fall back to
`default_camera_stream_settings.exposure_100us` from `constants.json`.

### Step 2 — Calibrate the RED side value

1. Place the robot on the **red** end of the field.
2. Make sure FMS reports your alliance as red (the driver station's alliance
   indicator should show red, and `aos_dump /frc frc.JoystickState` should
   show `alliance: kRed`).
3. Start the adjuster with a starting red value (try the C++ default 15, or
   half of whatever the blue value currently is, since red typically needs
   *less* exposure due to the red LED arrays washing out the cameras):

   ```
   aos_starter start field_side_exposure_adjuster --red_exposure=15 --blue_exposure=20
   ```

   Use the same value for `--blue_exposure` during this step — you are only
   tuning red here; you'll tune blue in Step 3.

4. Watch the camera feeds in Foxglove and iterate:

   - Too bright / saturated / AprilTag detections dropping: lower the value.
     ```
     aos_starter restart field_side_exposure_adjuster --red_exposure=12 --blue_exposure=20
     ```
   - Too dark / noisy / AprilTags not visible: raise the value.
     ```
     aos_starter restart field_side_exposure_adjuster --red_exposure=20 --blue_exposure=20
     ```

   Restarting the adjuster re-reads the flags; the new value will reach all
   four cameras within ~1 second (the 1 Hz broadcast timer).

5. Iterate until the image is not saturated and AprilTag detections are
   solid on all four camera feeds. **Write this value down** — call it `R`.

### Step 3 — Calibrate the BLUE side value

1. Move the robot to the **blue** end of the field.
2. Confirm FMS now reports blue (`alliance: kBlue` in `aos_dump /frc frc.JoystickState`).
3. Restart the adjuster with your calibrated red value and a starting blue
   value (the C++ default is 20):

   ```
   aos_starter restart field_side_exposure_adjuster --red_exposure=R --blue_exposure=20
   ```

4. Iterate the blue value the same way you iterated red:

   ```
   aos_starter restart field_side_exposure_adjuster --red_exposure=R --blue_exposure=15
   ```

5. Tune until blue looks right. **Write this value down** — call it `B`.

### Step 4 — Verify the alliance flip

Before persisting, verify that the adjuster actually switches exposure when
the alliance changes:

1. With the adjuster running with `--red_exposure=R --blue_exposure=B`, have
   someone toggle the driver station's alliance (or wait for a real alliance
   assignment during a practice match).
2. Watch the camera feeds — exposure should visibly change ~1 s after the
   `JoystickState` alliance field flips.
3. If the exposure does not change, suspect that `alliance` is `kInvalid`
   on the `/frc` channel (use `aos_dump /frc frc.JoystickState` to verify).
   The adjuster will not broadcast anything in that state.

### Step 5 — Persist the values

Once `R` and `B` are confirmed, persist them so a reboot or redeploy does
not lose them. Add (or update) the `field_side_exposure_adjuster` app entry
in `frc/vision/single_node_camera_config.json`:

```json
{
  "name": "field_side_exposure_adjuster",
  "executable_name": "field_side_exposure_adjuster",
  "args": ["--red_exposure=R", "--blue_exposure=B"],
  "nodes": [ "orin" ]
}
```

(Replace `R` and `B` with the actual numbers you found.) Then redeploy:

```
bazel run -c opt --config=arm64 //frc/vision:download_stripped -- 10.TE.AM.101
```

For an event-side stopgap when you cannot redeploy, the values can be edited
directly in the on-device starter config and the adjuster restarted, but
this approach is fragile (the next redeploy will clobber it). Prefer editing
`single_node_camera_config.json` and redeploying.

## Pit-tuning workaround (no FMS in the loop)

Before practice matches begin on Friday morning — or in any situation where
your driver station is not connected to FMS and cannot report an alliance —
the adjuster's `kInvalid` gate means it will not broadcast anything, making
the procedure above impossible. The workaround is to tune exposure directly
on each `camera_readerN` via the `--exposure` flag:

```
aos_starter stop field_side_exposure_adjuster
aos_starter restart camera_reader0 --exposure=15 --ignore_timestamps --viddevice=/dev/videoa --isp_latency_ms=0
aos_starter restart camera_reader1 --exposure=15 --ignore_timestamps --viddevice=/dev/videob --isp_latency_ms=0
aos_starter restart camera_reader2 --exposure=15 --ignore_timestamps --viddevice=/dev/videoc --isp_latency_ms=0
aos_starter restart camera_reader3 --exposure=15 --ignore_timestamps --viddevice=/dev/videod --isp_latency_ms=0
```

The `--exposure=N` flag (100 µs units, `0` = auto) sets the camera's exposure
at `camera_readerN` startup, bypassing the adjuster entirely. Iterate `N`
the same way as red/blue above, applying the same value to all four
`camera_readerN` processes. This will only give you a single "no alliance"
value — you still need to find the actual `R` and `B` once FMS is in the loop
(presumably during the first practice match), and persist them per Step 5.

After pit tuning, revert the per-`camera_readerN` overrides so the adjuster
takes over once an alliance is known:

```
aos_starter restart camera_reader0 --ignore_timestamps --viddevice=/dev/videoa --isp_latency_ms=0
aos_starter restart camera_reader1 --ignore_timestamps --viddevice=/dev/videob --isp_latency_ms=0
aos_starter restart camera_reader2 --ignore_timestamps --viddevice=/dev/videoc --isp_latency_ms=0
aos_starter restart camera_reader3 --ignore_timestamps --viddevice=/dev/videod --isp_latency_ms=0
```

## Units and ranges

- All exposure values are in units of 100 microseconds (per
  `frc/vision/camera_settings.fbs` and the `--red_exposure` / `--blue_exposure`
  flag help strings). So `15` = 1.5 ms, `30` = 3.0 ms.
- Typical FRC events land in the 8–25 range. Very bright arenas (against
  white backdrops with strong LED lighting) can go as low as 5; dim
  practice fields can go to 30+.
- `0` enables the camera's auto-exposure (NOT recommended — auto-exposure
  cannot keep up with rapid field lighting changes and produces inconsistent
  detection during match play).
- Do not go below ~5–8 without verifying: dim scenes will not gather enough
  photons and AprilTag detection will fail in the opposite direction (noise
  rather than saturation).
