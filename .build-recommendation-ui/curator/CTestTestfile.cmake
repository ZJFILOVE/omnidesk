# CMake generated Testfile for 
# Source directory: /home/zhu/Projects/omnidesk/plugins/omnidesk-plugin-ai-curator
# Build directory: /home/zhu/Projects/omnidesk/.build-recommendation-ui/curator
# 
# This file includes the relevant testing commands required for 
# testing this directory and lists subdirectories to be tested as well.
add_test("ai_curator_abi_smoke" "/home/zhu/Projects/omnidesk/.build-recommendation-ui/curator/test_abi" "/home/zhu/Projects/omnidesk/.build-recommendation-ui/curator/libplugin_ai_curator.so")
set_tests_properties("ai_curator_abi_smoke" PROPERTIES  _BACKTRACE_TRIPLES "/home/zhu/Projects/omnidesk/plugins/omnidesk-plugin-ai-curator/CMakeLists.txt;57;add_test;/home/zhu/Projects/omnidesk/plugins/omnidesk-plugin-ai-curator/CMakeLists.txt;0;")
add_test("ai_curator_suite" "/home/zhu/Projects/omnidesk/.build-recommendation-ui/curator/test_ai_curator")
set_tests_properties("ai_curator_suite" PROPERTIES  _BACKTRACE_TRIPLES "/home/zhu/Projects/omnidesk/plugins/omnidesk-plugin-ai-curator/CMakeLists.txt;71;add_test;/home/zhu/Projects/omnidesk/plugins/omnidesk-plugin-ai-curator/CMakeLists.txt;0;")
