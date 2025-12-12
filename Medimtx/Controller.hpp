#pragma once
#include <map>
#include <string>
#include <memory>
#include <zmq.hpp>
#include "PipelinePusher.hpp"
#include "ModelSubscriber.hpp"
class PipelinePusher;
class ModelSubscriber;

class Controller {
public:
    Controller();
    void run();

private:
    zmq::socket_t control_sub_;

    std::map<int, std::unique_ptr<PipelinePusher>> pipelines_;
    std::map<int, std::unique_ptr<ModelSubscriber>> model_subs_;

    void processCommand(const std::string &cmd);
    void addCamera(int cam_id, const std::string &rtsp);
    void removeCamera(int cam_id);
    void addModel(int model_id, const std::string &zmq_addr);
    void removeModel(int model_id);
};
