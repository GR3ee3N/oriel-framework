"""What the box's hardware decodes, so an app can choose between playing a
file as it is and converting it first.

Amlogic's video drivers list their decoders in vcodec_profile, one per
line as `name:features;`, and a decoder the SoC lacks as `<codec>_unsupport`
(the S922X has no AV1, for one). Kodi decodes anything else in software,
which a 4K AV1 or HEVC file is far too much for.
"""
PROFILE = "/sys/class/amstream/vcodec_profile"

# Driver names, as ffmpeg names the codecs.
CODECS = {
    "h264": "h264", "mh264": "h264",
    "hevc": "hevc", "hevc_fb": "hevc",
    "vp9": "vp9", "vp9_fb": "vp9",
    "av1": "av1", "av1_fb": "av1", "amvdec_av1": "av1",
    "mpeg12": "mpeg2video", "mmpeg12": "mpeg2video",
    "mpeg4": "mpeg4", "mmpeg4": "mpeg4",
    "vc1": "vc1",
}


def decoders():
    """ffmpeg codec names the hardware decodes, or None if the box does not
    say (not an Amlogic one)."""
    try:
        with open(PROFILE) as f:
            names = [line.split(":", 1)[0].strip() for line in f if ":" in line]
    except OSError:
        return None
    found = {CODECS[n] for n in names if n in CODECS}
    if "mpeg2video" in found:
        found.add("mpeg1video")
    return sorted(found)
