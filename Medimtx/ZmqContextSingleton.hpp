#pragma once
#include <zmq.hpp>

class ZmqContext {
public:
    static zmq::context_t& get() {
        static zmq::context_t ctx(1);
        return ctx;
    }
};
