#pragma once
#include <string>
#include <thread>
#include <atomic>
#include <map>
#include <memory>
#include <zmq.hpp>

class PipelinePusher;

class ModelSubscriber {
public:
    ModelSubscriber(int model_id, const std::string &address,
                    std::map<int, std::unique_ptr<PipelinePusher>> &pipeline_map);

    ~ModelSubscriber();

private:
    int model_id_;
    std::string address_;
    zmq::socket_t sub_;
    std::thread recv_thread_;
    std::atomic<bool> running_;

    std::map<int, std::unique_ptr<PipelinePusher>> &pipeline_map_;

    void recvLoop();
};
