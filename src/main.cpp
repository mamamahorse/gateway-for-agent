#include "gateway.hpp"

#include <charconv>
#include <cstdlib>
#include <iostream>
#include <regex>
#include <stdexcept>
#include <string_view>

namespace gateway {
namespace {

std::string environment(const char* name, std::string fallback = {}) {
    const auto* value = std::getenv(name);
    return value ? std::string(value) : std::move(fallback);
}

int positive_number(const char* name, int fallback, int maximum = 3600000) {
    const auto value = environment(name);
    if (value.empty()) return fallback;
    int result{};
    const auto [end, error] = std::from_chars(value.data(), value.data() + value.size(), result);
    if (error != std::errc{} || end != value.data() + value.size() || result <= 0 || result > maximum)
        throw std::runtime_error(std::string(name) + " must be a positive integer within its documented limit");
    return result;
}

} // namespace

Config config_from_environment() {
    Config config;
    config.api_key = environment("GATEWAY_API_KEY");
    if (config.api_key.empty() || config.api_key.size() > 512 ||
        config.api_key.find_first_of("\r\n") != std::string::npos)
        throw std::runtime_error("GATEWAY_API_KEY must be set to a nonempty, single-line value of at most 512 bytes");
    config.listen_address = environment("GATEWAY_LISTEN_ADDRESS", config.listen_address);
    config.listen_port = static_cast<unsigned short>(positive_number("GATEWAY_PORT", config.listen_port, 65535));
    const auto upstream = environment("GATEWAY_UPSTREAM", "http://127.0.0.1:8000");
    std::smatch parts;
    if (!std::regex_match(upstream, parts, std::regex(R"(^http://([A-Za-z0-9._-]+)(?::([0-9]+))?/?$)")))
        throw std::runtime_error("GATEWAY_UPSTREAM must be an http://host[:port] origin, without credentials or a path");
    config.upstream_host = parts[1].str();
    config.upstream_port = parts[2].matched ? parts[2].str() : "80";
    int port{};
    const auto [end, error] = std::from_chars(config.upstream_port.data(),
        config.upstream_port.data() + config.upstream_port.size(), port);
    if (error != std::errc{} || end != config.upstream_port.data() + config.upstream_port.size() || port < 1 || port > 65535)
        throw std::runtime_error("GATEWAY_UPSTREAM has an invalid port");
    config.health_path = environment("GATEWAY_UPSTREAM_HEALTH_PATH", config.health_path);
    if (config.health_path.empty() || config.health_path.front() != '/' ||
        config.health_path.find_first_of("\r\n #") != std::string::npos || config.health_path.starts_with("//"))
        throw std::runtime_error("GATEWAY_UPSTREAM_HEALTH_PATH must be an origin-relative HTTP target");
    config.header_timeout_ms = positive_number("GATEWAY_HEADER_TIMEOUT_MS", config.header_timeout_ms);
    config.request_timeout_ms = positive_number("GATEWAY_REQUEST_TIMEOUT_MS", config.request_timeout_ms);
    config.model_timeout_ms = positive_number("GATEWAY_MODEL_TIMEOUT_MS", config.model_timeout_ms);
    config.connect_timeout_ms = positive_number("GATEWAY_CONNECT_TIMEOUT_MS", config.connect_timeout_ms);
    config.readiness_timeout_ms = positive_number("GATEWAY_READINESS_TIMEOUT_MS", config.readiness_timeout_ms);
    config.stream_timeout_ms = positive_number("GATEWAY_STREAM_TIMEOUT_MS", config.stream_timeout_ms);
    config.stream_first_byte_timeout_ms = positive_number("GATEWAY_STREAM_FIRST_BYTE_TIMEOUT_MS", config.stream_first_byte_timeout_ms);
    config.stream_idle_timeout_ms = positive_number("GATEWAY_STREAM_IDLE_TIMEOUT_MS", config.stream_idle_timeout_ms);
    config.stream_write_timeout_ms = positive_number("GATEWAY_STREAM_WRITE_TIMEOUT_MS", config.stream_write_timeout_ms);
    config.upload_timeout_ms = positive_number("GATEWAY_UPLOAD_TIMEOUT_MS", config.upload_timeout_ms);
    const auto admission_enabled = environment("GATEWAY_ADMISSION_ENABLED", "0");
    if (admission_enabled != "0" && admission_enabled != "1")
        throw std::runtime_error("GATEWAY_ADMISSION_ENABLED must be 0 or 1");
    config.admission.enabled = admission_enabled == "1";
    config.admission.max_active = static_cast<std::size_t>(positive_number("GATEWAY_MODEL_CONCURRENCY", 2, 128));
    config.admission.max_queued = static_cast<std::size_t>(positive_number("GATEWAY_MODEL_QUEUE_LIMIT", 8, 128));
    config.admission.max_queue_bytes = static_cast<std::size_t>(positive_number("GATEWAY_MODEL_QUEUE_BYTES", 4 * 1024 * 1024, 128 * 1024 * 1024));
    config.admission.queue_timeout_ms = positive_number("GATEWAY_MODEL_QUEUE_TIMEOUT_MS", 2000, 60000);
    const auto enabled = environment("GATEWAY_RATE_LIMIT_ENABLED", "0");
    if (enabled != "0" && enabled != "1")
        throw std::runtime_error("GATEWAY_RATE_LIMIT_ENABLED must be 0 or 1");
    config.redis.enabled = enabled == "1";
    config.redis.host = environment("GATEWAY_REDIS_HOST", config.redis.host);
    if (!std::regex_match(config.redis.host, std::regex(R"(^[A-Za-z0-9._-]{1,253}$)")))
        throw std::runtime_error("GATEWAY_REDIS_HOST must be a hostname or IPv4 address");
    config.redis.port = std::to_string(positive_number("GATEWAY_REDIS_PORT", 6379, 65535));
    config.redis.prefix = environment("GATEWAY_RATE_LIMIT_PREFIX", config.redis.prefix);
    config.redis.key_id = environment("GATEWAY_KEY_ID", config.redis.key_id);
    if (!std::regex_match(config.redis.prefix, std::regex(R"(^[A-Za-z0-9:_-]{1,64}$)")))
        throw std::runtime_error("GATEWAY_RATE_LIMIT_PREFIX must be 1-64 namespace characters");
    if (!std::regex_match(config.redis.key_id, std::regex(R"(^[A-Za-z0-9_-]{1,64}$)")))
        throw std::runtime_error("GATEWAY_KEY_ID must be 1-64 public identity characters");
    config.redis.timeout_ms = positive_number("GATEWAY_REDIS_TIMEOUT_MS", config.redis.timeout_ms, 60000);
    config.redis.window_ms = positive_number("GATEWAY_RATE_LIMIT_WINDOW_MS", config.redis.window_ms);
    config.redis.normal_limit = positive_number("GATEWAY_RATE_LIMIT_NORMAL", config.redis.normal_limit, 1000000);
    config.redis.model_limit = positive_number("GATEWAY_RATE_LIMIT_MODEL", config.redis.model_limit, 1000000);
    config.redis.upload_limit = positive_number("GATEWAY_RATE_LIMIT_UPLOAD", config.redis.upload_limit, 1000000);
    return config;
}

} // namespace gateway

int main() {
    try {
        gateway::run(gateway::config_from_environment());
        return 0;
    } catch (const std::exception& error) {
        // Config diagnostics contain names and constraints, never supplied values.
        std::cerr << "Gateway startup failed: " << error.what() << '\n';
        return 1;
    }
}
