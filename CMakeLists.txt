cmake_minimum_required(VERSION 3.20)
project(omnidesk VERSION 0.1.0 LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_C_STANDARD 11)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

option(OMNIDESK_BUILD_TESTS "Build OmniDesk SDK & PM tests" ON)

# Options expected by upstream borealis / wiliwili dependency stack
set(PLATFORM_DESKTOP ON CACHE BOOL "" FORCE)
set(USE_SDL2 ON CACHE BOOL "" FORCE)
set(USE_SYSTEM_SDL2 ON CACHE BOOL "" FORCE)
set(BRLS_USE_SYSTEM_SDL2 ON CACHE BOOL "" FORCE)
set(USE_SYSTEM_CURL ON CACHE BOOL "" FORCE)
set(USE_SYSTEM_FMT OFF CACHE BOOL "" FORCE)
set(USE_SYSTEM_TINYXML2 OFF CACHE BOOL "" FORCE)
set(DISABLE_OPENCC ON CACHE BOOL "" FORCE)
set(ANALYTICS OFF CACHE BOOL "" FORCE)
set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)

find_package(Threads REQUIRED)
find_package(OpenGL REQUIRED)
find_package(OpenSSL REQUIRED)
find_package(CURL REQUIRED)
find_package(nlohmann_json REQUIRED)
find_package(PkgConfig REQUIRED)
pkg_check_modules(SDL2 REQUIRED IMPORTED_TARGET sdl2)
pkg_check_modules(MPV REQUIRED IMPORTED_TARGET mpv)
pkg_check_modules(LIBWEBP REQUIRED IMPORTED_TARGET libwebp)

# Upstream Borealis + lunasvg (compiled -fPIC, kept untouched under sources/)
set(BRLS_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/sources/native-wiliwili/upstream/library/borealis")
set(LUNASVG_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/sources/native-wiliwili/upstream/library/lunasvg")

set(BRLS_UNITY_BUILD OFF CACHE BOOL "" FORCE)
include("${BRLS_ROOT}/library/cmake/commonOption.cmake")

set(LUNASVG_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
add_subdirectory("${BRLS_ROOT}/library" "${CMAKE_BINARY_DIR}/_deps/borealis" EXCLUDE_FROM_ALL)
add_subdirectory("${LUNASVG_ROOT}" "${CMAKE_BINARY_DIR}/_deps/lunasvg" EXCLUDE_FROM_ALL)

# Ensure Borealis looks in ./resources/ at runtime and preserves RTTI/symbols cleanly
set(OMNIDESK_BRLS_DEFS
    YG_ENABLE_EVENTS
    __SDL2__
    BOREALIS_USE_OPENGL
    FONS_HASH_LUT_SIZE=4096
    "BRLS_RESOURCES=\"./resources/\""
)

# ------------------------------------------------------------------------------
# 1. OmniDesk Shared SDK (libomnidesk_sdk.so)
#    Exports SDK registries, theme tokens, and Borealis/Yoga singletons via one .so
#    so decoupled plugins share RTTI & UI state seamlessly.
# ------------------------------------------------------------------------------
add_library(omnidesk_sdk SHARED
    sdk/src/async.cpp
    sdk/src/capabilities.cpp
    sdk/src/config_store.cpp
    sdk/src/domain_registry.cpp
    sdk/src/entity_bus.cpp
    sdk/src/image_cache.cpp
    sdk/src/job_center.cpp
    sdk/src/mpv_core.cpp
    sdk/src/plugin_context.cpp
    sdk/src/ui_theme.cpp
    sdk/src/worker_supervisor.cpp
)
add_library(OmniDesk::SDK ALIAS omnidesk_sdk)

set(OMNIDESK_SDK_INCLUDE_DIRS
    "${CMAKE_CURRENT_SOURCE_DIR}/sdk/include"
    "${BRLS_ROOT}/library/include"
    "${BRLS_ROOT}/library/include/borealis/extern"
    "${BRLS_ROOT}/library/include/borealis/extern/nanovg"
    "${BRLS_ROOT}/library/include/borealis/extern/tinyxml2"
    "${BRLS_ROOT}/library/include/borealis/extern/glad"
    "${BRLS_ROOT}/library/include/borealis/extern/libretro-common"
    "${BRLS_ROOT}/library/lib/extern/fmt/include"
    "${BRLS_ROOT}/library/lib/extern/yoga"
    "${BRLS_ROOT}/library/lib/extern/tweeny/include"
)

foreach(_inc IN LISTS OMNIDESK_SDK_INCLUDE_DIRS)
    target_include_directories(omnidesk_sdk PUBLIC $<BUILD_INTERFACE:${_inc}>)
endforeach()
target_include_directories(omnidesk_sdk PUBLIC $<INSTALL_INTERFACE:include>)

target_compile_definitions(omnidesk_sdk
    PUBLIC ${OMNIDESK_BRLS_DEFS}
    PRIVATE OMNIDESK_BUILDING_SDK=1
)

set_target_properties(omnidesk_sdk PROPERTIES
    VERSION ${PROJECT_VERSION}
    SOVERSION 1
    CXX_VISIBILITY_PRESET default
    C_VISIBILITY_PRESET default
    VISIBILITY_INLINES_HIDDEN OFF
    BUILD_RPATH "$ORIGIN"
    INSTALL_RPATH "$ORIGIN"
)

target_link_libraries(omnidesk_sdk
    PRIVATE
        $<LINK_LIBRARY:WHOLE_ARCHIVE,borealis>
        $<LINK_LIBRARY:WHOLE_ARCHIVE,yogacore>
        $<$<TARGET_EXISTS:fmt>:$<LINK_LIBRARY:WHOLE_ARCHIVE,fmt>>
        $<$<TARGET_EXISTS:tinyxml2>:$<LINK_LIBRARY:WHOLE_ARCHIVE,tinyxml2>>
        PkgConfig::MPV
        PkgConfig::LIBWEBP
        CURL::libcurl
        OpenSSL::Crypto
    PUBLIC
        nlohmann_json::nlohmann_json
        PkgConfig::SDL2
        OpenGL::GL
        Threads::Threads
        ${CMAKE_DL_LIBS}
)

# Generate build-tree OmniDeskSDKConfig.cmake so omnidesk-pm can compile out-of-tree plugins immediately
include(CMakePackageConfigHelpers)
set(OMNIDESK_SDK_CONFIG_LIB "$<TARGET_FILE:omnidesk_sdk>")
set(OMNIDESK_SDK_CONFIG_INCLUDES "${OMNIDESK_SDK_INCLUDE_DIRS};${SDL2_INCLUDE_DIRS}")
set(OMNIDESK_BRLS_RESOURCES "./resources/")
set(OMNIDESK_VERSION "${PROJECT_VERSION}")

file(MAKE_DIRECTORY "${CMAKE_BINARY_DIR}/sdk-cmake")
configure_package_config_file(
    "${CMAKE_CURRENT_SOURCE_DIR}/sdk/cmake/OmniDeskSDKConfig.cmake.in"
    "${CMAKE_BINARY_DIR}/sdk-cmake/OmniDeskSDKConfig.cmake.raw"
    INSTALL_DESTINATION "${CMAKE_BINARY_DIR}/sdk-cmake"
    NO_SET_AND_CHECK_MACRO
    NO_CHECK_REQUIRED_COMPONENTS_MACRO
)
file(GENERATE
    OUTPUT "${CMAKE_BINARY_DIR}/sdk-cmake/OmniDeskSDKConfig.cmake"
    INPUT "${CMAKE_BINARY_DIR}/sdk-cmake/OmniDeskSDKConfig.cmake.raw"
)
write_basic_package_version_file(
    "${CMAKE_BINARY_DIR}/sdk-cmake/OmniDeskSDKConfigVersion.cmake"
    VERSION ${PROJECT_VERSION}
    COMPATIBILITY SameMajorVersion
)

# ------------------------------------------------------------------------------
# 2. OmniDesk Plugin Marketplace Engine (libomnidesk_pm + omnidesk-pm CLI)
# ------------------------------------------------------------------------------
add_library(omnidesk_pm STATIC
    pm/src/catalog.cpp
    pm/src/installer.cpp
    pm/src/manifest.cpp
    pm/src/manager.cpp
    pm/src/process.cpp
    pm/src/scaffold.cpp
)
target_include_directories(omnidesk_pm PUBLIC
    "${CMAKE_CURRENT_SOURCE_DIR}/pm/include"
)
target_link_libraries(omnidesk_pm
    PUBLIC
        omnidesk_sdk
        OpenSSL::Crypto
        CURL::libcurl
        ${CMAKE_DL_LIBS}
)

add_executable(omnidesk-pm
    pm/src/cli_main.cpp
)
target_link_libraries(omnidesk-pm PRIVATE omnidesk_pm omnidesk_sdk)
target_compile_definitions(omnidesk-pm PRIVATE
    OMNIDESK_DEFAULT_SDK_CMAKE_DIR="${CMAKE_BINARY_DIR}/sdk-cmake"
)
set_target_properties(omnidesk-pm PROPERTIES
    BUILD_RPATH "$ORIGIN"
    INSTALL_RPATH "$ORIGIN:$ORIGIN/../lib"
    ENABLE_EXPORTS ON
)

# ------------------------------------------------------------------------------
# 3. OmniDesk Host Desktop Workbench (omnidesk GUI)
# ------------------------------------------------------------------------------
add_executable(omnidesk
    host/src/book_workbench.cpp
    host/src/companion_dock.cpp
    host/src/desktop_ui.cpp
    host/src/host_settings_view.cpp
    host/src/kernel.cpp
    host/src/main.cpp
    host/src/media_workbench.cpp
    host/src/plugin_store_view.cpp
    host/src/shell_activity.cpp
    host/src/svg_view.cpp
    host/src/text_workbench.cpp
)
target_include_directories(omnidesk PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/host/include"
    "${LUNASVG_ROOT}/include"
)
target_link_libraries(omnidesk PRIVATE
    omnidesk_pm
    omnidesk_sdk
    lunasvg
)
target_compile_definitions(omnidesk PRIVATE
    OMNIDESK_DEFAULT_SDK_CMAKE_DIR="${CMAKE_BINARY_DIR}/sdk-cmake"
    "BRLS_RESOURCES=\"./resources/\""
    LUNASVG_STATIC_DEFINE
)
set_target_properties(omnidesk PROPERTIES
    BUILD_RPATH "$ORIGIN"
    INSTALL_RPATH "$ORIGIN:$ORIGIN/../lib"
    ENABLE_EXPORTS ON
)

# ------------------------------------------------------------------------------
# 4. Stage Runtime Resources (Borealis core + CJK fonts + OmniDesk assets)
# ------------------------------------------------------------------------------
set(WILIWILI_RES_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/sources/native-wiliwili/upstream/resources")
add_custom_target(omnidesk_resources ALL
    COMMAND ${CMAKE_COMMAND} -E make_directory "${CMAKE_BINARY_DIR}/resources"
    COMMAND ${CMAKE_COMMAND} -E copy_directory "${BRLS_ROOT}/resources/font" "${CMAKE_BINARY_DIR}/resources/font"
    COMMAND ${CMAKE_COMMAND} -E copy_directory "${BRLS_ROOT}/resources/i18n" "${CMAKE_BINARY_DIR}/resources/i18n"
    COMMAND ${CMAKE_COMMAND} -E copy_directory "${BRLS_ROOT}/resources/material" "${CMAKE_BINARY_DIR}/resources/material"
    COMMAND ${CMAKE_COMMAND} -E copy_directory "${BRLS_ROOT}/resources/xml" "${CMAKE_BINARY_DIR}/resources/xml"
    COMMAND ${CMAKE_COMMAND} -E copy_directory "${WILIWILI_RES_ROOT}/font" "${CMAKE_BINARY_DIR}/resources/font"
    COMMAND ${CMAKE_COMMAND} -E copy_directory "${WILIWILI_RES_ROOT}/i18n" "${CMAKE_BINARY_DIR}/resources/i18n"
    COMMAND ${CMAKE_COMMAND} -E copy_directory "${WILIWILI_RES_ROOT}/material" "${CMAKE_BINARY_DIR}/resources/material"
    COMMAND ${CMAKE_COMMAND} -E copy_directory "${CMAKE_CURRENT_SOURCE_DIR}/host/resources" "${CMAKE_BINARY_DIR}/resources"
    COMMENT "Staging Borealis + OmniDesk runtime resources"
)
add_dependencies(omnidesk omnidesk_resources)
add_dependencies(omnidesk-pm omnidesk_resources)

# ------------------------------------------------------------------------------
# 5. Automated Unit & Integration Tests
# ------------------------------------------------------------------------------
if(OMNIDESK_BUILD_TESTS AND EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/tests/test_sdk_bus.cpp")
    enable_testing()
    add_executable(test_sdk_bus tests/test_sdk_bus.cpp)
    target_link_libraries(test_sdk_bus PRIVATE omnidesk_sdk)
    add_test(NAME test_sdk_bus COMMAND test_sdk_bus)

    add_executable(test_pm_core tests/test_pm_core.cpp)
    target_link_libraries(test_pm_core PRIVATE omnidesk_pm omnidesk_sdk)
    target_compile_definitions(test_pm_core PRIVATE
        OMNIDESK_TEST_SDK_CMAKE_DIR="${CMAKE_BINARY_DIR}/sdk-cmake"
        OMNIDESK_TEST_PM_EXE="$<TARGET_FILE:omnidesk-pm>"
        OMNIDESK_TEST_CATALOG="${CMAKE_CURRENT_SOURCE_DIR}/host/resources/marketplace/catalog.json"
        OMNIDESK_TEST_SAMPLE_PLUGIN="${CMAKE_CURRENT_SOURCE_DIR}/tests/test_plugin_sample"
    )
    add_dependencies(test_pm_core omnidesk-pm)
    add_test(NAME test_pm_core COMMAND test_pm_core)
endif()
