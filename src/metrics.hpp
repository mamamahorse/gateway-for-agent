#pragma once

#include <array>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <string>
#include <string_view>

namespace gateway {
class AdmissionController;
struct AdmissionSettings;

enum class BusinessState { unknown, completed, failed, unavailable };
// Inspection is optional telemetry. It never changes the response being forwarded.
BusinessState inspect_business_json(std::string_view body);

class StreamObserver {
public:
    void feed(std::string_view bytes);
    bool failed() const { return failed_; }
    bool done() const { return done_; }
    bool unavailable() const { return unavailable_; }
private:
    static constexpr std::size_t limit = 8192;
    std::string line_;
    std::string event_;
    std::string data_;
    bool line_overflow_ = false;
    bool data_overflow_ = false;
    bool data_seen_ = false;
    bool failed_ = false;
    bool done_ = false;
    bool unavailable_ = false;
    bool skip_lf_ = false;
    bool first_line_ = true;
    void consume_line();
    void dispatch();
};

// Accessed only by the existing single io_context thread. No per-client/URL labels.
class Metrics {
public:
    void accepted(bool admitted);
    void connection_closed();
    void model_started();
    void record(unsigned status, std::string_view reason, bool model_counted,
                bool business_failure, bool inspection_unavailable, bool stream_without_done,
                std::size_t request_bytes, std::size_t response_bytes, std::uint64_t duration_ms);
    std::string snapshot(const AdmissionController& admission, const AdmissionSettings& settings) const;
private:
    using Counter = std::uint64_t;
    static constexpr std::array<std::string_view, 31> reason_names{
        "proxied", "stream_complete", "auth_rejected", "route_rejected", "invalid_target",
        "invalid_http", "upgrade_rejected", "upload_type_rejected", "expect_rejected",
        "body_limit", "header_limit", "rate_limited", "redis_unavailable", "queue_full",
        "queue_timeout", "queue_client_closed", "queue_client_error", "queue_unexpected_data",
        "client_disconnected", "client_timeout", "timeout", "connect_timeout", "upstream_error",
        "invalid_stream", "stream_total_timeout", "stream_read_timeout", "upstream_stream_error",
        "stream_write_error", "downstream_write_error", "shutdown", "other"};
    std::chrono::steady_clock::time_point started_ = std::chrono::steady_clock::now();
    Counter connections_accepted_ = 0;
    Counter connections_rejected_ = 0;
    Counter connections_active_ = 0;
    Counter model_in_flight_ = 0;
    Counter finished_ = 0;
    Counter status_other_ = 0;
    Counter upstream_http_errors_ = 0;
    Counter business_failures_ = 0;
    Counter inspection_unavailable_ = 0;
    Counter streams_without_done_ = 0;
    Counter request_bytes_ = 0;
    Counter response_bytes_ = 0;
    Counter duration_ms_ = 0;
    std::array<Counter, 600> statuses_{};
    std::array<Counter, reason_names.size()> reasons_{};
};
} // namespace gateway
