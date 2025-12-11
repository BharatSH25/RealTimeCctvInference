import asyncio
import json
import sqlite3
import zmq
import zmq.asyncio
from fastapi import FastAPI, HTTPException
from model_runner import start_model_runner, stop_model_runner
from model_commands import send_model_cmd
import zmq.asyncio
from fastapi import WebSocket

class WSManager:
    def __init__(self):
        self.clients = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.clients.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.clients:
            self.clients.remove(ws)

    async def broadcast(self, data):
        dead = []
        for ws in self.clients:
            try:
                await ws.send_json(data)
            except:
                dead.append(ws)

        for ws in dead:
            self.disconnect(ws)

ws_manager = WSManager()

app = FastAPI(title="Camera Supervisor API")

# ---------- ZeroMQ Setup ----------
ZMQ_ENDPOINT = "tcp://127.0.0.1:5555"  # C++ supervisor binds here
zmq_context = zmq.asyncio.Context()
zmq_socket = zmq_context.socket(zmq.REQ)
zmq_socket.connect(ZMQ_ENDPOINT)

# ---------- DB Helper ----------
def fetch_cameras_from_db():
    conn = sqlite3.connect("cameras.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, rtsp_url, width, height FROM cameras")
    rows = cursor.fetchall()
    conn.close()

    return [
        {"id": r[0], "rtsp_url": r[1], "width": r[2], "height": r[3]}
        for r in rows
    ]

@app.websocket("/ws/alerts")
async def alerts_socket(ws: WebSocket):
    await ws_manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except:
        ws_manager.disconnect(ws)
async def alert_listener():
    ctx = zmq.asyncio.Context()
    socket = ctx.socket(zmq.PULL)
    socket.bind("tcp://127.0.0.1:8880")
    print("🚨 FastAPI Alert Listener: READY")

    while True:
        alert = await socket.recv_json()
        # print("📥 Received alert:", alert)
        await ws_manager.broadcast(alert)

# ---------- Startup: load all cameras and start them ----------
@app.on_event("startup")
async def startup_event():
    print("🚀 FastAPI: Loading cameras from DB...")

    # cameras = fetch_cameras_from_db()

    # for cam in cameras:
        # print(f"▶️ Starting camera {cam['id']} ...")
    msg = {1:{
        "cmd": "START_CAMERA",
        "camera_id": 1,
        "rtsp": "rtsp://admin:abcd%401234@192.168.1.2:554",
        "desired_width": 1920,
        "desired_height": 1080
    },
    2:{
        "cmd": "START_CAMERA",
        "camera_id": 2,
        "rtsp": "rtsp://admin:abcd%401234@192.168.1.5:554",
        "desired_width": 1920,
        "desired_height": 1080
    },
    }
        # cameras = load_from_db()
    # rois = load_roi_from_db()
    
    # Dummy example
    rois = {
        1: [{"x":10,"y":10,"w":200,"h":200}],
        2: [{"x":50,"y":50,"w":100,"h":100}]
    }

    
    for v in msg.values():
        await send_command(v)
    start_model_runner()
    for cam_id, roi_list in rois.items():
        send_model_cmd({
            "action": "set_roi",
            "cam_id": cam_id,
            "rois": roi_list
        })
    asyncio.create_task(alert_listener()) 


# ---------- ZeroMQ Send Wrapper ----------
async def send_command(obj: dict):
    try:
        payload = json.dumps(obj)
        await zmq_socket.send_string(payload)
        reply = await zmq_socket.recv_string()  # wait for C++ response
        print("C++ Supervisor replied:", reply)
        return reply
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==================================================================
#                          API ENDPOINTS
# ==================================================================

# ▶️ Manual Start Camera
@app.post("/camera/start")
async def start_camera():
    # msg = {
    #     "cmd": "START_CAMERA",
    #     "camera_id": camera_id,
    #     "rtsp": rtsp,
    #     "desired_width": width,
    #     "desired_height": height
    # }
    msg = {
        "cmd": "START_CAMERA",
        "camera_id": 3,
        "rtsp": "rtsp://admin:abcd%401234@192.168.1.5:554",
        "desired_width": 1920,
        "desired_height": 1080
    }
    return {"status": await send_command(msg)}


# ⏹ Stop Camera
@app.post("/camera/stop")
async def stop_camera():
    msg = {"cmd": "STOP_CAMERA", "camera_id": 2}
    await send_model_cmd({
        "action": "remove_camera",
        "cam_id": 2
    })
    return {"status": await send_command(msg)}


# 🔄 Resize Camera Stream
@app.post("/camera/resize")
async def resize_camera(camera_id: int, width: int, height: int):
    msg = {
        "cmd": "RESIZE",
        "camera_id": camera_id,
        "new_width": width,
        "new_height": height
    }
    return {"status": await send_command(msg)}


# 🟢 Health Check
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/roi/{cam_id}")
async def add_roi(cam_id: int, body: dict):
    roi = body["roi"]
    
    await send_model_cmd({
        "action": "set_roi",
        "cam_id": cam_id,
        "rois": [roi]   # or append existing ones
    })

    return {"status": "ok"}


@app.put("/roi/{cam_id}")
async def update_roi(cam_id: int, body: dict):
    await send_model_cmd({
        "action": "set_roi",
        "cam_id": cam_id,
        "rois": body["rois"]
    })
    return {"status": "ok"}


@app.delete("/roi/{cam_id}")
async def delete_roi(cam_id: int):
    await send_model_cmd({
        "action": "clear_roi",
        "cam_id": cam_id
    })
    return {"status": "ok"}


