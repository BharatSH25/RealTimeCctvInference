# model_runner.py
import time
import os
import mmap
import numpy as np
from multiprocessing import Process
from dataclasses import dataclass
import threading
import cv2
import zmq
import json
SHM_PREFIX = "/dev/shm/camera_shm_"
import struct

def read_header(mm):
    mm.seek(0)
    header_data = mm.read(32)
    # print("header len:", len(header_data), "raw:", header_data.hex())

    if len(header_data) < 32:
        return None  # SHM header not yet written (supervisor hasn't produced it)

    try:
        return struct.unpack("QIIQQ", header_data)
    except struct.error:
        return None

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

class SHMModelRunner:
    def __init__(self):
        self.cameras = {}
        self.stop_flag = False
        self.lock = threading.Lock()
        self.roi_map = {}     # <-- new
        self.stop_flag = False

        # setup ZMQ command receiver
        context = zmq.Context()
        self.cmd_socket = context.socket(zmq.PULL)
        self.cmd_socket.bind("tcp://*:7777")
        self.alert_ctx = zmq.Context()
        self.alert_socket = self.alert_ctx.socket(zmq.PUSH)
        self.alert_socket.connect("tcp://127.0.0.1:8880")

        # start command thread
        threading.Thread(target=self._cmd_listener, daemon=True).start()

        import gi
        gi.require_version('Gst', '1.0')
        from gi.repository import Gst
        Gst.init(None)

        self.Gst = Gst   # <-- IMPORTANT
    def _cmd_listener(self):
        print("[Model] Command listener started...")
        while not self.stop_flag:
            cmd = self.cmd_socket.recv_json()
            self._handle_command(cmd)

    def _handle_command(self, cmd):
        action = cmd.get("action")

        if action == "set_roi":
            cam_id = cmd["cam_id"]
            rois = cmd["rois"]
            self.roi_map[cam_id] = rois
            print(f"[Model] Updated ROI for cam {cam_id}")

        elif action == "clear_roi":
            cam_id = cmd["cam_id"]
            self.roi_map.pop(cam_id, None)
            print(f"[Model] Cleared ROI for cam {cam_id}")

        elif action == "remove_camera":
            cam_id = cmd["cam_id"]
            self.cameras.pop(cam_id, None)
            self.roi_map.pop(cam_id, None)
            print(f"[Model] Removed camera {cam_id} + ROI")


    def discover_cameras(self):
        existing = set()

        # 1) Detect newly added cameras
        for fname in os.listdir("/dev/shm"):
            if fname.startswith("camera_shm_"):
                cam_id = int(fname.split("_")[2])
                existing.add(cam_id)
                if cam_id not in self.cameras:
                    self._attach_to_camera(cam_id)

        # 2) Detect removed cameras
        removed = []
        for cam_id, info in self.cameras.items():
            if cam_id not in existing:
                removed.append(cam_id)

        for cam_id in removed:
            print(f"[Model] Camera {cam_id} removed - closing mmap")
            info = self.cameras.pop(cam_id)
            info.mmap_obj.close()

    def _create_rtsp_pipeline(self, cam_id, width, height):
        Gst = self.Gst

        rtsp_url = f"rtsp://127.0.0.1:8554/cam{cam_id}_processed"

        pipeline_str = f"""
            appsrc name=src is-live=true block=true format=time 
                max-buffers=1 leaky-type=upstream !
            video/x-raw,format=RGB,width={width},height={height},framerate=30/1 !
            videoconvert !
            video/x-raw,format=I420 !
            x264enc tune=zerolatency speed-preset=ultrafast bitrate=2000 key-int-max=30 bframes=0 sliced-threads=true !
            rtspclientsink location={rtsp_url}
        """

        pipeline = Gst.parse_launch(pipeline_str)
        appsrc = pipeline.get_by_name("src")

        appsrc.set_property("is-live", True)
        appsrc.set_property("format", Gst.Format.TIME)
        appsrc.set_property("do-timestamp", True)
        appsrc.set_property("max-buffers", 1)
        appsrc.set_property("leaky-type", 2)

        pipeline.set_state(Gst.State.PLAYING)
        print(f"[Model] RTSP publishing started → {rtsp_url}")
        return appsrc, pipeline

    def _attach_to_camera(self, cam_id):
        shm_path = f"/dev/shm/camera_shm_{cam_id}"
        if not os.path.exists(shm_path):
            return
        size = os.path.getsize(shm_path)
        with open(shm_path, "r+b") as f:
            mm = mmap.mmap(f.fileno(), size, access=mmap.ACCESS_READ)
        hdr = read_header(mm)
        if hdr is None:
            print(f"[Model] Camera {cam_id}: header not yet written, retrying next cycle")
            return
        frame_seq, width, height, frame_no, ts_us = hdr
        appsrc, pipeline = self._create_rtsp_pipeline(cam_id, width, height)

        info = CameraSHMInfo(cam_id, width, height, size, shm_path, mm, appsrc, pipeline)
        self.cameras[cam_id] = info

        print(f"[Model] Attached to Camera {cam_id}: {width}x{height}")

    def read_all_cameras(self):
        for cam_id, info in self.cameras.items():

            # ---- READ SHM HEADER ----
            hdr = read_header(info.mmap_obj)
            if hdr is None:
                print(f"[Model] Camera {cam_id}: header unreadable, skip frame")
                continue

            frame_seq, width, height, frame_no, ts = hdr
            frame_size = width * height * 3
            frame_offset = 32  # header size

            info.mmap_obj.seek(frame_offset)
            frame_bytes = info.mmap_obj.read(frame_size)

            if len(frame_bytes) != frame_size:
                print(f"[Model] Camera {cam_id}: incomplete frame, skipping")
                continue
            # frame = np.frombuffer(frame_bytes, dtype=np.uint8).reshape((height, width, 3))
            # SHM gives RGB → convert to BGR
            frame = np.frombuffer(frame_bytes, dtype=np.uint8).reshape((height, width, 3))
            frame_bgr = np.frombuffer(frame_bytes, dtype=np.uint8).reshape((height, width, 3))
            # if cam_id == 2:
            #     cv2.imshow("CAM 2 DEBUG", frame_bgr)
            #     cv2.waitKey(1)

            # push to GStreamer
            buf = self.Gst.Buffer.new_allocate(None, frame_bgr.nbytes, None)
            buf.fill(0, frame_bgr.tobytes())
            info.appsrc.emit("push-buffer", buf)
            # ---- Push to RTSP ----
            # buf = self.Gst.Buffer.new_allocate(None, frame.nbytes, None)
            # buf.fill(0, frame.tobytes())
            # info.appsrc.emit("push-buffer", buf)
            rois = self.roi_map.get(cam_id, [])
            if frame_no % 50 == 0:
                alert = {
                    "cam_id": cam_id,
                    "status": "ROI_TRIGGER",
                    "frame_no": frame_no,
                    "ts": ts,
                    "details": f"ROI event detected for camera {cam_id}"
                }
                self.alert_socket.send_json(alert)
            # print(f"[Model] Camera {cam_id}: frame {frame_no}, {width}x{height}, ROI{rois}")


    def run(self):
        print("[Model] Model Runner Started")
        while not self.stop_flag:
            with self.lock:
                self.discover_cameras()
                self.read_all_cameras()
            time.sleep(0.02)
        print("[Model] Model Runner Stopped")


# -------------- SAFE PROCESS ENTRY FUNCTION -------------- #

def _model_process_entry():
    """This is the real entry used by multiprocessing."""
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



