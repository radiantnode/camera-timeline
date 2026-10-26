"""Constants for Camera Timeline."""

DOMAIN = "camera_timeline"

#: Where the timeline lives, under Home Assistant's config folder. Outside
#: `www`, so pictures are only handed out to someone signed in.
FOLDER = "camera_timeline"

CONF_CAMERAS = "cameras"
CONF_STREAM_CAMERAS = "stream_cameras"
CONF_QUIET_CAMERAS = "quiet_cameras"
CONF_INTERVAL = "interval"
CONF_KEEP_HOURS = "keep_hours"

#: A picture of each camera this often (seconds); 0 for never.
DEFAULT_INTERVAL = 60
#: How long pictures are kept.
DEFAULT_KEEP_HOURS = 24

#: Frames in a burst, and how far apart (seconds).
BURST_FRAMES = 5
BURST_GAP_S = 0.5
#: Longest a capture may take, from asking to the last frame. A battery
#: camera can take most of this to wake.
CAPTURE_TIMEOUT_S = 40
#: The width pictures are kept at; anything asked for is resized from this.
KEEP_WIDTH = 1280

#: Cameras from these integrations are battery cameras whose own picture is
#: only as fresh as their last wake, so they're captured from their stream.
#: Any other camera is captured from its snapshot unless set otherwise.
STREAM_PLATFORMS = {"eufy_sdk", "eufy_security"}

#: Event entities and the device classes of binary sensors that count as an
#: event when they turn on, by the camera's device.
EVENT_DEVICE_CLASSES = {"motion", "occupancy", "presence", "moving", "sound", "vibration", "door", "opening"}

#: An event this soon after the camera's last event burst is marked on that
#: burst instead of taking another: a busy motion sensor fires every few
#: seconds while someone moves about.
EVENT_COOLDOWN_S = 30

#: A sensor that turns on this soon after the timeline started taking a
#: picture of its camera is the picture's doing, not an event: a doorbell's
#: motion sensor trips when it's woken for a snapshot.
SELF_TRIGGER_S = 8
#: An event entity whose event is older than this when its state arrives is
#: being restored (Home Assistant starting), not firing.
STALE_EVENT_S = 60

#: Watching a camera for movement (`camera_timeline/watch`): a frame looked
#: at this often, quiet for this long after it says something moved, and
#: never watched longer than this, whatever the watcher does.
WATCH_EVERY_S = 0.5
WATCH_REARM_S = 10
WATCH_MOST_S = 600

#: A HomeBase 2 writes its recording of a detection after the fact; the clip
#: is fetched this long after the event (eufy-sdk bridge only).
CLIP_DELAY_S = 45
CLIP_TIMEOUT_S = 120
