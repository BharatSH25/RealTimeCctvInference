#include "ModelSubscriber.hpp"
#include "ZmqContextSingleton.hpp"
#include "PipelinePusher.hpp"
#include <iostream>
#include <vector>

ModelSubscriber::ModelSubscriber(
    int model_id, const std::string &address,
    std::map<int, std::unique_ptr<PipelinePusher>> &pipelines)
    : model_id_(model_id),
      address_(address),
      sub_(ZmqContext::get(), ZMQ_SUB),
      running_(true),
      pipeline_map_(pipelines)
{
    sub_.connect(address.c_str());
    sub_.set(zmq::sockopt::subscribe, "");

    recv_thread_ = std::thread(&ModelSubscriber::recvLoop, this);
}

ModelSubscriber::~ModelSubscriber() {
    running_ = false;
    if (recv_thread_.joinable()) recv_thread_.join();
}

void ModelSubscriber::recvLoop() {
    while (running_) {
        zmq::message_t msg;

        if(!sub_.recv(msg, zmq::recv_flags::none))
            continue;

        // format: [cam_id][raw_frame]
        struct FrameHeader {
            int cam_id;
            int frame_size;
        };

        auto *hdr = reinterpret_cast<const FrameHeader*>(msg.data());
        int cam = hdr->cam_id;
        int sz = hdr->frame_size;

        const uint8_t *frame_data = reinterpret_cast<const uint8_t*>(msg.data()) + sizeof(FrameHeader);
        std::vector<uint8_t> frame(frame_data, frame_data + sz);

        if (pipeline_map_.count(cam)) {
            pipeline_map_[cam]->pushFrame(frame);
        }
    }
}
