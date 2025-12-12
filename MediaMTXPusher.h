#pragma once

#include <iostream>
#include <thread>
#include <map>
#include <vector>
#include <atomic>
#include <zmq.hpp>
#include <gst/gst.h>

struct PipelineInfo {
    GstElement* pipeline;
    GstElement* appsrc;
    std::atomic<bool> active;
};

class MediaMTXPusher {
public:
    MediaMTXPusher();
    ~MediaMTXPusher();

    void run(); // start the pusher

private:
    zmq::context_t context;
    zmq::socket_t cmd_socket;
    zmq::socket_t frame_socket;

    std::map<int, PipelineInfo> pipelines;      // cam_id -> pipeline info
    std::map<int, std::thread> push_threads;    // per-camera push thread

    // Threads
    void command_listener();
    void frame_listener();

    // Camera management
    void add_camera(int cam_id);
    void remove_camera(int cam_id);

    // Pipeline management
    GstElement* create_pipeline(int cam_id);

    // Frame pushing
    void push_frame(int cam_id, const std::vector<uint8_t>& frame);
};
