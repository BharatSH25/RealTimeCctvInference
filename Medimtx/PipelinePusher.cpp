#include "PipelinePusher.hpp"
#include <iostream>
#include <chrono>

PipelinePusher::PipelinePusher(int cam_id, const std::string &rtsp_url)
    : cam_id_(cam_id), rtsp_url_(rtsp_url), running_(true),
      pipeline_(nullptr), appsrc_(nullptr)
{
    gst_init(nullptr, nullptr);

    if (!initPipeline()) {
        std::cerr << "[ERROR] Failed to initialize GStreamer pipeline for cam "
                  << cam_id_ << std::endl;
        running_ = false;
        return;
    }

    push_thread_ = std::thread(&PipelinePusher::pushLoop, this);
}

PipelinePusher::~PipelinePusher() {
    running_ = false;
    if (push_thread_.joinable())
        push_thread_.join();

    destroyPipeline();
}

bool PipelinePusher::initPipeline() {
    // >>>>> YOUR PIPELINE EXACT FORMAT <<<<<
    std::string pipeline_desc =
        "appsrc name=mysrc is-live=true block=true format=time "
        "max-buffers=1 leaky-type=upstream ! "
        "video/x-raw,format=RGB,width=1920,height=1080,framerate=30/1 ! "
        "videoconvert ! video/x-raw,format=I420 ! "
        "x264enc tune=zerolatency speed-preset=ultrafast bitrate=2000 "
        "key-int-max=30 bframes=0 sliced-threads=true ! "
        "rtspclientsink location=" + rtsp_url_;

    GError *err = nullptr;
    pipeline_ = gst_parse_launch(pipeline_desc.c_str(), &err);

    if (err) {
        std::cerr << "[GST ERROR] " << err->message << std::endl;
        g_error_free(err);
        return false;
    }

    appsrc_ = gst_bin_get_by_name(GST_BIN(pipeline_), "mysrc");
    if (!appsrc_) {
        std::cerr << "[GST ERROR] Failed to get appsrc\n";
        return false;
    }

    GstCaps *caps = gst_caps_new_simple(
        "video/x-raw",
        "format", G_TYPE_STRING, "RGB",
        "width", G_TYPE_INT, 1920,
        "height", G_TYPE_INT, 1080,
        "framerate", GST_TYPE_FRACTION, 30, 1,
        NULL
    );

    g_object_set(G_OBJECT(appsrc_),
                 "caps", caps,
                 "format", GST_FORMAT_TIME,
                 NULL);

    gst_caps_unref(caps);

    gst_element_set_state(pipeline_, GST_STATE_PLAYING);

    std::cout << "[Pusher] Pipeline created for cam " << cam_id_ << std::endl;
    return true;
}

void PipelinePusher::destroyPipeline() {
    if (pipeline_) {
        gst_element_set_state(pipeline_, GST_STATE_NULL);
        gst_object_unref(pipeline_);
        pipeline_ = nullptr;
    }
}

void PipelinePusher::pushFrame(const std::vector<uint8_t> &frame) {
    std::lock_guard<std::mutex> lock(frame_mtx_);
    latest_frame_ = frame;   // Copy frame (safe)
}

void PipelinePusher::pushLoop() {
    while (running_) {
        std::this_thread::sleep_for(std::chrono::milliseconds(5));

        std::vector<uint8_t> frame_copy;

        {
            std::lock_guard<std::mutex> lock(frame_mtx_);
            frame_copy = latest_frame_;
        }

        if (frame_copy.empty())
            continue;

        GstBuffer *buffer =
            gst_buffer_new_allocate(nullptr, frame_copy.size(), nullptr);
        gst_buffer_fill(buffer, 0, frame_copy.data(), frame_copy.size());

        GstFlowReturn ret;
        g_signal_emit_by_name(appsrc_, "push-buffer", buffer, &ret);

        gst_buffer_unref(buffer);

        if (ret != GST_FLOW_OK) {
            std::cerr << "[GST ERROR] push-buffer failed for cam "
                      << cam_id_ << "\n";
        }
    }
}
