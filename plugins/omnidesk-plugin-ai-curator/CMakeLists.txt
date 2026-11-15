cmake_minimum_required(VERSION 3.20)
project(plugin_ai_curator VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(CMAKE_CXX_VISIBILITY_PRESET hidden)
set(CMAKE_VISIBILITY_INLINES_HIDDEN ON)

find_package(OmniDeskSDK REQUIRED)
find_package(OpenSSL REQUIRED)
find_package(Threads REQUIRED)

add_library(ai_curator_core STATIC
    src/utils.cpp
    src/git_mirror.cpp
    src/archive_rehydrator.cpp
    src/daily_scheduler.cpp
    src/curation_engine.cpp
    src/domain_sources.cpp
    src/plugin.cpp
)
target_include_directories(ai_curator_core
    PUBLIC
        ${CMAKE_CURRENT_SOURCE_DIR}/include
)
target_link_libraries(ai_curator_core
    PUBLIC
        OmniDesk::SDK
        OpenSSL::Crypto
        Threads::Threads
)
target_compile_options(ai_curator_core PRIVATE -Wall -Wextra -Wpedantic)

add_library(plugin-ai-curator SHARED
    src/plugin_main.cpp
)
target_include_directories(plugin-ai-curator
    PRIVATE
        ${CMAKE_CURRENT_SOURCE_DIR}/include
)
target_link_libraries(plugin-ai-curator
    PRIVATE
        ai_curator_core
)
target_compile_options(plugin-ai-curator PRIVATE -Wall -Wextra -Wpedantic)
set_target_properties(plugin-ai-curator PROPERTIES
    OUTPUT_NAME "plugin_ai_curator"
    PREFIX "lib"
)

enable_testing()

add_executable(test_abi tests/test_abi.cpp)
target_link_libraries(test_abi PRIVATE OmniDesk::SDK ${CMAKE_DL_LIBS})
target_compile_options(test_abi PRIVATE -Wall -Wextra -Wpedantic)
add_test(NAME ai_curator_abi_smoke COMMAND test_abi $<TARGET_FILE:plugin-ai-curator>)

add_executable(test_ai_curator tests/test_ai_curator.cpp)
target_link_libraries(test_ai_curator
    PRIVATE
        ai_curator_core
        OmniDesk::SDK
        Threads::Threads
        ${CMAKE_DL_LIBS}
)
target_compile_options(test_ai_curator PRIVATE -Wall -Wextra -Wpedantic)
target_compile_definitions(test_ai_curator PRIVATE
    AI_CURATOR_PLUGIN_SO_PATH="$<TARGET_FILE:plugin-ai-curator>"
)
add_test(NAME ai_curator_suite COMMAND test_ai_curator)
