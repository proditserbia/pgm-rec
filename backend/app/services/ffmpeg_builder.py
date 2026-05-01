"""
FFmpeg command builder for PGMRec.

Translates a ChannelConfig into a subprocess-safe argument list.
Always use shell=False with the returned list — never join into a shell string.

Replicates record_rts1.bat behavior exactly:

  C:\\AutoRec\\ffmpeg\\bin\\ffmpeg.exe
    -f dshow -video_size 720x576 -framerate 25
    -i video=Decklink Video Capture:audio=Decklink Audio Capture
    -b:v 1500k -b:a 128k
    -vf drawtext=fontsize=13:...,scale=1024:576,yadif
    -f stream_segment -segment_time 00:05:00
    -segment_atclocktime 1 -reset_timestamps 1 -strftime 1
    -c:v libx264 -preset veryfast
    D:\\AutoRec\\record\\rts1\\1_record\\%d%m%y-%H%M%S.mp4

Filter chain order (matches bat): drawtext → scale → yadif

Phase 11: Capture input is now fully configurable per channel.
  - dshow: uses -video_size (device-specific flag, not the generic -s)
  - pixel_format / vcodec optional overrides are emitted when set
"""
from __future__ import annotations

import logging
import platform
import shlex
from pathlib import Path

from ..config.settings import resolve_channel_path
from ..models.schemas import ChannelConfig, OverlayConfig

logger = logging.getLogger(__name__)


# ─── FFmpeg filter escaping helpers ───────────────────────────────────────────

def _escape_fontfile(path: str) -> str:
    r"""
    Escape a filesystem path for use as the drawtext ``fontfile`` option value.

    Uses FFmpeg single-quote wrapping.  Inside single quotes:
      - backslash  ->  \\\\  (escaped to produce \\ which FFmpeg reads as \)
      - colon      ->  \:    (escaped so FFmpeg doesn't treat it as option separator)

    Example (Windows)::

      C:\Windows\Fonts\verdana.ttf  ->  'C\:\\Windows\\Fonts\\verdana.ttf'

    FFmpeg then resolves that to the actual path C:\Windows\Fonts\verdana.ttf.
    """
    escaped = path.replace("\\", "\\\\").replace(":", "\\:")
    return f"'{escaped}'"


def _escape_time_format(fmt: str) -> str:
    """
    Escape a strftime format string for use inside the drawtext ``text`` option.

    The text option value is always wrapped in single quotes (see
    :func:`_build_drawtext_filter`).  Inside single-quoted FFmpeg filter option
    values, only ``\\`` (backslash) and ``'`` (single quote) are special — colons
    and hyphens are **literal** and must not be escaped.  Escaping them with
    ``\\:`` / ``\\-`` would cause drawtext to receive those backslashes literally
    (since no unescaping happens inside single quotes), which prevents
    ``%{localtime:FORMAT}`` from being recognised and produces the FFmpeg error
    ``%{localtime} requires at most 1 arguments``.

    Example:
      %d-%m-%y %H:%M:%S  →  %d-%m-%y %H:%M:%S  (unchanged — no escaping needed)
    """
    # Only backslash and single-quote are special inside single-quoted FFmpeg
    # option values; escape them so the string is safe to embed in '...'.
    # Escape backslash first, then single-quote.  Order matters: escaping '
    # before \ would cause a newly-introduced \ (from \') to be re-escaped.
    return fmt.replace("\\", "\\\\").replace("'", "\\'")


def _build_drawtext_filter(overlay: OverlayConfig) -> str:
    """
    Build the ``drawtext=...`` filter string from overlay config.

    Selects the platform-appropriate font path and applies all necessary
    FFmpeg filter-level escaping.  The resulting string is passed directly
    as the -vf argument value (no additional shell quoting needed).
    """
    font_path = (
        overlay.fontfile_win if platform.system() == "Windows" else overlay.fontfile_linux
    )
    fontfile = _escape_fontfile(font_path)
    time_fmt = _escape_time_format(overlay.time_format)
    # Braces in the localtime macro must be escaped in Python f-strings with {{}}.
    # The colon separating "localtime" from the format string is a plain ":" —
    # drawtext expects a literal colon here.  We are inside single quotes so no
    # further FFmpeg option-level escaping is needed for that colon.
    text = f"'%{{localtime:{time_fmt}}}'"

    parts = [
        f"fontsize={overlay.fontsize}",
        f"fontcolor={overlay.fontcolor}",
        f"box={'1' if overlay.box else '0'}",
        f"boxcolor={overlay.boxcolor}",
        f"fontfile={fontfile}",
        f"text={text}",
        f"x={overlay.x}",
        f"y={overlay.y}",
    ]
    return "drawtext=" + ":".join(parts)


def _build_vf_chain(config: ChannelConfig) -> str:
    """
    Build the full -vf filter chain string.

    Order replicates record_rts1.bat: drawtext → scale → yadif.
    (The bat applies overlay on the raw 720x576 frame before scaling/deinterlace.)
    """
    filters: list[str] = []

    if config.filters.overlay.enabled:
        filters.append(_build_drawtext_filter(config.filters.overlay))

    filters.append(
        f"scale={config.filters.scale_width}:{config.filters.scale_height}"
    )

    if config.filters.deinterlace:
        filters.append("yadif")

    return ",".join(filters)


def _build_input_specifier(config: ChannelConfig) -> str:
    """
    Build the value for the -i flag.

    dshow  (Windows):  video=<name>:audio=<name>
    v4l2   (Linux):    /dev/video0  (audio handled separately via alsa/pulse)
    """
    cap = config.capture
    if cap.device_type == "dshow":
        return f"video={cap.video_device}:audio={cap.audio_device}"
    # v4l2 and fallback: just the video device
    return cap.video_device


def _build_capture_args(config: ChannelConfig) -> list[str]:
    """
    Build the complete list of capture input arguments (everything before the
    output section), i.e.:

        -f <device_type>
        -video_size <resolution>   (dshow)  OR  -s <resolution>  (other)
        -framerate <fps>
        [-pixel_format <fmt>]      (only when capture.pixel_format is set)
        [-vcodec <codec>]          (only when capture.vcodec is set)
        -i <input_specifier>

    Device-type notes:
    - dshow (Windows Decklink): FFmpeg's dshow demuxer uses ``-video_size``
      rather than the generic ``-s`` to set the capture resolution.  Using
      ``-s`` would silently scale the captured frames instead of requesting
      the correct size from the device, which produces incorrect output.
    - All other demuxers (v4l2, avfoundation, alsa, …): use the generic
      ``-s`` flag.

    Phase 11: pixel_format and vcodec are optional override fields on
    CaptureConfig — set them only if the hardware requires explicit
    specification (e.g. Decklink needing ``-pixel_format uyvy422``).
    """
    cap = config.capture
    args: list[str] = []

    # Demuxer
    args += ["-f", cap.device_type]

    # Frame size — dshow uses -video_size; generic demuxers use -s
    if cap.device_type == "dshow":
        args += ["-video_size", cap.resolution]
    else:
        args += ["-s", cap.resolution]

    # Frame rate
    args += ["-framerate", str(cap.framerate)]

    # Optional pixel format (e.g. uyvy422 for Decklink)
    if cap.pixel_format:
        args += ["-pixel_format", cap.pixel_format]

    # Optional input codec override (rarely needed; e.g. rawvideo)
    if cap.vcodec:
        args += ["-vcodec", cap.vcodec]

    # Input specifier
    args += ["-i", _build_input_specifier(config)]

    return args


def _output_pattern(config: ChannelConfig) -> str:
    """
    Build the strftime output path pattern for the stream_segment muxer.

    Returns a native-platform path string; pathlib handles separator differences.
    """
    seg = config.segmentation
    record_dir = resolve_channel_path(config.paths.record_dir)
    return str(record_dir / f"{seg.filename_pattern}.mp4")


# ─── Public API ───────────────────────────────────────────────────────────────

def _build_filter_complex_with_preview(config: ChannelConfig) -> str:
    """
    Build a ``-filter_complex`` string that branches the video pipeline into
    two labelled output pads for dual-output recording+preview.

    Output pads:
    - ``[main_v]`` — full-resolution recording video (all main filters applied)
    - ``[prev_v]`` — low-resolution preview video (scale + fps reduction only)

    The raw input ``[0:v]`` is split immediately so each branch processes the
    source independently.  The main branch applies the same filter chain as
    the normal single-output command (drawtext → scale → yadif); the preview
    branch only does scale + fps reduction.

    Example output for rts1 with overlay + deinterlace enabled::

        [0:v]split=2[raw_m][raw_p];
        [raw_m]drawtext=...,scale=1024:576,yadif[main_v];
        [raw_p]scale=480:270,fps=10,format=yuv420p[prev_v]
    """
    rpo = config.recording_preview_output  # guaranteed non-None by caller

    # ── Main branch: replicate the -vf chain ──────────────────────────────
    main_filters: list[str] = []
    if config.filters.overlay.enabled:
        main_filters.append(_build_drawtext_filter(config.filters.overlay))
    main_filters.append(
        f"scale={config.filters.scale_width}:{config.filters.scale_height}"
    )
    if config.filters.deinterlace:
        main_filters.append("yadif")
    main_chain = ",".join(main_filters)

    # ── Preview branch: scale + fps + yuv420p ────────────────────────────
    # h264_nvenc (and some other hardware encoders) only accept yuv420p; the
    # raw capture pixel format is often yuv422p (e.g. Decklink uyvy422 decoded
    # to yuv422p).  Adding format=yuv420p here ensures the preview encoder
    # always receives a compatible pixel format regardless of the input.
    # The main recording branch is left unchanged — libx264 accepts yuv422p
    # and the operator may intentionally want to keep that format.
    prev_chain = f"scale={rpo.width}:{rpo.height},fps={rpo.fps},format=yuv420p"

    return (
        f"[0:v]split=2[raw_m][raw_p];"
        f"[raw_m]{main_chain}[main_v];"
        f"[raw_p]{prev_chain}[prev_v]"
    )


def _build_recording_command_with_preview(config: ChannelConfig) -> list[str]:
    """
    Build a dual-output FFmpeg recording command that additionally sends a
    low-res preview stream to a UDP endpoint — Phase 12.

    Uses ``-filter_complex`` to split the pipeline:
    - First output:  main recording (stream_segment muxer → file)
    - Second output: UDP preview (mpegts muxer → UDP URL)

    ⚠️  NVENC inside recording process safety check:
    If ``recording_preview_output.video_codec == "h264_nvenc"`` and
    ``fail_safe_mode=True`` a WARNING is logged, reminding the operator that
    an NVENC failure here will crash the recording process, not just preview.

    Safe for ``subprocess.Popen(cmd, shell=False)``.
    """
    rpo = config.recording_preview_output  # guaranteed non-None + enabled by caller
    enc = config.encoding
    seg = config.segmentation

    if rpo.fail_safe_mode and rpo.video_codec == "h264_nvenc":
        logger.warning(
            "[ffmpeg-builder][%s] recording_preview_output: NVENC (h264_nvenc) is "
            "enabled inside the recording FFmpeg process (fail_safe_mode=True). "
            "If h264_nvenc is unavailable or misconfigured the ENTIRE recording "
            "process will crash, not just preview.  "
            "Set video_codec='libx264' for a CPU-safe alternative.",
            config.id,
        )

    cmd: list[str] = [config.ffmpeg_path]

    # ── Input ──────────────────────────────────────────────────────────────
    cmd += _build_capture_args(config)

    # ── Filter complex: split raw input into main_v + prev_v ───────────────
    cmd += ["-filter_complex", _build_filter_complex_with_preview(config)]

    # ── First output: main recording ───────────────────────────────────────
    cmd += ["-map", "[main_v]", "-map", "0:a"]
    cmd += ["-b:v", enc.video_bitrate]
    cmd += ["-b:a", enc.audio_bitrate]
    cmd += ["-c:v", enc.video_codec]
    cmd += ["-preset", enc.preset]
    cmd += ["-f", "stream_segment"]
    cmd += ["-segment_time", seg.segment_time]
    if seg.segment_atclocktime:
        cmd += ["-segment_atclocktime", "1"]
    if seg.reset_timestamps:
        cmd += ["-reset_timestamps", "1"]
    if seg.strftime:
        cmd += ["-strftime", "1"]
    cmd.append(_output_pattern(config))

    # ── Second output: UDP preview ──────────────────────────────────────────
    cmd += ["-map", "[prev_v]"]
    if rpo.audio_enabled:
        cmd += ["-map", "0:a"]
        cmd += ["-c:a", rpo.audio_codec]
        cmd += ["-b:a", rpo.audio_bitrate]
        cmd += ["-ar", str(rpo.audio_sample_rate)]
    else:
        cmd += ["-an"]

    cmd += ["-c:v", rpo.video_codec]
    if rpo.preset:
        cmd += ["-preset", rpo.preset]
    if rpo.tune and rpo.video_codec == "h264_nvenc":
        cmd += ["-tune", rpo.tune]
    cmd += ["-b:v", rpo.bitrate]

    cmd += ["-f", rpo.format]
    cmd.append(rpo.url)

    return cmd


def build_ffmpeg_command(config: ChannelConfig) -> list[str]:
    """
    Build a complete FFmpeg recording command as a subprocess argument list.

    Safe for ``subprocess.Popen(cmd, shell=False)``.
    Never pass the result to a shell — it is not shell-escaped.

    Mirrors record_rts1.bat parameter-by-parameter.

    Phase 12: if ``recording_preview_output.enabled`` is True, delegates to
    :func:`_build_recording_command_with_preview` which uses ``-filter_complex``
    and a second UDP output instead of a simple ``-vf`` chain.
    """
    rpo = config.recording_preview_output
    if rpo is not None and rpo.enabled:
        return _build_recording_command_with_preview(config)

    cap = config.capture
    enc = config.encoding
    seg = config.segmentation

    cmd: list[str] = [config.ffmpeg_path]

    # ── Input ──────────────────────────────────────────────────────────────
    cmd += _build_capture_args(config)

    # ── Encoding ───────────────────────────────────────────────────────────
    cmd += ["-b:v", enc.video_bitrate]
    cmd += ["-b:a", enc.audio_bitrate]

    # ── Filters ────────────────────────────────────────────────────────────
    vf = _build_vf_chain(config)
    if vf:
        cmd += ["-vf", vf]

    # ── Segmentation muxer ─────────────────────────────────────────────────
    cmd += ["-f", "stream_segment"]
    cmd += ["-segment_time", seg.segment_time]
    if seg.segment_atclocktime:
        cmd += ["-segment_atclocktime", "1"]
    if seg.reset_timestamps:
        cmd += ["-reset_timestamps", "1"]
    if seg.strftime:
        cmd += ["-strftime", "1"]

    # ── Codec (after muxer flags, before output) ───────────────────────────
    cmd += ["-c:v", enc.video_codec]
    cmd += ["-preset", enc.preset]

    # ── Output pattern ─────────────────────────────────────────────────────
    cmd.append(_output_pattern(config))

    return cmd


def format_command_for_log(cmd: list[str]) -> str:
    """Return a human-readable representation of the command (uses shlex quoting)."""
    return shlex.join(cmd)


def build_hls_preview_command(config: ChannelConfig, output_dir: Path) -> list[str]:
    """
    Build an FFmpeg HLS preview command as a subprocess argument list — Phase 5.

    Produces a low-resolution HLS stream (index.m3u8 + *.ts segments) written
    to *output_dir*.  Uses the hardware capture device directly
    (``input_mode == "direct_capture"``).

    Key properties:
    - Same capture source as recording (hardware must support concurrent access)
    - Scale + fps reduction for low bandwidth
    - Audio disabled (-an)
    - Output: HLS muxer writing to output_dir/index.m3u8
    - Encoder configurable (default libx264 / ultrafast; GPU variant added later)

    Safe for ``subprocess.Popen(cmd, shell=False)``.
    Never pass the result to a shell — it is not shell-escaped.

    Notes:
    - input_mode == "disabled": raises ValueError — caller should have blocked
      this earlier; this is a safety belt.
    - input_mode == "from_recording_output": use
      :func:`build_hls_preview_from_file_command` instead; the caller is
      responsible for selecting the source file.
    - input_mode == "direct_capture" (default): opens the same hardware device.
      On systems with a single Blackmagic Decklink input this WILL fail if
      recording is already running, because the Decklink SDK only allows one
      owner per input.  Set preview.input_mode = "from_recording_output" to
      avoid this — preview will read completed segments instead of opening
      the device.
    """
    cap = config.capture
    preview = config.preview

    input_mode = getattr(preview, "input_mode", "direct_capture")
    if input_mode == "disabled":
        raise ValueError(
            "build_hls_preview_command called with input_mode='disabled'. "
            "The caller should have rejected this request before reaching the builder."
        )
    if input_mode == "from_recording_output":
        raise ValueError(
            "build_hls_preview_command called with input_mode='from_recording_output'. "
            "Use build_hls_preview_from_file_command() for file-based preview."
        )
    if input_mode == "from_udp":
        raise ValueError(
            "build_hls_preview_command called with input_mode='from_udp'. "
            "Use build_hls_preview_from_udp_command() for UDP-based preview."
        )

    playlist_path = str(output_dir / "index.m3u8")
    segment_pattern = str(output_dir / "seg%05d.ts")

    cmd: list[str] = [config.ffmpeg_path]

    # ── Suppress interactive prompts ───────────────────────────────────────
    cmd += ["-y"]

    # ── Input ──────────────────────────────────────────────────────────────
    cmd += _build_capture_args(config)

    # ── Video filters: scale + fps ─────────────────────────────────────────
    cmd += ["-vf", f"scale={preview.width}:{preview.height},fps={preview.hls_fps}"]

    # ── Disable audio (preview is video-only) ─────────────────────────────
    cmd += ["-an"]

    # ── Encoding ───────────────────────────────────────────────────────────
    cmd += ["-c:v", preview.encoder]
    if preview.encoder in ("libx264", "libx265"):
        cmd += ["-preset", "ultrafast"]
    cmd += ["-b:v", preview.video_bitrate]

    # ── HLS muxer ─────────────────────────────────────────────────────────
    cmd += ["-f", "hls"]
    cmd += ["-hls_time", str(preview.segment_time)]
    cmd += ["-hls_list_size", str(preview.list_size)]
    # delete_segments: remove old .ts files; append_list: don't rewrite whole playlist
    cmd += ["-hls_flags", "delete_segments+append_list"]
    cmd += ["-hls_segment_filename", segment_pattern]

    # ── Output playlist ────────────────────────────────────────────────────
    cmd.append(playlist_path)

    return cmd


def build_hls_preview_from_file_command(
    config: ChannelConfig,
    input_file: Path,
    output_dir: Path,
) -> list[str]:
    """
    Build an FFmpeg HLS preview command that reads from a completed segment file.

    Used for ``preview.input_mode = "from_recording_output"`` — Phase 10.

    This approach **never opens the capture device**, so recording and preview
    can coexist on single-input Blackmagic Decklink systems that allow only one
    device owner at a time.

    Behaviour:
    - Reads *input_file* at real-time speed (``-re``) looping indefinitely
      (``-stream_loop -1``).
    - Produces the same low-res HLS output as the direct-capture command.
    - The caller (HlsPreviewManager watchdog) is responsible for stopping the
      process and restarting with a newer file when a newer completed segment
      becomes available.

    Safe for ``subprocess.Popen(cmd, shell=False)``.
    Never pass the result to a shell — it is not shell-escaped.
    """
    preview = config.preview

    playlist_path = str(output_dir / "index.m3u8")
    segment_pattern = str(output_dir / "seg%05d.ts")

    cmd: list[str] = [config.ffmpeg_path]

    # ── Suppress interactive prompts ───────────────────────────────────────
    cmd += ["-y"]

    # ── Input: loop the file at real-time speed ────────────────────────────
    # -re          : read at native playback speed (1×); avoids flooding HLS
    # -stream_loop : loop indefinitely so preview never stops between segment
    #                 switches; caller kills the process when a newer file appears
    cmd += ["-re"]
    cmd += ["-stream_loop", "-1"]
    cmd += ["-i", str(input_file)]

    # ── Video filters: scale + fps ─────────────────────────────────────────
    cmd += ["-vf", f"scale={preview.width}:{preview.height},fps={preview.hls_fps}"]

    # ── Disable audio (preview is video-only) ─────────────────────────────
    cmd += ["-an"]

    # ── Encoding ───────────────────────────────────────────────────────────
    cmd += ["-c:v", preview.encoder]
    if preview.encoder in ("libx264", "libx265"):
        cmd += ["-preset", "ultrafast"]
    cmd += ["-b:v", preview.video_bitrate]

    # ── HLS muxer ─────────────────────────────────────────────────────────
    cmd += ["-f", "hls"]
    cmd += ["-hls_time", str(preview.segment_time)]
    cmd += ["-hls_list_size", str(preview.list_size)]
    cmd += ["-hls_flags", "delete_segments+append_list"]
    cmd += ["-hls_segment_filename", segment_pattern]

    # ── Output playlist ────────────────────────────────────────────────────
    cmd.append(playlist_path)

    return cmd



# ─── Codec compatibility sets for browser HLS ─────────────────────────────────

# H.264-producing video codecs recognised by browser HLS (all produce AVC/H.264)
_HLS_H264_VIDEO_CODECS = frozenset({
    "libx264",
    "h264_nvenc",
    "h264_amf",
    "h264_qsv",
    "h264_v4l2m2m",
    "h264_videotoolbox",
})

# AAC-producing audio codecs compatible with browser HLS
_HLS_AAC_AUDIO_CODECS = frozenset({
    "aac",
    "aac_latm",
    "libfdk_aac",
})


def build_hls_preview_from_udp_command(
    config: ChannelConfig,
    output_dir: Path,
) -> list[str]:
    """
    Build an FFmpeg HLS preview command that reads from a UDP stream — Phase 12.

    Used for ``preview.input_mode = "from_udp"``.

    The UDP stream is produced by the recording FFmpeg process via
    ``recording_preview_output``.  Since the stream is already encoded
    (H.264 video + optional AAC audio), this command simply remuxes
    MPEG-TS → HLS without re-encoding (``-c:v copy``).

    Key properties:
    - Input: MPEG-TS over UDP (``recording_preview_output.url``)
    - ``-fflags +nobuffer+genpts``: low-latency flags (do not buffer frames;
      generate PTS from DTS if missing — guards against incomplete timestamps
      from some UDP streams)
    - Video: ``-c:v copy``  (stream already H.264; no re-encode needed)
    - Audio: ``-c:a copy`` if ``recording_preview_output.audio_enabled``,
      else ``-an``
    - Output: HLS muxer writing to output_dir/index.m3u8

    Raises:
      ValueError  if ``recording_preview_output`` is not configured on the channel.
      ValueError  if the configured video codec is not H.264-compatible (browser
                  HLS requires H.264 video for cross-browser playback).
      ValueError  if audio is enabled and the configured audio codec is not AAC
                  (browser HLS requires AAC audio for cross-browser playback).

    Safe for ``subprocess.Popen(cmd, shell=False)``.
    Never pass the result to a shell — it is not shell-escaped.
    """
    rpo = config.recording_preview_output
    if rpo is None:
        raise ValueError(
            f"build_hls_preview_from_udp_command: channel '{config.id}' has "
            "no recording_preview_output configured.  Set "
            "recording_preview_output.enabled=True and provide a UDP URL."
        )

    # ── Browser HLS codec compatibility check ─────────────────────────────────
    # HLS served to browsers must use H.264 video; all other codecs (e.g. mpeg4,
    # hevc, vp9) are either unsupported or require MSE extensions not universally
    # available.  Reject non-H.264 early with a clear message rather than
    # producing a stream that silently fails to play.
    if rpo.video_codec not in _HLS_H264_VIDEO_CODECS:
        raise ValueError(
            f"build_hls_preview_from_udp_command: channel '{config.id}' uses "
            f"video_codec='{rpo.video_codec}' which is not H.264-compatible. "
            "Browser HLS requires H.264 video (e.g. libx264 or h264_nvenc). "
            "Set recording_preview_output.video_codec to a supported H.264 encoder."
        )

    # Audio codec must be AAC when audio is enabled (MP3, Opus, etc. are not
    # reliably supported in HLS by all browsers).
    if rpo.audio_enabled and rpo.audio_codec not in _HLS_AAC_AUDIO_CODECS:
        raise ValueError(
            f"build_hls_preview_from_udp_command: channel '{config.id}' uses "
            f"audio_codec='{rpo.audio_codec}' which is not AAC-compatible. "
            "Browser HLS requires AAC audio. "
            "Set recording_preview_output.audio_codec to 'aac'."
        )

    preview = config.preview
    playlist_path = str(output_dir / "index.m3u8")
    segment_pattern = str(output_dir / "seg%05d.ts")

    cmd: list[str] = [config.ffmpeg_path]

    # ── Suppress interactive prompts ───────────────────────────────────────
    cmd += ["-y"]

    # ── Low-latency input flags ────────────────────────────────────────────
    # +nobuffer : read packets immediately without buffering
    # +genpts   : generate PTS from DTS when PTS is missing (common in UDP)
    cmd += ["-fflags", "+nobuffer+genpts"]

    # ── UDP input ──────────────────────────────────────────────────────────
    cmd += ["-i", rpo.url]

    # ── Video: copy H.264 stream (already encoded by recording process) ────
    cmd += ["-c:v", "copy"]

    # ── Audio ──────────────────────────────────────────────────────────────
    if rpo.audio_enabled:
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-an"]

    # ── HLS muxer ─────────────────────────────────────────────────────────
    cmd += ["-f", "hls"]
    cmd += ["-hls_time", str(preview.segment_time)]
    cmd += ["-hls_list_size", str(preview.list_size)]
    cmd += ["-hls_flags", "delete_segments+append_list"]
    cmd += ["-hls_segment_filename", segment_pattern]

    # ── Output playlist ────────────────────────────────────────────────────
    cmd.append(playlist_path)

    return cmd


def build_preview_command(config: ChannelConfig) -> list[str]:
    """
    Build a lightweight FFmpeg MJPEG preview command as a subprocess argument list.

    Produces low-resolution, low-fps MJPEG frames on stdout (pipe:1).
    The caller is responsible for reading stdout and distributing frames.

    Key differences from the recording command:
    - Audio disabled (-an)
    - Scale down + fps reduce via -vf
    - Output: raw MJPEG frames to stdout (-f mjpeg pipe:1)
    - No stream_segment muxer, no file output
    - No overlay filter (saves CPU; preview does not need watermark)
    """
    preview = config.preview

    cmd: list[str] = [config.ffmpeg_path]

    # ── Input ──────────────────────────────────────────────────────────────
    cmd += _build_capture_args(config)

    # ── Video filters: scale down + fps reduction ──────────────────────────
    cmd += ["-vf", f"scale={preview.scale},fps={preview.fps}"]

    # ── Disable audio (preview is video-only) ─────────────────────────────
    cmd += ["-an"]

    # ── Output: MJPEG frames to stdout ────────────────────────────────────
    cmd += ["-f", "mjpeg"]
    cmd += ["-q:v", "5"]  # JPEG quality 1=best, 31=worst; 5 is a good preview quality
    cmd.append("pipe:1")

    return cmd
