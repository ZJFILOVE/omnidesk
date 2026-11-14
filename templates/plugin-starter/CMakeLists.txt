cmake_minimum_required(VERSION 3.20)
project(plugin_starter LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

find_package(OmniDeskSDK REQUIRED)

add_library(plugin_starter SHARED
    src/plugin_main.cpp
)
target_link_libraries(plugin_starter PRIVATE OmniDesk::SDK)
set_target_properties(plugin_starter PROPERTIES
    OUTPUT_NAME "plugin_starter"
    PREFIX "lib"
)

enable_testing()
add_executable(test_abi tests/test_abi.cpp)
target_link_libraries(test_abi PRIVATE OmniDesk::SDK dl)
add_test(NAME abi_smoke COMMAND test_abi $<TARGET_FILE:plugin_starter> "plugin-starter")
