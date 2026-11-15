# CMake generated Testfile for 
# Source directory: /home/zhu/Projects/omnidesk/plugins/omnidesk-plugin-magpie-agent
# Build directory: /home/zhu/Projects/omnidesk/.build-recommendation-ui/magpie
# 
# This file includes the relevant testing commands required for 
# testing this directory and lists subdirectories to be tested as well.
add_test(test_abi "/home/zhu/Projects/omnidesk/.build-recommendation-ui/magpie/test_abi" "/home/zhu/Projects/omnidesk/.build-recommendation-ui/magpie/libplugin_magpie_agent.so")
set_tests_properties(test_abi PROPERTIES  _BACKTRACE_TRIPLES "/home/zhu/Projects/omnidesk/plugins/omnidesk-plugin-magpie-agent/CMakeLists.txt;53;add_test;/home/zhu/Projects/omnidesk/plugins/omnidesk-plugin-magpie-agent/CMakeLists.txt;0;")
add_test(test_magpie_plugin "/home/zhu/Projects/omnidesk/.build-recommendation-ui/magpie/test_magpie_plugin")
set_tests_properties(test_magpie_plugin PROPERTIES  _BACKTRACE_TRIPLES "/home/zhu/Projects/omnidesk/plugins/omnidesk-plugin-magpie-agent/CMakeLists.txt;64;add_test;/home/zhu/Projects/omnidesk/plugins/omnidesk-plugin-magpie-agent/CMakeLists.txt;0;")
add_test(test_magpie_agent "/home/zhu/Projects/omnidesk/.build-recommendation-ui/magpie/test_magpie_agent")
set_tests_properties(test_magpie_agent PROPERTIES  _BACKTRACE_TRIPLES "/home/zhu/Projects/omnidesk/plugins/omnidesk-plugin-magpie-agent/CMakeLists.txt;75;add_test;/home/zhu/Projects/omnidesk/plugins/omnidesk-plugin-magpie-agent/CMakeLists.txt;0;")
