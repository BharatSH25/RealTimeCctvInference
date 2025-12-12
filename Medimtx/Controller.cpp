#include "Controller.hpp"
#include "ZmqContextSingleton.hpp"

#include <iostream>
#include <sstream>

Controller::Controller()
    : control_sub_(ZmqContext::get(), ZMQ_SUB)
{
    control_sub_.connect("tcp://127.0.0.1:9000");
    control_sub_.set(zmq::sockopt::subscribe, "");
}

void Controller::run() {
    while (true) {
        zmq::message_t msg;
        auto res = control_sub_.recv(msg, zmq::recv_flags::none);
        if (!res) {
            std::cerr << "Control channel recv failed\n";
            continue;
        }


        std::string cmd(static_cast<char*>(msg.data()), msg.size());
        processCommand(cmd);
    }
}

void Controller::processCommand(const std::string &cmd) {
    std::stringstream ss(cmd);
    std::string op;
    ss >> op;

    if (op == "ADD_CAMERA") {
        int id; std::string url;
        ss >> id >> url;
        addCamera(id, url);
    }
    else if (op == "REMOVE_CAMERA") {
        int id; ss >> id;
        removeCamera(id);
    }
    else if (op == "NEW_MODEL") {
        int id; std::string addr;
        ss >> id >> addr;
        addModel(id, addr);
    }
    else if (op == "DELETE_MODEL") {
        int id; ss >> id;
        removeModel(id);
    }
}

void Controller::addCamera(int cam_id, const std::string &rtsp) {
    if (!pipelines_.count(cam_id))
        pipelines_[cam_id] = std::make_unique<PipelinePusher>(cam_id, rtsp);
}

void Controller::removeCamera(int cam_id) {
    pipelines_.erase(cam_id);
}

void Controller::addModel(int model_id, const std::string &zmq_addr) {
    if (!model_subs_.count(model_id))
        model_subs_[model_id] = std::make_unique<ModelSubscriber>(model_id, zmq_addr, pipelines_);
}

void Controller::removeModel(int model_id) {
    model_subs_.erase(model_id);
}
