cmake_minimum_required(VERSION 3.20)
project(ai_gateway VERSION 0.7.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
option(GATEWAY_SANITIZERS "Enable AddressSanitizer and UndefinedBehaviorSanitizer" OFF)

# Boost.System is header-only; Boost.Redis's single implementation TU is ours.
set(Boost_NO_BOOST_CMAKE ON)
if(POLICY CMP0144)
    cmake_policy(SET CMP0144 NEW)
endif()
if(POLICY CMP0167)
    cmake_policy(SET CMP0167 OLD)
endif()
find_package(Boost 1.88 REQUIRED)
find_package(OpenSSL REQUIRED)
find_package(Threads REQUIRED)
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/scripts/rate_limit.lua" RATE_LIMIT_LUA)
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/scripts/rate_limit.lua")
configure_file(src/rate_limit_script.hpp.in generated/rate_limit_script.hpp @ONLY)
add_executable(ai-gateway src/main.cpp src/gateway.cpp src/policy.cpp src/redis_limiter.cpp src/admission.cpp src/metrics.cpp)
target_include_directories(ai-gateway PRIVATE "${CMAKE_CURRENT_BINARY_DIR}/generated")
target_link_libraries(ai-gateway PRIVATE Boost::headers OpenSSL::SSL OpenSSL::Crypto Threads::Threads)
if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
    target_compile_options(ai-gateway PRIVATE -Wall -Wextra -Wpedantic -Wconversion -Wshadow)
    if(GATEWAY_SANITIZERS)
        target_compile_options(ai-gateway PRIVATE -fsanitize=address,undefined -fno-omit-frame-pointer)
        target_link_options(ai-gateway PRIVATE -fsanitize=address,undefined)
    endif()
endif()

include(CTest)
if(BUILD_TESTING)
    find_package(Python3 REQUIRED COMPONENTS Interpreter)
    add_test(NAME proxy_integration
        COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_proxy.py
                --binary $<TARGET_FILE:ai-gateway>)
    set_tests_properties(proxy_integration PROPERTIES TIMEOUT 60)
    add_test(NAME streaming_integration
        COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_streaming.py
                --binary $<TARGET_FILE:ai-gateway>)
    set_tests_properties(streaming_integration PROPERTIES TIMEOUT 90)
    add_test(NAME rate_limit_integration
        COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_rate_limit.py
                --binary $<TARGET_FILE:ai-gateway>)
    set_tests_properties(rate_limit_integration PROPERTIES TIMEOUT 90)
    add_test(NAME admission_integration
        COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_admission.py
                --binary $<TARGET_FILE:ai-gateway>)
    set_tests_properties(admission_integration PROPERTIES TIMEOUT 90)
    add_test(NAME metrics_integration
        COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_metrics.py
                --binary $<TARGET_FILE:ai-gateway>)
    set_tests_properties(metrics_integration PROPERTIES TIMEOUT 90)
    add_test(NAME cancellation_integration
        COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_cancellation.py
                --binary $<TARGET_FILE:ai-gateway>)
    set_tests_properties(cancellation_integration PROPERTIES TIMEOUT 90)
    add_test(NAME governance_proxy_integration
        COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_governance_proxy.py
                --binary $<TARGET_FILE:ai-gateway>)
    set_tests_properties(governance_proxy_integration PROPERTIES TIMEOUT 90)
endif()
