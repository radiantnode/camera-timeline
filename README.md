# Camera Timeline

What your Home Assistant cameras saw, kept for a while: a picture of each camera
on a schedule, a burst of frames at every motion, person or doorbell event, and
the camera's own recording of a detection where it has one. Services take a
picture now or look back through what's kept, and the media browser shows it all.

It works with any camera entity, and it was written for battery cameras, whose
picture in Home Assistant is only as fresh as their last wake.

- **A picture on a schedule**: every minute by default, spread across the cameras so
  they don't all wake at once. The timeline reads as "what it looked like at 2:41".
- **A burst at every event**: five frames half a second apart, enough to tell
  movement from stillness, when a motion, occupancy or similar sensor on the
  camera's device turns on, or one of its event entities fires (a doorbell press,
  a person detected).
- **The camera's own recording** of a detection, from a Eufy HomeBase 2 through
  the [eufy-sdk bridge](https://github.com/mega-yfue/ha-eufy-sdk-bridge).
- **Services** for a picture now, a burst, or a look back by time or by event, each
  answering with the pictures themselves (base64 JPEG), for scripts, automations and
  AI agents.
- **Media > Camera Timeline**: each camera's captures newest first, events named,
  recordings playable.

## Install

**HACS:** add `https://github.com/radiantnode/camera-timeline` as a custom repository
(category *Integration*), install **Camera Timeline**, restart Home Assistant.

**By hand:** copy `custom_components/camera_timeline` into your config folder's
`custom_components` and restart.

Then **Settings > Devices & services > Add integration > Camera Timeline**, and pick
your cameras. Everything else has a default; change it later under **Configure**.

| Option | Default | |
| --- | --- | --- |
| Cameras | — | the cameras to keep a timeline of |
| A picture every | 60 s | 0 keeps only events and what you ask for |
| Keep pictures for | 24 h | up to a week |
| Capture these from their stream | — | for battery cameras not from eufy-sdk (see below) |
| Only at events and when asked | — | no scheduled pictures: battery doorbells, and cameras whose own motion sensor trips when they're woken |

## How a picture is taken

- **From the snapshot** for most cameras: a mains-powered camera answers with a
  current picture, and that's the cheapest way to get one.
- **From the stream** for battery cameras, whose snapshot is the last thing they
  saw. Cameras from `eufy_sdk` (and the older `eufy_security`) are captured this way
  automatically; add others under *Capture these from their stream*. ffmpeg reads
  the camera's stream at 4 frames a second while pictures are wanted, and keeps it
  open for 15 seconds after, so a look that follows a scheduled picture finds the
  camera already awake. On an eufy-sdk bridge, that also avoids reopening a stream
  that has only just closed, which the bridge refuses for 30 seconds.

A camera is captured one way at a time. A look that comes while a scheduled picture
is being taken waits for it.

## Events

Entities on the **same device** as the camera are watched:

- **binary sensors** with a device class of motion, occupancy, presence, moving,
  sound, vibration, door or opening, when they turn on;
- **event entities**, when they fire. The event's type is kept (`person`, `pet`,
  `motion`…), and a doorbell's press is kept as `doorbell`.

Not counted as events: a state restored when Home Assistant starts (coming back
from unavailable), an event entity's event more than a minute old, and a sensor that
trips within 8 seconds of the timeline starting a picture of its camera. An Aqara G4
doorbell's motion sensor trips every time it's woken for a snapshot, so without that
rule every scheduled picture of it read as motion. Such a camera belongs under *Only
at events and when asked* anyway.

Each event takes a burst. An event within 30 seconds of the camera's last burst is
counted on that burst instead of taking another, because a busy motion sensor fires
every few seconds while someone moves about. For a camera from `eufy_sdk`, the
HomeBase's own recording of the detection is fetched 45 seconds later from the
bridge's `/clip/<serial>` and kept with it.

A Eufy HomeBase only detects (and so only sends events) in a guard mode with motion
detection on. While it's disarmed there are no events, and the timeline keeps its
scheduled pictures.

## Services

All three answer with data (`return_response`).

### `camera_timeline.snapshot`

A picture taken now, or `burst: true` for five, kept on the timeline:

```yaml
action: camera_timeline.snapshot
data:
  entity_id: camera.patio
  burst: true      # optional
  width: 1280      # optional
response_variable: shot
# shot = {"time": "2026-10-26T18:13:45+00:00",
#         "frames": [{"offset": 0.0, "image": "<base64 JPEG>"}, ...]}
```

### `camera_timeline.get_frames`

What a camera saw, newest first. By count (`wakes`), or every capture in a window
(`minutes`, or `since` and `until`); `most` thins a window to that many, evenly
spread, and then the answer also lists the time of every capture in it, so a caller
can tell a quiet stretch from a gap. `events_only` keeps only captures that caught
an event; `frames` keeps that many frames from the middle of each.

```yaml
action: camera_timeline.get_frames
data:
  entity_id: camera.patio
  minutes: 60
  most: 12
  width: 640
response_variable: timeline
# {"wakes": [{"time": ..., "reason": "sample" | "look" | "event",
#             "event": {"type": "person", "entity_id": "event.patio_detection"} | null,
#             "clip": false, "frames": [...]}, ...],
#  "times": ["...", ...]}
```

### `camera_timeline.get_events`

The events caught, newest first, for one camera or all:

```yaml
action: camera_timeline.get_events
data:
  hours: 24
response_variable: events
# {"events": [{"camera": "camera.patio", "time": ..., "type": "person",
#              "entity_id": ..., "clip": true, "repeats": 2}, ...]}
```

## Storage

Pictures are kept under `config/camera_timeline/<camera>/`, outside `www`, so they're
only handed out to someone signed in: through the services, and through the media
browser's signed addresses. They're stored 1280 wide, about 100–250 KB a picture
depending on the scene, so at the defaults a camera keeps roughly 150–350 MB, plus
bursts and recordings. Only the last `keep_hours` are kept.

## Good to know

- A camera's stream opened once a minute keeps a battery camera's radio busy for a
  few seconds each time. On battery, set a longer interval or 0.
- Removing a camera from the options stops its timeline; its pictures are kept until
  they age out.

## License

MIT
