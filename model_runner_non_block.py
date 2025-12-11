# model_runner.py
import time
import os
import mmap
import socket
import numpy as np
from multiprocessing import Process
from dataclasses import dataclass
import threading
import struct
import traceback

SHM_PREFIX = "/dev/shm/camera_shm_"
HEADER_SIZE = 32  # QIIQQ => 8 + 4 + 4 + 8 + 8

# Safety / tuning parameters (tweak for your host)
MAX_CONCURRENT_PIPELINES = 2   # emergency concurrency limit for software encode
TARGET_FPS = 12                # throttle pushes to this FPS per camera
RTSP_HOST = "127.0.0.1"
RTSP_PORT = 8554
RTSP_CONNECT_TIMEOUT = 0.5     # seconds for quick responsiveness
QUEUE_FRAMES = 3               # how many frames to allow in queue (approx)
DEFAULT_BITRATE = 800          # kbit/s conservative default for x264enc
DEFAULT_PIPELINE_FPS = TARGET_FPS


def read_header(mm):
    """Read 32-byte header from an mmap object.

    Returns (frame_seq, width, height, frame_no, ts_us) or None if not available.
    """
    try:
        mm.seek(0)
        header_data = mm.read(HEADER_SIZE)
    except Exception:
        return None

    if len(header_data) < HEADER_SIZE:
        return None

    try:
        return struct.unpack("QIIQQ", header_data)
    except struct.error:
        return None


def is_rtsp_alive(host=RTSP_HOST, port=RTSP_PORT, timeout=RTSP_CONNECT_TIMEOUT):
    """Quick TCP check to see if an RTSP server is listening."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


@dataclass
class CameraSHMInfo:
    cam_id: int
    width: int
    height: int
    size: int
    shm_path: str
    mmap_obj: mmap.mmap
    appsrc: any = None
    pipeline: any = None
    last_push_time: float = 0.0
    pipeline_ok: bool = False
    last_pipeline_attempt: float = 0.0


class SHMModelRunner:
    def __init__(self):
        self.cameras: dict[int, CameraSHMInfo] = {}
        self.stop_flag = False
        self.lock = threading.Lock()

        # Initialize GStreamer in this process
        import gi
        gi.require_version('Gst', '1.0')
        from gi.repository import Gst, GLib
        Gst.init(None)
        self.Gst = Gst
        self.GLib = GLib

    # ---------------- discovery / attach / detach ----------------
    def discover_cameras(self):
        """Discover camera SHM files and attach if needed. Also prune missing ones."""
        try:
            for fname in os.listdir("/dev/shm"):
                if fname.startswith("camera_shm_"):
                    parts = fname.split("_")
                    if len(parts) >= 3:
                        try:
                            cam_id = int(parts[2])
                        except Exception:
                            continue
                        if cam_id not in self.cameras:
                            self._attach_to_camera(cam_id)
        except FileNotFoundError:
            # /dev/shm missing or inaccessible (rare)
            return

        # prune removed SHM files
        to_remove = []
        for cam_id, info in list(self.cameras.items()):
            if not os.path.exists(info.shm_path):
                to_remove.append(cam_id)
        for cam_id in to_remove:
            print(f"[Model] Camera {cam_id} SHM disappeared — detaching")
            self._detach_camera(cam_id)

    def _attach_to_camera(self, cam_id):
        """Attach to existing SHM file and create pipeline if RTSP is up."""
        shm_path = f"/dev/shm/camera_shm_{cam_id}"
        if not os.path.exists(shm_path):
            return

        # Emergency concurrency check
        active_pipelines = sum(1 for c in self.cameras.values() if c.pipeline is not None)
        if active_pipelines >= MAX_CONCURRENT_PIPELINES:
            print(f"[Model] Too many pipelines ({active_pipelines}), deferring attach for cam{cam_id}")
            return

        try:
            size = os.path.getsize(shm_path)
            f = open(shm_path, "r+b")
            mm = mmap.mmap(f.fileno(), size, access=mmap.ACCESS_READ)
        except Exception as e:
            print(f"[Model] Camera {cam_id}: failed to open mmap: {e}")
            return

        hdr = read_header(mm)
        if hdr is None:
            print(f"[Model] Camera {cam_id}: header not ready — keeping mmap open but deferring pipeline")
            # Create info without pipeline so we can read header/frames but skip pushing
            info = CameraSHMInfo(cam_id, 0, 0, size, shm_path, mm, None, None)
            self.cameras[cam_id] = info
            return

        frame_seq, width, height, frame_no, ts_us = hdr

        # Create info object first (pipeline may be None until server available)
        info = CameraSHMInfo(cam_id, width, height, size, shm_path, mm, None, None)
        self.cameras[cam_id] = info

        # Only create pipeline if RTSP server is reachable
        if not is_rtsp_alive():
            print(f"[Model] RTSP server unreachable — skipping pipeline for cam{cam_id} (will retry)")
            info.pipeline_ok = False
            info.last_pipeline_attempt = time.time()
            return

        try:
            appsrc, pipeline = self._create_rtsp_pipeline(cam_id, width, height)
            info.appsrc = appsrc
            info.pipeline = pipeline
            info.pipeline_ok = True
            print(f"[Model] Attached camera {cam_id}: {width}x{height} — pipeline running")
        except Exception as e:
            info.pipeline_ok = False
            info.last_pipeline_attempt = time.time()
            print(f"[Model] Failed to create pipeline for cam{cam_id}: {e}")
            # keep mmap open to continue reading (no pushing)

    def _detach_camera(self, cam_id):
        info = self.cameras.get(cam_id)
        if not info:
            return

        # try to tear down pipeline
        try:
            if info.pipeline:
                info.pipeline.set_state(self.Gst.State.NULL)
        except Exception:
            pass

        # close mmap
        try:
            if info.mmap_obj:
                info.mmap_obj.close()
        except Exception:
            pass

        self.cameras.pop(cam_id, None)
        print(f"[Model] Detached Camera {cam_id}")

    # ---------------- RTSP / GStreamer pipeline creation ----------------
    def _create_rtsp_pipeline(self, cam_id, width, height):
        Gst = self.Gst

        rtsp_url = f"rtsp://{RTSP_HOST}:{RTSP_PORT}/cam{cam_id}_processed"

        # approximate bytes per frame (RGB)
        approx_frame_bytes = width * height * 3
        queue_bytes = max(approx_frame_bytes * QUEUE_FRAMES, 5_000_000)

        # Components:
        # - appsrc (RGB input) -> queue (bounded, leaky=downstream) -> videoconvert -> encode -> rtspclientsink
        # Lower fps & lower bitrate by default to be conservative and avoid CPU exhaustion.
        pipeline_str = f"""
            appsrc name=src !
            queue max-size-buffers=0 max-size-bytes={queue_bytes} leaky=downstream !
            videoconvert !
            video/x-raw,format=I420,framerate={DEFAULT_PIPELINE_FPS}/1 !
            x264enc tune=zerolatency bitrate={DEFAULT_BITRATE} speed-preset=superfast key-int-max=30 !
            rtspclientsink location={rtsp_url}
        """

        pipeline = Gst.parse_launch(pipeline_str)
        appsrc = pipeline.get_by_name("src")

        # Set explicit caps for incoming frames (RGB) so videoconvert knows what it receives
        try:
            caps = Gst.Caps.from_string(f"video/x-raw,format=RGB,width={width},height={height},framerate={DEFAULT_PIPELINE_FPS}/1")
            appsrc.set_property("caps", caps)
        except Exception:
            # Not fatal; some gst builds may ignore
            pass

        # Safety properties on appsrc
        try:
            appsrc.set_property("is-live", True)
            appsrc.set_property("block", False)  # don't block entire process; we manage backpressure via queue
            appsrc.set_property("format", Gst.Format.TIME)
            appsrc.set_property("do-timestamp", True)
            # attempt to set a max-bytes to limit internal buffering in appsrc
            try:
                appsrc.set_property("max-bytes", int(approx_frame_bytes * (QUEUE_FRAMES + 1)))
            except Exception:
                pass
        except Exception:
            pass

        # Attach a bus watch to capture errors/warnings
        bus = pipeline.get_bus()
        bus.add_signal_watch()

        def on_message(bus, message):
            try:
                t = message.type
                if t == Gst.MessageType.ERROR:
                    err, dbg = message.parse_error()
                    print(f"[Model] GStreamer ERROR (cam{cam_id}): {err}, debug={dbg}")
                    # Mark pipeline unhealthy — caller will handle reset logic
                    # We avoid tearing down pipeline from inside bus handler to prevent reentrancy
                elif t == Gst.MessageType.WARNING:
                    warn, dbg = message.parse_warning()
                    print(f"[Model] GStreamer WARNING (cam{cam_id}): {warn}, debug={dbg}")
            except Exception:
                print(f"[Model] Bus handler exception (cam{cam_id}): {traceback.format_exc()}")

        try:
            bus.connect("message", on_message)
        except Exception:
            # Some platforms require different connection semantics; ignore if connect fails
            pass

        pipeline.set_state(Gst.State.PLAYING)
        print(f"[Model] RTSP publishing started → {rtsp_url} (queue_bytes={queue_bytes})")
        return appsrc, pipeline

    # ---------------- main frame read / push loop ----------------
    def read_all_cameras(self):
        """Read frames from SHM and push safely to appsrc/pipeline."""
        now = time.time()
        for cam_id, info in list(self.cameras.items()):
            # Read header
            hdr = read_header(info.mmap_obj)
            if hdr is None:
                # header not ready yet; attempt to re-attach header info if missing
                continue

            frame_seq, width, height, frame_no, ts = hdr

            # if width/height changed update info (rare)
            if info.width == 0:
                info.width = width
                info.height = height

            # read frame payload
            frame_size = width * height * 3
            frame_offset = HEADER_SIZE
            try:
                info.mmap_obj.seek(frame_offset)
                frame_bytes = info.mmap_obj.read(frame_size)
            except Exception:
                # reading failed; skip
                continue

            if len(frame_bytes) != frame_size:
                # incomplete frame, skip
                continue

            # Numpy view (no copy)
            try:
                frame = np.frombuffer(frame_bytes, dtype=np.uint8).reshape((height, width, 3))
            except Exception:
                # shape mismatch - skip
                continue

            # If pipeline is not created (server down at attach time), periodically try to create it
            if info.pipeline is None or info.appsrc is None:
                # throttle pipeline retries to avoid spin
                retry_interval = 2.0  # seconds between attempts
                if time.time() - info.last_pipeline_attempt > retry_interval:
                    info.last_pipeline_attempt = time.time()
                    if is_rtsp_alive():
                        try:
                            appsrc, pipeline = self._create_rtsp_pipeline(cam_id, width, height)
                            info.appsrc = appsrc
                            info.pipeline = pipeline
                            info.pipeline_ok = True
                            print(f"[Model] cam{cam_id}: pipeline created on retry")
                        except Exception as e:
                            info.pipeline_ok = False
                            print(f"[Model] cam{cam_id}: pipeline retry failed: {e}")
                # skip pushing this frame (server still down)
                continue

            # Ensure pipeline is playing (quick check)
            try:
                state = info.pipeline.get_state(0).state
                if state != self.Gst.State.PLAYING:
                    # pipeline isn't ready; attempt to restart it gracefully
                    try:
                        info.pipeline.set_state(self.Gst.State.NULL)
                    except Exception:
                        pass
                    try:
                        appsrc, pipeline = self._create_rtsp_pipeline(cam_id, width, height)
                        info.appsrc = appsrc
                        info.pipeline = pipeline
                        info.pipeline_ok = True
                    except Exception as e:
                        info.pipeline_ok = False
                        info.pipeline = None
                        info.appsrc = None
                        print(f"[Model] cam{cam_id}: pipeline recreate failed: {e}")
                    continue
            except Exception:
                # cannot query pipeline state; skip this frame
                continue

            # Throttle pushes to configured FPS
            tnow = time.time()
            min_interval = 1.0 / TARGET_FPS
            if (tnow - info.last_push_time) < min_interval:
                continue

            # Prepare buffer
            try:
                buf = self.Gst.Buffer.new_allocate(None, frame.nbytes, None)
                buf.fill(0, frame.tobytes())
                # set PTS from header timestamp (microseconds -> nanoseconds)
                try:
                    buf.pts = int(ts * 1000)
                except Exception:
                    pass
            except Exception as e:
                print(f"[Model] cam{cam_id}: failed to create Gst.Buffer: {e}")
                continue

            # Push buffer (appsrc is non-blocking; queue will drop old frames when overloaded)
            try:
                info.appsrc.emit("push-buffer", buf)
                info.last_push_time = time.time()
            except Exception as e:
                # push failure, mark pipeline unhealthy and tear down pipeline to avoid loops
                print(f"[Model] cam{cam_id}: push-buffer failed: {e}")
                try:
                    info.pipeline.set_state(self.Gst.State.NULL)
                except Exception:
                    pass
                info.pipeline = None
                info.appsrc = None
                info.pipeline_ok = False
                # continue to next camera
                continue

    # ---------------- lifecycle ----------------
    def run(self):
        print("[Model] Model Runner Started")
        try:
            while not self.stop_flag:
                with self.lock:
                    self.discover_cameras()
                    self.read_all_cameras()
                # small sleep to keep responsiveness but avoid busy spin
                time.sleep(0.005)
        except KeyboardInterrupt:
            pass
        finally:
            # graceful shutdown: tear down pipelines, close mmaps
            for cam_id in list(self.cameras.keys()):
                self._detach_camera(cam_id)
            print("[Model] Model Runner Stopped")


# -------------- SAFE PROCESS ENTRY FUNCTION -------------- #
def _model_process_entry():
    runner = SHMModelRunner()
    runner.run()


# -------------- API FOR FASTAPI -------------- #
_model_process: Process | None = None


def start_model_runner():
    """Start model runner in independent process."""
    global _model_process

    if _model_process and _model_process.is_alive():
        print("[Model] Already running")
        return

    print("[Model] Launching Model Process...")
    _model_process = Process(target=_model_process_entry)
    _model_process.start()


def stop_model_runner():
    """Stop the model process."""
    global _model_process
    if _model_process and _model_process.is_alive():
        print("[Model] Stopping model process...")
        _model_process.terminate()
        _model_process.join()
