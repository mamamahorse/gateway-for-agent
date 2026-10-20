#include "gateway.hpp"
#include "policy.hpp"
#include "metrics.hpp"

#include <boost/asio.hpp>
#include <boost/beast.hpp>
#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <csignal>
#include <functional>
#include <iostream>
#include <memory>
#include <optional>
#include <string>
#include <unordered_map>
#include <vector>

namespace gateway {
namespace net = boost::asio;
namespace beast = boost::beast;
namespace http = beast::http;
using tcp = net::ip::tcp;
using Clock = std::chrono::steady_clock;
using Error = boost::system::error_code;

namespace {
std::atomic<unsigned long long> next_request{0};
const auto process_epoch = std::chrono::duration_cast<std::chrono::milliseconds>(
    std::chrono::system_clock::now().time_since_epoch()).count();

beast::string_view media_type(beast::string_view value) {
    auto media = value.substr(0, value.find(';'));
    while (!media.empty() && (media.back() == ' ' || media.back() == '\t')) media.remove_suffix(1);
    return media;
}

class ProxySession : public std::enable_shared_from_this<ProxySession> {
public:
    ProxySession(tcp::socket socket, const Config& config, std::shared_ptr<AdmissionController> admission,
                 std::shared_ptr<Metrics> metrics,
                 std::function<void(ProxySession*)> release)
        : downstream_(std::move(socket)), upstream_(downstream_.get_executor()),
          resolver_(downstream_.get_executor()), deadline_(downstream_.get_executor()),
          connect_deadline_(downstream_.get_executor()), upload_read_deadline_(downstream_.get_executor()),
          config_(config), admission_(std::move(admission)), metrics_(std::move(metrics)), release_(std::move(release)),
          request_id_("gw-" + std::to_string(process_epoch) + "-" + std::to_string(++next_request)) {
        request_.header_limit(32 * 1024);
        // Decide the smaller JSON bound after routing, before parsing body octets.
        request_.body_limit(config_.upload_body_limit);
        response_parser_.header_limit(32 * 1024);
        response_parser_.body_limit(config_.stream_body_limit);
    }

    void start() {
        arm_deadline(config_.header_timeout_ms);
        http::async_read_header(downstream_, downstream_buffer_, request_,
            [self = shared_from_this()](Error error, std::size_t) {
                if (self->reply_started_ || self->finished_) return;
                if (error) return self->client_error(error);
                self->read_request();
            });
    }

    void stop() { finish("shutdown"); }

private:
    beast::tcp_stream downstream_;
    beast::tcp_stream upstream_;
    tcp::resolver resolver_;
    net::steady_timer deadline_;
    net::steady_timer connect_deadline_;
    net::steady_timer upload_read_deadline_;
    beast::flat_buffer downstream_buffer_{64 * 1024};
    beast::flat_buffer upstream_buffer_{64 * 1024};
    http::request_parser<http::string_body> request_;
    http::response_parser<http::string_body> response_parser_;
    http::request<http::string_body> upstream_request_;
    http::response<http::string_body> response_;
    std::unique_ptr<http::request_parser<http::buffer_body>> upload_parser_;
    std::unique_ptr<http::response_parser<http::buffer_body>> stream_parser_;
    http::request<http::empty_body> upload_header_;
    http::response<http::empty_body> stream_header_;
    http::response<http::empty_body> continue_response_{http::status::continue_, 11};
    std::unique_ptr<http::request_serializer<http::empty_body>> upload_serializer_;
    std::unique_ptr<http::response_serializer<http::empty_body>> stream_serializer_;
    std::array<char, 16 * 1024> upload_buffer_{};
    std::array<char, 16 * 1024> stream_buffer_{};
    std::optional<http::chunk_body<net::const_buffer>> upload_chunk_;
    std::optional<http::chunk_body<net::const_buffer>> stream_chunk_;
    std::shared_ptr<RedisCheck> redis_check_;
    decltype(http::make_chunk_last()) last_chunk_{http::make_chunk_last()};
    const Config& config_;
    std::shared_ptr<AdmissionController> admission_;
    std::shared_ptr<Metrics> metrics_;
    StreamObserver stream_observer_;
    Permit permit_;
    AdmissionController::Ticket queue_ticket_ = 0;
    net::cancellation_signal queue_cancellation_;
    net::cancellation_signal client_cancellation_;
    bool watching_model_client_ = false;
    Clock::time_point admission_started_;
    long long admission_wait_ms_ = 0;
    std::function<void(ProxySession*)> release_;
    std::string request_id_;
    std::string route_ = "unmatched";
    std::string method_ = "UNKNOWN";
    std::string reason_;
    Clock::time_point started_ = Clock::now();
    unsigned long long deadline_generation_ = 0;
    unsigned long long upload_read_generation_ = 0;
    bool readiness_ = false;
    bool local_probe_ = false;
    bool agent_metrics_ = false;
    bool model_counted_ = false;
    bool stream_observed_ = false;
    bool business_failure_ = false;
    bool business_inspection_unavailable_ = false;
    bool model_request_ = false;
    bool stream_request_ = false;
    bool upload_request_ = false;
    bool upload_stopped_ = false;
    bool upstream_started_ = false;
    bool reply_started_ = false;
    bool finished_ = false;
    std::size_t request_bytes_ = 0;
    std::size_t response_bytes_ = 0;
    Clock::time_point stream_read_deadline_;

    void arm_deadline(int milliseconds) {
        const auto generation = ++deadline_generation_;
        deadline_.expires_after(std::chrono::milliseconds(milliseconds));
        deadline_.async_wait([weak = weak_from_this(), generation](Error error) {
            if (error) return;
            if (auto self = weak.lock(); self && generation == self->deadline_generation_ &&
                !self->finished_) {
                if (self->reply_started_) return self->finish("stream_total_timeout");
                self->error_reply(self->readiness_ ? http::status::service_unavailable :
                                  (self->upstream_started_ ? http::status::gateway_timeout : http::status::request_timeout),
                                  "GATEWAY_TIMEOUT", "Request deadline exceeded", "timeout");
            }
        });
    }

    void client_error(Error error) {
        if (error == beast::error::timeout)
            return error_reply(http::status::request_timeout, "CLIENT_TIMEOUT", "Client body read deadline exceeded", "client_timeout");
        if (error == http::error::end_of_stream || error == net::error::eof ||
            error == net::error::connection_reset) return finish("client_disconnected");
        if (error == http::error::body_limit)
            return error_reply(http::status::payload_too_large, "BODY_TOO_LARGE", "Request body is too large", "body_limit");
        if (error == http::error::header_limit)
            return error_reply(http::status::request_header_fields_too_large, "HEADERS_TOO_LARGE", "Headers are too large", "header_limit");
        error_reply(http::status::bad_request, "INVALID_HTTP", "Invalid HTTP request", "invalid_http");
    }

    void read_request() {
        auto& request = request_.get();
        method_ = std::string(http::to_string(request.method()));
        std::string_view target(request.target().data(), request.target().size());
        if (target.empty() || target.front() != '/' || target.starts_with("//") || target.find('#') != target.npos)
            return error_reply(http::status::bad_request, "INVALID_TARGET", "An origin-relative target is required", "invalid_target");
        const auto path = target.substr(0, target.find('?'));
        if (request.method() == http::verb::get && path == "/metrics") {
            route_ = "/metrics";
            local_probe_ = true;
            if (!valid_key())
                return error_reply(http::status::unauthorized, "INVALID_API_KEY", "A valid X-API-Key is required", "auth_rejected");
            return json_reply(http::status::ok, metrics_->snapshot(*admission_, config_.admission), "metrics");
        }
        if (request.method() == http::verb::get && path == "/agent-metrics") {
            route_ = "/agent-metrics";
            local_probe_ = true;
            agent_metrics_ = true;
            if (!valid_key())
                return error_reply(http::status::unauthorized, "INVALID_API_KEY", "A valid X-API-Key is required", "auth_rejected");
            if (request_.upgrade() || request.chunked() || (request_.content_length() && *request_.content_length() != 0))
                return error_reply(http::status::bad_request, "INVALID_HTTP", "Metrics requires a bodyless GET", "invalid_http");
            request.target("/internal/metrics");
            // Operational observation must remain available during Redis or
            // model-capacity failures. Python availability is still required.
            return receive_body();
        }
        if (request.method() == http::verb::get && path == "/livez") {
            route_ = "/livez";
            local_probe_ = true;
            return json_reply(http::status::ok, "{\"status\":\"alive\"}", "local");
        }
        if (request.method() == http::verb::get && path == "/readyz") {
            route_ = "/readyz";
            readiness_ = true;
            local_probe_ = true;
        } else {
            const auto route = route_for(request.method(), path);
            if (!route) return error_reply(http::status::not_found, "ROUTE_NOT_FOUND", "Route is not enabled", "route_rejected");
            route_ = std::string(route->pattern);
            model_request_ = route->model_request;
            stream_request_ = route->stream_request;
            upload_request_ = route->upload_request;
            if (!valid_key())
                return error_reply(http::status::unauthorized, "INVALID_API_KEY", "A valid X-API-Key is required", "auth_rejected");
            if (model_request_) {
                model_counted_ = true;
                metrics_->model_started();
            }
        }
        if (request_.upgrade())
            return error_reply(http::status::bad_request, "UPGRADE_NOT_SUPPORTED", "Protocol upgrades are not supported", "upgrade_rejected");
        const auto limit = upload_request_ ? config_.upload_body_limit : config_.request_body_limit;
        if (request_.content_length() && *request_.content_length() > limit)
            return client_error(http::error::body_limit);
        request_.body_limit(limit);
        if (upload_request_) {
            const auto type = request[http::field::content_type];
            if (!beast::iequals(media_type(type), "multipart/form-data"))
                return error_reply(http::status::unsupported_media_type, "MULTIPART_REQUIRED", "Upload requires multipart/form-data with a boundary", "upload_type_rejected");
        }
        const auto expect = request[http::field::expect];
        if (!expect.empty()) {
            if (!upload_request_ || !beast::iequals(expect, "100-continue"))
                return error_reply(http::status::expectation_failed, "EXPECT_NOT_SUPPORTED", "Only upload 100-continue is supported", "expect_rejected");
        }
        if (config_.redis.enabled) {
            // The Redis operation bounds DNS/connect/handshake/write/read with
            // its own deadline; this wait is not a slow-client header read.
            ++deadline_generation_;
            deadline_.cancel();
            const auto category = upload_request_ ? RequestClass::upload :
                model_request_ ? RequestClass::model : RequestClass::normal;
            redis_check_ = RedisCheck::start(downstream_.get_executor(), config_.redis, category, readiness_,
                [self = shared_from_this()](LimitDecision decision) {
                    if (self->reply_started_ || self->finished_) return;
                    self->redis_check_.reset();
                    if (decision.status == LimitStatus::unavailable)
                        return self->error_reply(http::status::service_unavailable, "REDIS_UNAVAILABLE",
                            "Rate limit service is unavailable", "redis_unavailable");
                    if (decision.status == LimitStatus::denied)
                        return self->error_reply(http::status::too_many_requests, "RATE_LIMITED",
                            "Request rate limit exceeded", "rate_limited",
                            static_cast<int>((decision.retry_ms + 999) / 1000));
                    self->after_rate_limit();
                });
            return;
        }
        after_rate_limit();
    }

    bool valid_key() const {
        const auto& request = request_.get();
        const auto key = request["X-API-Key"];
        return request.count("X-API-Key") == 1 && key_matches({key.data(), key.size()}, config_.api_key);
    }

    void after_rate_limit() {
        auto& request = request_.get();
        if (!request[http::field::expect].empty()) {
            request.erase(http::field::expect);
            arm_deadline(config_.header_timeout_ms);
            downstream_.expires_after(std::chrono::milliseconds(config_.header_timeout_ms));
            return http::async_write(downstream_, continue_response_,
                [self = shared_from_this()](Error error, std::size_t) {
                    if (self->reply_started_ || self->finished_) return;
                    if (error) return self->finish("downstream_write_error");
                    self->receive_body();
                });
        }
        receive_body();
    }

    void receive_body() {
        downstream_.expires_never();
        if (upload_request_) {
            upstream_request_.base() = request_.get().base();
            upload_parser_ = std::make_unique<http::request_parser<http::buffer_body>>(std::move(request_));
            return begin_upstream();
        }
        arm_deadline(config_.header_timeout_ms);
        if (request_.is_done()) return admit_or_begin();
        http::async_read(downstream_, downstream_buffer_, request_,
            [self = shared_from_this()](Error error, std::size_t) {
                if (self->reply_started_ || self->finished_) return;
                if (error) return self->client_error(error);
                self->admit_or_begin();
            });
    }

    void admit_or_begin() {
        if (!config_.admission.enabled || !model_request_) return begin_upstream();
        ++deadline_generation_;
        deadline_.cancel();
        admission_started_ = Clock::now();
        queue_ticket_ = admission_->acquire(request_.get().body().size(),
            [weak = weak_from_this()](AdmissionStatus status, Permit permit) mutable {
                auto self = weak.lock();
                if (!self || self->finished_ || self->reply_started_) return;
                self->queue_ticket_ = 0;
                self->queue_cancellation_.emit(net::cancellation_type::terminal);
                self->admission_wait_ms_ = std::chrono::duration_cast<std::chrono::milliseconds>(
                    Clock::now() - self->admission_started_).count();
                if (status != AdmissionStatus::granted)
                    return self->error_reply(http::status::service_unavailable, "GATEWAY_OVERLOADED",
                        "Model request capacity exceeded", status == AdmissionStatus::timed_out ?
                        "queue_timeout" : status == AdmissionStatus::full ? "queue_full" : "shutdown", 1);
                self->permit_ = std::move(permit);
                self->begin_upstream();
            });
        if (queue_ticket_ != 0) {
            std::cout << "{\"event\":\"admission\",\"action\":\"queued\",\"request_id\":\"" << request_id_
                      << "\",\"active\":" << admission_->active() << ",\"queued\":" << admission_->queued()
                      << ",\"queued_bytes\":" << admission_->queued_bytes() << "}" << std::endl;
            watch_queue_client();
        }
    }

    void watch_queue_client() {
        // Only a socket wait is pending here: the complete bounded JSON body is
        // already parsed. Cancelling this wait must not cancel subsequent writes.
        Error error;
        downstream_.socket().non_blocking(true, error);
        if (error) return finish("queue_client_error");
        downstream_.socket().async_wait(tcp::socket::wait_read,
            net::bind_cancellation_slot(queue_cancellation_.slot(),
            [self = shared_from_this()](Error wait_error) {
                if (!self->queue_ticket_ || self->finished_ || self->reply_started_) return;
                if (wait_error == net::error::operation_aborted) return;
                if (wait_error) return self->finish("queue_client_error");
                char byte{};
                Error peek_error;
                const auto bytes = self->downstream_.socket().receive(net::buffer(&byte, 1),
                    tcp::socket::message_peek, peek_error);
                if (peek_error == net::error::would_block || peek_error == net::error::try_again)
                    return self->watch_queue_client();
                // Queued requests require the client's write side to stay open;
                // FIN means withdrawal here. Pipelining is not supported.
                self->finish(bytes == 0 || peek_error ? "queue_client_closed" : "queue_unexpected_data");
            }));
    }

    void cancel_queue() {
        if (queue_ticket_ != 0) {
            admission_->cancel(queue_ticket_);
            queue_ticket_ = 0;
        }
        queue_cancellation_.emit(net::cancellation_type::terminal);
    }

    void begin_upstream() {
        upstream_started_ = true;
        if (model_request_) {
            watching_model_client_ = true;
            watch_model_client();
            if (finished_) return;
        }
        if (readiness_) {
            upstream_request_ = {http::verb::get, config_.health_path, 11};
        } else if (!upload_request_) {
            upstream_request_ = request_.release();
        }
        remove_hop_headers(upstream_request_.base());
        upstream_request_.set(http::field::host, config_.upstream_host + ":" + config_.upstream_port);
        upstream_request_.set(http::field::accept_encoding, "identity");
        upstream_request_.set("X-Request-ID", request_id_);
        upstream_request_.keep_alive(false);
        upstream_request_.prepare_payload();
        request_bytes_ = upstream_request_.body().size();
        arm_deadline((readiness_ || agent_metrics_) ? config_.readiness_timeout_ms : upload_request_ ? config_.upload_timeout_ms :
                     stream_request_ ? config_.stream_timeout_ms :
                     (model_request_ ? config_.model_timeout_ms : config_.request_timeout_ms));
        connect_deadline_.expires_after(std::chrono::milliseconds(config_.connect_timeout_ms));
        connect_deadline_.async_wait([weak = weak_from_this()](Error error) {
            if (error) return;
            if (auto self = weak.lock(); self && !self->reply_started_ && !self->finished_)
                self->error_reply(self->readiness_ ? http::status::service_unavailable : http::status::gateway_timeout,
                                  "UPSTREAM_CONNECT_TIMEOUT", "Upstream connection deadline exceeded", "connect_timeout");
        });
        resolver_.async_resolve(config_.upstream_host, config_.upstream_port,
            [self = shared_from_this()](Error error, tcp::resolver::results_type endpoints) {
                if (self->reply_started_ || self->finished_) return;
                if (error) return self->upstream_error(error);
                self->upstream_.async_connect(endpoints,
                    [self](Error connect_error, const tcp::resolver::results_type::endpoint_type&) {
                        if (self->reply_started_ || self->finished_) return;
                        self->connect_deadline_.cancel();
                        if (connect_error) return self->upstream_error(connect_error);
                        self->write_upstream();
                    });
            });
    }

    void watch_model_client() {
        // The bounded JSON body is complete. A read readiness wait can coexist
        // with downstream writes, without consuming data or cancelling them.
        // For model requests FIN means withdrawal (also for TCP write-half-close).
        Error error;
        downstream_.socket().non_blocking(true, error);
        if (error) return finish("client_disconnected");
        downstream_.socket().async_wait(tcp::socket::wait_read,
            net::bind_cancellation_slot(client_cancellation_.slot(),
            [self = shared_from_this()](Error wait_error) {
                if (!self->watching_model_client_ || self->finished_) return;
                if (wait_error == net::error::operation_aborted) return;
                if (wait_error) return self->finish("client_disconnected");
                char byte{};
                Error peek_error;
                const auto bytes = self->downstream_.socket().receive(net::buffer(&byte, 1),
                    tcp::socket::message_peek, peek_error);
                if (peek_error == net::error::would_block || peek_error == net::error::try_again)
                    return self->watch_model_client();
                self->finish(bytes == 0 || peek_error ? "client_disconnected" : "invalid_http");
            }));
    }

    void write_upstream() {
        if (upload_request_) {
            upload_header_.base() = upstream_request_.base();
            upload_header_.erase(http::field::content_length);
            upload_header_.chunked(true);
            upload_serializer_ = std::make_unique<http::request_serializer<http::empty_body>>(upload_header_);
            return http::async_write_header(upstream_, *upload_serializer_,
                [self = shared_from_this()](Error error, std::size_t) {
                    if (self->reply_started_ || self->finished_) return;
                    if (error) return self->upstream_error(error);
                    // Read early upstream rejection concurrently with request-body forwarding.
                    self->read_upstream_header();
                    self->read_upload_block();
                });
        }
        http::async_write(upstream_, upstream_request_,
            [self = shared_from_this()](Error error, std::size_t) {
                if (self->reply_started_ || self->finished_) return;
                if (error) return self->upstream_error(error);
                self->read_upstream_header();
            });
    }

    void read_upstream_header() {
        http::async_read_header(upstream_, upstream_buffer_, response_parser_,
            [self = shared_from_this()](Error error, std::size_t) {
                if (self->reply_started_ || self->finished_) return;
                if (error) return self->upstream_error(error);
                const auto& response = self->response_parser_.get();
                self->upload_stopped_ = true;
                self->upload_read_deadline_.cancel();
                if (self->readiness_) {
                    const bool healthy = response.result_int() >= 200 && response.result_int() < 300;
                    return self->json_reply(healthy ? http::status::ok : http::status::service_unavailable,
                        healthy ? "{\"status\":\"ready\"}" : "{\"status\":\"not_ready\"}", "readiness");
                }
                const auto type = response[http::field::content_type];
                const auto media = media_type(type);
                const bool success = response.result_int() >= 200 && response.result_int() < 300;
                const bool event_stream = beast::iequals(media, "text/event-stream");
                if (self->stream_request_ && success) {
                    if (!event_stream)
                        return self->error_reply(http::status::bad_gateway, "INVALID_STREAM_RESPONSE", "Upstream did not return SSE", "invalid_stream");
                    const auto encoding = response[http::field::content_encoding];
                    if (!encoding.empty() && !beast::iequals(encoding, "identity"))
                        return self->error_reply(http::status::bad_gateway, "ENCODED_STREAM_UNSUPPORTED", "Upstream must return an uncompressed stream", "invalid_stream");
                    return self->start_stream();
                }
                if (event_stream && success)
                    return self->error_reply(http::status::bad_gateway, "UNEXPECTED_STREAM", "This route requires a bounded response", "invalid_stream");
                if (self->response_parser_.content_length() &&
                    *self->response_parser_.content_length() > self->config_.response_body_limit)
                    return self->upstream_error(http::error::body_limit);
                self->response_parser_.body_limit(self->config_.response_body_limit);
                http::async_read(self->upstream_, self->upstream_buffer_, self->response_parser_,
                    [self](Error body_error, std::size_t) {
                        if (self->reply_started_ || self->finished_) return;
                        if (body_error) return self->upstream_error(body_error);
                        self->response_ = self->response_parser_.release();
                        if (self->model_request_ && !self->stream_request_ &&
                            self->response_.result_int() >= 200 && self->response_.result_int() < 300) {
                            const auto encoding = self->response_[http::field::content_encoding];
                            if (beast::iequals(media_type(self->response_[http::field::content_type]), "application/json") &&
                                (encoding.empty() || beast::iequals(encoding, "identity"))) {
                                const auto state = inspect_business_json(self->response_.body());
                                self->business_failure_ = state == BusinessState::failed;
                                self->business_inspection_unavailable_ = state == BusinessState::unavailable;
                            } else self->business_inspection_unavailable_ = true;
                        }
                        remove_hop_headers(self->response_.base());
                        self->respond("proxied");
                    });
            });
    }

    void read_upload_block() {
        if (finished_ || reply_started_ || upload_stopped_) return;
        if (upload_parser_->is_done()) {
            return net::async_write(upstream_, last_chunk_,
                [self = shared_from_this()](Error error, std::size_t) {
                    if (self->finished_ || self->reply_started_ || self->upload_stopped_) return;
                    if (error) self->upstream_error(error);
                    // Response-header read is already outstanding; do not start a second read.
                });
        }
        auto& body = upload_parser_->get().body();
        body.data = upload_buffer_.data();
        body.size = upload_buffer_.size();
        // A tcp_stream read timeout closes its socket. Use our own timer so a
        // stalled upload can still receive HTTP 408 before the connection closes.
        downstream_.expires_never();
        const auto generation = ++upload_read_generation_;
        upload_read_deadline_.expires_after(std::chrono::milliseconds(config_.header_timeout_ms));
        upload_read_deadline_.async_wait([weak = weak_from_this(), generation](Error error) {
            if (error) return;
            if (auto self = weak.lock(); self && generation == self->upload_read_generation_ &&
                !self->finished_ && !self->reply_started_ && !self->upload_stopped_)
                self->error_reply(http::status::request_timeout, "CLIENT_TIMEOUT", "Client body read deadline exceeded", "client_timeout");
        });
        http::async_read_some(downstream_, downstream_buffer_, *upload_parser_,
            [self = shared_from_this()](Error error, std::size_t) {
                if (self->finished_ || self->reply_started_ || self->upload_stopped_) return;
                ++self->upload_read_generation_;
                self->upload_read_deadline_.cancel();
                if (error && error != http::error::need_buffer) return self->client_error(error);
                const auto count = self->upload_buffer_.size() - self->upload_parser_->get().body().size;
                if (count == 0) return self->read_upload_block();
                self->upload_chunk_.emplace(net::const_buffer(self->upload_buffer_.data(), count));
                net::async_write(self->upstream_, *self->upload_chunk_,
                    [self, count](Error write_error, std::size_t) {
                        if (self->finished_ || self->reply_started_ || self->upload_stopped_) return;
                        self->upload_chunk_.reset();
                        if (write_error) return self->upstream_error(write_error);
                        self->request_bytes_ += count;
                        // Upstream has consumed this buffer before it is filled again.
                        self->read_upload_block();
                    });
            });
    }

    void start_stream() {
        stream_header_.base() = response_parser_.get().base();
        remove_hop_headers(stream_header_.base());
        stream_header_.version(11);
        stream_header_.keep_alive(false);
        stream_header_.chunked(true);
        stream_header_.set(http::field::server, "ai-gateway/0.7");
        stream_header_.set("X-Request-ID", request_id_);
        if (stream_header_[http::field::cache_control].empty())
            stream_header_.set(http::field::cache_control, "no-cache");
        response_.result(stream_header_.result());
        stream_parser_ = std::make_unique<http::response_parser<http::buffer_body>>(std::move(response_parser_));
        stream_serializer_ = std::make_unique<http::response_serializer<http::empty_body>>(stream_header_);
        reply_started_ = true;
        stream_observed_ = true;
        downstream_.expires_after(std::chrono::milliseconds(config_.stream_write_timeout_ms));
        http::async_write_header(downstream_, *stream_serializer_,
            [self = shared_from_this()](Error error, std::size_t) {
                if (self->finished_) return;
                if (error) return self->finish("stream_write_error");
                self->stream_read_deadline_ = Clock::now() + std::chrono::milliseconds(self->config_.stream_first_byte_timeout_ms);
                self->read_stream_block();
            });
    }

    void read_stream_block() {
        if (finished_) return;
        if (stream_parser_->is_done()) {
            downstream_.expires_after(std::chrono::milliseconds(config_.stream_write_timeout_ms));
            return net::async_write(downstream_, last_chunk_,
                [self = shared_from_this()](Error error, std::size_t) {
                    self->finish(error ? "stream_write_error" : "stream_complete");
                });
        }
        auto& body = stream_parser_->get().body();
        body.data = stream_buffer_.data();
        body.size = stream_buffer_.size();
        upstream_.expires_at(stream_read_deadline_);
        http::async_read_some(upstream_, upstream_buffer_, *stream_parser_,
            [self = shared_from_this()](Error error, std::size_t) {
                if (self->finished_) return;
                if (error && error != http::error::need_buffer) return self->upstream_error(error);
                const auto count = self->stream_buffer_.size() - self->stream_parser_->get().body().size;
                if (count == 0) return self->read_stream_block();
                self->stream_observer_.feed({self->stream_buffer_.data(), count});
                self->stream_chunk_.emplace(net::const_buffer(self->stream_buffer_.data(), count));
                self->downstream_.expires_after(std::chrono::milliseconds(self->config_.stream_write_timeout_ms));
                net::async_write(self->downstream_, *self->stream_chunk_,
                    [self, count](Error write_error, std::size_t) {
                        if (self->finished_) return;
                        self->stream_chunk_.reset();
                        if (write_error) return self->finish("stream_write_error");
                        self->response_bytes_ += count;
                        self->stream_read_deadline_ = Clock::now() + std::chrono::milliseconds(self->config_.stream_idle_timeout_ms);
                        // Read only after the downstream write completes: bounded backpressure.
                        self->read_stream_block();
                    });
            });
    }

    void upstream_error(Error error) {
        const bool timeout = error == beast::error::timeout || error == net::error::timed_out;
        if (reply_started_) return finish(timeout ? "stream_read_timeout" : "upstream_stream_error");
        error_reply(readiness_ ? http::status::service_unavailable :
                    (timeout ? http::status::gateway_timeout : http::status::bad_gateway),
                    timeout ? "GATEWAY_TIMEOUT" : "UPSTREAM_ERROR",
                    "Upstream request did not complete", timeout ? "timeout" : "upstream_error");
    }

    void error_reply(http::status status, const char* code, const char* message, const char* reason, int retry_after = 0) {
        json_reply(status, std::string("{\"error\":{\"code\":\"") + code + "\",\"message\":\"" + message +
            "\"},\"request_id\":\"" + request_id_ + "\"}", reason, retry_after);
    }

    void json_reply(http::status status, std::string body, const char* reason, int retry_after = 0) {
        if (reply_started_ || finished_) return;
        response_ = {status, 11};
        if (retry_after > 0) response_.set(http::field::retry_after, std::to_string(retry_after));
        response_.set(http::field::content_type, "application/json; charset=utf-8");
        if (route_ == "/metrics" || agent_metrics_) response_.set(http::field::cache_control, "no-store");
        response_.body() = std::move(body);
        respond(reason);
    }

    void cancel_upstream() {
        cancel_queue();
        watching_model_client_ = false;
        client_cancellation_.emit(net::cancellation_type::terminal);
        if (redis_check_) {
            auto check = std::move(redis_check_);
            check->cancel();
        }
        Error ignored;
        resolver_.cancel();
        connect_deadline_.cancel();
        upload_read_deadline_.cancel();
        upstream_.socket().cancel(ignored);
        upstream_.socket().close(ignored);
    }

    void respond(const char* reason) {
        if (reply_started_ || finished_) return;
        reply_started_ = true;
        reason_ = reason;
        deadline_.cancel();
        cancel_upstream();
        response_.version(11);
        response_.set(http::field::server, "ai-gateway/0.7");
        if (agent_metrics_) response_.set(http::field::cache_control, "no-store");
        response_.set("X-Request-ID", request_id_);
        response_.keep_alive(false);
        response_.prepare_payload();
        response_bytes_ = response_.body().size();
        downstream_.expires_after(std::chrono::milliseconds(config_.header_timeout_ms));
        http::async_write(downstream_, response_,
            [self = shared_from_this()](Error error, std::size_t) {
                self->finish(error ? "downstream_write_error" : self->reason_);
            });
    }

    void finish(std::string reason) {
        if (finished_) return;
        finished_ = true;
        deadline_.cancel();
        cancel_upstream();
        Error ignored;
        downstream_.socket().cancel(ignored);
        downstream_.socket().shutdown(tcp::socket::shutdown_both, ignored);
        downstream_.socket().close(ignored);
        permit_.reset();
        const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now() - started_).count();
        metrics_->connection_closed();
        if (!local_probe_)
            metrics_->record(reply_started_ ? response_.result_int() : 0, reason, model_counted_,
                business_failure_ || stream_observer_.failed(),
                business_inspection_unavailable_ || stream_observer_.unavailable(),
                stream_observed_ && !stream_observer_.done(), request_bytes_, response_bytes_,
                static_cast<std::uint64_t>(elapsed));
        // Only program-selected route templates and reasons enter logs, never targets or bodies.
        std::cout << "{\"event\":\"request\",\"request_id\":\"" << request_id_
                  << "\",\"route\":\"" << route_ << "\",\"method\":\"" << method_
                  << "\",\"status\":" << (reply_started_ ? response_.result_int() : 0)
                  << ",\"duration_ms\":" << elapsed << ",\"request_bytes\":" << request_bytes_
                  << ",\"response_bytes\":" << response_bytes_ << ",\"admission_wait_ms\":" << admission_wait_ms_
                  << ",\"reason\":\"" << reason << "\"}" << std::endl;
        release_(this);
    }
};

class Listener {
public:
    Listener(net::io_context& context, const Config& config)
        : acceptor_(context), config_(config),
          admission_(std::make_shared<AdmissionController>(context.get_executor(), config.admission)),
          metrics_(std::make_shared<Metrics>()) {
        const tcp::endpoint endpoint(net::ip::make_address(config.listen_address), config.listen_port);
        acceptor_.open(endpoint.protocol());
        acceptor_.set_option(net::socket_base::reuse_address(true));
        acceptor_.bind(endpoint);
        acceptor_.listen();
    }

    void start() { accept(); }
    void stop() {
        Error ignored;
        acceptor_.close(ignored);
        admission_->stop();
        std::vector<std::shared_ptr<ProxySession>> active;
        for (const auto& [_, session] : sessions_) active.push_back(session);
        for (const auto& session : active) session->stop();
    }

private:
    tcp::acceptor acceptor_;
    const Config& config_;
    std::shared_ptr<AdmissionController> admission_;
    std::shared_ptr<Metrics> metrics_;
    std::unordered_map<ProxySession*, std::shared_ptr<ProxySession>> sessions_;

    void accept() {
        acceptor_.async_accept([this](Error error, tcp::socket socket) {
            if (!error) metrics_->accepted(sessions_.size() < config_.max_connections);
            if (!error && sessions_.size() < config_.max_connections) {
                auto session = std::make_shared<ProxySession>(std::move(socket), config_, admission_, metrics_,
                    [this](ProxySession* released) { sessions_.erase(released); });
                sessions_.emplace(session.get(), session);
                session->start();
            }
            if (acceptor_.is_open()) accept();
        });
    }
};

} // namespace

void run(const Config& config) {
    net::io_context context{1};
    Listener listener(context, config);
    net::signal_set signals(context, SIGINT, SIGTERM);
    signals.async_wait([&listener](Error error, int) { if (!error) listener.stop(); });
    listener.start();
    std::cout << "{\"event\":\"started\",\"milestone\":\"M2.3.3\"}" << std::endl;
    context.run();
}

} // namespace gateway
