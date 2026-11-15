# CMake generated Testfile for 
# Source directory: /home/zhu/Projects/omnidesk
# Build directory: /home/zhu/Projects/omnidesk/.build-recommendation-ui
# 
# This file includes the relevant testing commands required for 
# testing this directory and lists subdirectories to be tested as well.
add_test("test_sdk_bus" "/home/zhu/Projects/omnidesk/.build-recommendation-ui/test_sdk_bus")
set_tests_properties("test_sdk_bus" PROPERTIES  TIMEOUT "90" _BACKTRACE_TRIPLES "/home/zhu/Projects/omnidesk/CMakeLists.txt;304;add_test;/home/zhu/Projects/omnidesk/CMakeLists.txt;0;")
add_test("test_c_abi_header" "/home/zhu/Projects/omnidesk/.build-recommendation-ui/test_c_abi_header")
set_tests_properties("test_c_abi_header" PROPERTIES  _BACKTRACE_TRIPLES "/home/zhu/Projects/omnidesk/CMakeLists.txt;308;add_test;/home/zhu/Projects/omnidesk/CMakeLists.txt;0;")
add_test("test_pm_core" "/home/zhu/Projects/omnidesk/.build-recommendation-ui/test_pm_core")
set_tests_properties("test_pm_core" PROPERTIES  TIMEOUT "240" _BACKTRACE_TRIPLES "/home/zhu/Projects/omnidesk/CMakeLists.txt;321;add_test;/home/zhu/Projects/omnidesk/CMakeLists.txt;0;")
add_test("test_control_mcp" "/home/zhu/Projects/omnidesk/.build-recommendation-ui/test_control_mcp")
set_tests_properties("test_control_mcp" PROPERTIES  TIMEOUT "180" _BACKTRACE_TRIPLES "/home/zhu/Projects/omnidesk/CMakeLists.txt;334;add_test;/home/zhu/Projects/omnidesk/CMakeLists.txt;0;")
subdirs("_deps/borealis")
subdirs("_deps/lunasvg")
