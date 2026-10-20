#pragma once

#include <cstddef>
#include <string>
#include "redis_limiter.hpp"
#include "admission.hpp"

namespace gateway {

struct Config {
    RedisSettings redis;
    AdmissionSettings admission;
    std::string listen_address = "127.0.0.1";
    unsigned short listen_port = 8080;
    std::string upstream_host = "127.0.0.1";
    std::string upstream_port = "8000";
    std::string health_path = "/healthz";
    std::string api_key;
    int header_timeout_ms = 5000;
    int request_timeout_ms = 10000;
    int model_timeout_ms = 180000;
    int connect_timeout_ms = 3000;
    int readiness_timeout_ms = 1000;
    int stream_timeout_ms = 300000;
    int stream_first_byte_timeout_ms = 10000;
    int stream_idle_timeout_ms = 45000;
    int stream_write_timeout_ms = 15000;
    int upload_timeout_ms = 120000;
    std::size_t max_connections = 128;
    std::size_t request_body_limit = 1024 * 1024;
    std::size_t response_body_limit = 4 * 1024 * 1024;
    std::size_t stream_body_limit = 64 * 1024 * 1024;
    std::size_t upload_body_limit = 416 * 1024 * 1024;
};

Config config_from_environment();
void run(const Config& config);

} // namespace gateway
