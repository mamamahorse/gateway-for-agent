#include "metrics.hpp"
#include "admission.hpp"

// Compile bundled Boost.JSON here only, like the existing Boost.Redis implementation TU.
#include <boost/json/src.hpp>
#include <algorithm>

namespace gateway {
namespace json = boost::json;

BusinessState inspect_business_json(std::string_view body) {
    if (body.size() > 64 * 1024) return BusinessState::unavailable;
    boost::system::error_code error;
    json::parse_options options;
    options.max_depth = 16;
    const auto value = json::parse({body.data(), body.size()}, error, {}, options);
    if (error) return BusinessState::unavailable;
    if (!value.is_object()) return BusinessState::unknown;
    const auto* status = value.as_object().if_contains("status");
    if (!status || !status->is_string()) return BusinessState::unknown;
    if (status->as_string() == "failed") return BusinessState::failed;
    if (status->as_string() == "completed") return BusinessState::completed;
    return BusinessState::unknown;
}

void StreamObserver::feed(std::string_view bytes) {
    while (!bytes.empty()) {
        if (skip_lf_) {
            if (bytes.front() == '\n') bytes.remove_prefix(1);
            skip_lf_ = false;
            if (bytes.empty()) return;
        }
        const auto newline = bytes.find_first_of("\r\n");
        const auto part = bytes.substr(0, newline);
        const auto available = limit - line_.size();
        line_.append(part.substr(0, available));
        if (part.size() > available) line_overflow_ = true;
        if (newline == std::string_view::npos) return;
        skip_lf_ = bytes[newline] == '\r';
        consume_line();
        line_.clear();
        line_overflow_ = false;
        bytes.remove_prefix(newline + 1);
    }
}

void StreamObserver::consume_line() {
    std::string_view line(line_);
    if (first_line_) {
        if (line.starts_with("\xEF\xBB\xBF")) line.remove_prefix(3);
        first_line_ = false;
    }
    if (line.empty() && !line_overflow_) return dispatch();
    if (line.empty() || line.front() == ':') return;
    const auto colon = line.find(':');
    const auto field = line.substr(0, colon);
    auto value = colon == std::string_view::npos ? std::string_view{} : line.substr(colon + 1);
    if (!value.empty() && value.front() == ' ') value.remove_prefix(1);
    if (field == "event") {
        event_ = !line_overflow_ && value.size() <= 64 ? std::string(value) : std::string{};
    } else if (field == "data") {
        data_seen_ = true;
        if (line_overflow_ || value.size() + 1 > limit - data_.size()) {
            data_overflow_ = true;
        } else if (!data_overflow_) {
            data_.append(value);
            data_.push_back('\n');
        }
    }
}

void StreamObserver::dispatch() {
    if (data_seen_ && event_ == "error") failed_ = true;
    if (data_seen_ && event_ == "done") {
        const auto state = data_overflow_ ? BusinessState::unavailable : inspect_business_json(data_);
        if (state == BusinessState::failed) failed_ = true;
        if (state == BusinessState::failed || state == BusinessState::completed) done_ = true;
        else unavailable_ = true;
    }
    event_.clear();
    data_.clear();
    data_seen_ = false;
    data_overflow_ = false;
}

void Metrics::accepted(bool admitted) {
    ++connections_accepted_;
    if (admitted) ++connections_active_;
    else ++connections_rejected_;
}
void Metrics::connection_closed() { --connections_active_; }
void Metrics::model_started() { ++model_in_flight_; }

void Metrics::record(unsigned status, std::string_view reason, bool model_counted,
                     bool business_failure, bool inspection_unavailable, bool stream_without_done,
                     std::size_t request_bytes, std::size_t response_bytes, std::uint64_t duration_ms) {
    ++finished_;
    if (model_counted) --model_in_flight_;
    if (status < statuses_.size()) ++statuses_[status];
    else ++status_other_;
    const auto found = std::find(reason_names.begin(), reason_names.end(), reason);
    ++reasons_[found == reason_names.end() ? reason_names.size() - 1 :
               static_cast<std::size_t>(found - reason_names.begin())];
    if (reason == "proxied" && status >= 400) ++upstream_http_errors_;
    if (business_failure) ++business_failures_;
    if (inspection_unavailable) ++inspection_unavailable_;
    if (stream_without_done) ++streams_without_done_;
    request_bytes_ += static_cast<Counter>(request_bytes);
    response_bytes_ += static_cast<Counter>(response_bytes);
    duration_ms_ += duration_ms;
}

std::string Metrics::snapshot(const AdmissionController& admission, const AdmissionSettings& settings) const {
    json::object statuses;
    for (std::size_t i = 0; i < statuses_.size(); ++i)
        if (statuses_[i]) statuses[std::to_string(i)] = statuses_[i];
    json::object reasons;
    for (std::size_t i = 0; i < reason_names.size(); ++i)
        reasons[std::string(reason_names[i])] = reasons_[i];
    return json::serialize(json::object{
        {"schema_version", 1}, {"scope", "gateway_process"},
        {"uptime_ms", std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::steady_clock::now() - started_).count()},
        {"requests", json::object{{"finished", finished_}, {"http_status", std::move(statuses)},
            {"status_other", status_other_}, {"model_http_in_flight", model_in_flight_},
            {"upstream_http_errors", upstream_http_errors_}, {"observed_business_failures", business_failures_},
            {"business_inspection_unavailable", inspection_unavailable_},
            {"streams_without_done", streams_without_done_}, {"duration_ms_total", duration_ms_},
            {"request_body_bytes_total", request_bytes_}, {"response_body_bytes_total", response_bytes_}}},
        {"admission", json::object{{"enabled", settings.enabled}, {"held_permits", admission.active()},
            {"queued", admission.queued()}, {"queued_body_bytes", admission.queued_bytes()},
            {"active_limit", settings.max_active}, {"queue_limit", settings.max_queued},
            {"queue_bytes_limit", settings.max_queue_bytes}, {"queue_timeout_ms", settings.queue_timeout_ms}}},
        {"connections", json::object{{"accepted_total", connections_accepted_},
            {"limit_rejected_total", connections_rejected_}, {"active", connections_active_}}},
        {"end_reasons", std::move(reasons)}});
}
} // namespace gateway
