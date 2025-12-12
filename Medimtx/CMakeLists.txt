cmake_minimum_required(VERSION 3.10)

project(CameraManager LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(PkgConfig REQUIRED)
pkg_check_modules(ZMQ libzmq REQUIRED)
pkg_check_modules(GST gstreamer-1.0 REQUIRED)

include_directories(
    ${ZMQ_INCLUDE_DIRS}
    ${GST_INCLUDE_DIRS}
    .
)

add_executable(app
    main.cpp
    Controller.cpp
    ModelSubscriber.cpp
    PipelinePusher.cpp
)

target_link_libraries(app
    ${ZMQ_LIBRARIES}
    ${GST_LIBRARIES}
    pthread
)
