#include "MediaMTXPusher.h"
#include <chrono>
#include <nlohmann/json.hpp>

using json = nlohmann::json;

MediaMTXPusher::MediaMTXPusher()
    : context(1),
      cmd_socket(context, ZMQ_SUB),
      frame_socket(context, ZMQ_SUB) 
{
    gst_init(nullptr, nullptr);
}

MediaMTXPusher::~MediaMTXPusher() {
    // Clean pipelines
    for (auto& [cam_id, info] : pipelines) {
        gst_element_set_state(info.pipeline, GST_STATE_NULL);
        gst_object_unref(info.pipeline);
    }
}

void MediaMTXPusher::run() {
    // Connect sockets
    cmd_socket.connect("tcp://127.0.0.1:5556"); // FastAPI commands
    cmd_socket.setsockopt(ZMQ_SUBSCRIBE, "", 0);

    frame_socket.connect("tcp://127.0.0.1:5555"); // Model frames
    frame_socket.setsockopt(ZMQ_SUBSCRIBE, "", 0);

    // Start threads
    std::thread(&MediaMTXPusher::command_listener, this).detach();
    std::thread(&MediaMTXPusher::frame_listener, this).detach();
}

void MediaMTXPusher::command_listener() {
    while (true) {
        zmq::message_t msg;
        cmd_socket.recv(&msg);
        std::string str_msg(static_cast<char*>(msg.data()), msg.size());

        try {
            auto j = json::parse(str_msg);
            std::string action = j["action"];
            int cam_id = j["cam_id"];

            if (action == "ADD") add_camera(cam_id);
            else if (action == "DELETE") remove_camera(cam_id);
        } catch (...) {
            std::cerr << "[Error] Invalid command received: " << str_msg << std::endl;
        }
    }
}

void MediaMTXPusher::frame_listener() {
    while (true) {
        zmq::message_t msg;
        frame_socket.recv(&msg);

        try {
            auto j = json::parse(std::string(static_cast<char*>(msg.data()), msg.size()));
            // Expecting {"1":[...frame_bytes...], "2":[...]}
            for (auto& [key, val] : j.items()) {
                int cam_id = std::stoi(key);
                std::vector<uint8_t> frame = val.get<std::vector<uint8_t>>();

                if (pipelines.count(cam_id) && pipelines[cam_id].active) {
                    push_frame(cam_id, frame);
                }
            }
        } catch (...) {
            std::cerr << "[Error] Invalid frame message received" << std::endl;
        }
    }
}

void MediaMTXPusher::add_camera(int cam_id) {
    if (pipelines.count(cam_id)) return;

    PipelineInfo pinfo;
    pinfo.pipeline = create_pipeline(cam_id);
    pinfo.active = true;
    pipelines[cam_id] = pinfo;

    push_threads[cam_id] = std::thread([this, cam_id]() {
        while (pipelines[cam_id].active) {
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
    });
    push_threads[cam_id].detach();

    std::cout << "[MediaMTX] Camera added: " << cam_id << std::endl;
}

void MediaMTXPusher::remove_camera(int cam_id) {
    if (!pipelines.count(cam_id)) return;

    pipelines[cam_id].active = false;
    gst_element_set_state(pipelines[cam_id].pipeline, GST_STATE_NULL);
    gst_object_unref(pipelines[cam_id].pipeline);
    pipelines.erase(cam_id);

    std::cout << "[MediaMTX] Camera removed: " << cam_id << std::endl;
}

GstElement* MediaMTXPusher::create_pipeline(int cam_id) {
    std::string rtsp_url = "rtsp://127.0.0.1:8554/cam" + std::to_string(cam_id);
    std::string pipeline_str = 
        "appsrc name=src is-live=true block=true format=time "
        "max-buffers=1 leaky-type=upstream ! "
        "video/x-raw,format=RGB,width=1920,height=1080,framerate=30/1 ! "
        "videoconvert ! video/x-raw,format=I420 ! "
        "x264enc tune=zerolatency speed-preset=ultrafast bitrate=2000 key-int-max=30 bframes=0 sliced-threads=true ! "
        "rtspclientsink location=" + rtsp_url;

    GstElement* pipeline = gst_parse_launch(pipeline_str.c_str(), nullptr);
    GstElement* appsrc = gst_bin_get_by_name(GST_BIN(pipeline), "src");

    gst_element_set_state(pipeline, GST_STATE_PLAYING);
    return pipeline;
}

void MediaMTXPusher::push_frame(int cam_id, const std::vector<uint8_t>& frame) {
    GstBuffer* buf = gst_buffer_new_allocate(nullptr, frame.size(), nullptr);
    gst_buffer_fill(buf, 0, frame.data(), frame.size());
    GstElement* appsrc = gst_bin_get_by_name(GST_BIN(pipelines[cam_id].pipeline), "src");
    g_signal_emit_by_name(appsrc, "push-buffer", buf);
    gst_buffer_unref(buf);
}
