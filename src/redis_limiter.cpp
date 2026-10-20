#include "redis_limiter.hpp"
#include "rate_limit_script.hpp"

// Boost.Redis is compiled here only; all remaining users see the small header.
#include <boost/redis/src.hpp>
#include <boost/redis/connection.hpp>
#include <boost/redis/response.hpp>
#include <boost/asio/steady_timer.hpp>
#include <chrono>
#include <array>
#include <tuple>

namespace gateway {
namespace net = boost::asio;
namespace redis = boost::redis;
using Error = boost::system::error_code;

struct RedisCheck::State {
    redis::connection connection;
    net::steady_timer timer;
    redis::request request;
    // This Boost.Redis adapter specializes long long, not Linux's int64_t=long.
    redis::response<std::array<long long, 3>> result;
    redis::response<std::string> pong;
    Callback callback;
    bool done = false;

    State(net::any_io_executor executor, Callback completion)
        : connection(executor, net::ssl::context{net::ssl::context::tlsv12_client}, 64 * 1024),
          timer(executor), callback(std::move(completion)) {}
};

RedisCheck::RedisCheck(net::any_io_executor executor, Callback callback)
    : state_(std::make_unique<State>(executor, std::move(callback))) {}
RedisCheck::~RedisCheck() = default;

std::shared_ptr<RedisCheck> RedisCheck::start(net::any_io_executor executor,
    const RedisSettings& settings, RequestClass category, bool probe, Callback callback) {
    auto check = std::shared_ptr<RedisCheck>(new RedisCheck(executor, std::move(callback)));
    check->run(settings, category, probe);
    return check;
}

void RedisCheck::run(const RedisSettings& settings, RequestClass category, bool probe) {
    redis::config config;
    config.addr.host = settings.host;
    config.addr.port = settings.port;
    config.clientname = "ai-gateway";
    // An unanswered EVAL may already have incremented Redis. Never reconnect
    // and replay it; report 503 rather than charge the same arrival twice.
    config.reconnect_wait_interval = std::chrono::milliseconds(0);
    state_->request.get_config().cancel_on_connection_lost = true;
    state_->request.get_config().cancel_if_unresponded = true;
    state_->request.get_config().cancel_if_not_connected = false;
    if (probe) {
        state_->request.push("PING");
    } else {
        const char* name = category == RequestClass::upload ? "upload" :
            category == RequestClass::model ? "model" : "normal";
        const int limit = category == RequestClass::upload ? settings.upload_limit :
            category == RequestClass::model ? settings.model_limit : settings.normal_limit;
        state_->request.push("EVAL", rate_limit_script, 1,
            settings.prefix + ":" + settings.key_id + ":" + name, limit, settings.window_ms);
    }
    state_->timer.expires_after(std::chrono::milliseconds(settings.timeout_ms));
    state_->timer.async_wait([self = shared_from_this()](Error error) {
        if (!error) self->complete({});
    });
    state_->connection.async_run(config, redis::logger{redis::logger::level::disabled},
        [self = shared_from_this()](Error) { self->complete({}); });
    if (probe) {
        state_->connection.async_exec(state_->request, state_->pong,
            [self = shared_from_this()](Error error, std::size_t) {
                if (self->state_->done) return;
                auto& result = std::get<0>(self->state_->pong);
                self->complete(!error && !result.has_error() && result.value() == "PONG" ?
                    LimitDecision{LimitStatus::allowed} : LimitDecision{});
            });
    } else {
        state_->connection.async_exec(state_->request, state_->result,
            [self = shared_from_this()](Error error, std::size_t) {
                if (self->state_->done) return;
                auto& result = std::get<0>(self->state_->result);
                if (error || result.has_error()) return self->complete({});
                const auto& values = result.value();
                if (values.size() != 3 || (values[0] != 0 && values[0] != 1) ||
                    values[1] < 0 || values[1] > 1000000 || values[2] < 1 || values[2] > 3600000)
                    return self->complete({});
                self->complete({values[0] == 1 ? LimitStatus::allowed : LimitStatus::denied, values[1], values[2]});
            });
    }
}

void RedisCheck::complete(LimitDecision decision) {
    if (state_->done) return;
    state_->done = true;
    state_->timer.cancel();
    state_->connection.cancel();
    auto callback = std::move(state_->callback);
    callback(decision);
}

void RedisCheck::cancel() { complete({}); }

} // namespace gateway
