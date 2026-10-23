"""Report hardware video codecs for direct-play and transcode decisions.

Amlogic's vcodec_profile lists one decoder per line as `name:features;`.
Unsupported codecs appear as `<codec>_unsupport`, such as AV1 on S922X.
Kodi can fall back to software decoding, which is too slow for 4K AV1 or
HEVC on these devices.
"""
PROFILE = "/sys/class/amstream/vcodec_profile"

# Map Amlogic driver names to FFmpeg codec names.
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
    """Return hardware codec names in FFmpeg notation, or None if the driver
    profile cannot be read.
    """
    try:
        with open(PROFILE) as f:
            names = [line.split(":", 1)[0].strip() for line in f if ":" in line]
    except OSError:
        return None
    found = {CODECS[n] for n in names if n in CODECS}
    if "mpeg2video" in found:
        found.add("mpeg1video")
    return sorted(found)
