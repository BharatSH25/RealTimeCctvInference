#pragma once
#include <string>
#include <thread>
#include <atomic>
#include <mutex>
#include <vector>

#include <gst/gst.h>
#include <gst/app/gstappsrc.h>

class PipelinePusher {
public:
    PipelinePusher(int cam_id, const std::string &rtsp_url);
    ~PipelinePusher();

    void pushFrame(const std::vector<uint8_t> &frame);

private:
    int cam_id_;
    std::string rtsp_url_;

    std::thread push_thread_;
    std::atomic<bool> running_;

    std::mutex frame_mtx_;
    std::vector<uint8_t> latest_frame_;

    GstElement *pipeline_;
    GstElement *appsrc_;

    void pushLoop();
    bool initPipeline();
    void destroyPipeline();
};
