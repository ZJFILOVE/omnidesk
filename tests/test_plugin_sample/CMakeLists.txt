cmake_minimum_required(VERSION 3.20)
project(plugin_sample_echo LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

find_package(OmniDeskSDK REQUIRED)

add_library(plugin_sample_echo SHARED
    src/plugin_main.cpp
)
target_link_libraries(plugin_sample_echo PRIVATE OmniDesk::SDK)
set_target_properties(plugin_sample_echo PROPERTIES
    OUTPUT_NAME "plugin_sample_echo"
    PREFIX "lib"
)

enable_testing()
add_executable(test_abi tests/test_abi.cpp)
target_link_libraries(test_abi PRIVATE OmniDesk::SDK dl)
add_test(NAME abi_smoke COMMAND test_abi $<TARGET_FILE:plugin_sample_echo> "plugin-sample-echo")
