#pragma once

#if defined(_WIN32)
#  if defined(OMNIDESK_SDK_BUILD)
#    define OMNIDESK_API __declspec(dllexport)
#  else
#    define OMNIDESK_API __declspec(dllimport)
#  endif
#  define OMNIDESK_PLUGIN_EXPORT __declspec(dllexport)
#else
#  define OMNIDESK_API __attribute__((visibility("default")))
#  define OMNIDESK_PLUGIN_EXPORT __attribute__((visibility("default")))
#endif
