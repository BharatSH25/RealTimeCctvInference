# model_commands.py
import zmq
import json
import zmq.asyncio
import asyncio
from manager import ws_manager

CMD_SOCKET_ADDR = "tcp://127.0.0.1:7777"

context = zmq.Context()
cmd_socket = context.socket(zmq.PUSH)
cmd_socket.connect(CMD_SOCKET_ADDR)

def send_model_cmd(command: dict):
    cmd_socket.send_json(command)




ZMQ_ALERT_ENDPOINT = "tcp://127.0.0.1:8888"
ctx = zmq.asyncio.Context()

async def alert_listener():
    socket = ctx.socket(zmq.PULL)
    socket.bind(ZMQ_ALERT_ENDPOINT)

    print("🚨 FastAPI Alert Listener Started")

    while True:
        alert = await socket.recv_json()
        print("📥 Received alert:", alert)

        # Broadcast to all connected clients
        await ws_manager.broadcast(alert)

