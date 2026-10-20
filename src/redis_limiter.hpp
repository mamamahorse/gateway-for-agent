#pragma once

#include <boost/asio/any_io_executor.hpp>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>

namespace gateway {

struct RedisSettings {
    bool enabled = false;
    std::string host = "127.0.0.1";
    std::string port = "6379";
    std::string prefix = "gw:rl";
    std::string key_id = "default"; // Public identity, never the secret API key.
    int timeout_ms = 100;
    int window_ms = 60000;
    int normal_limit = 120;
    int model_limit = 10;
    int upload_limit = 5;
};

enum class RequestClass { normal, model, upload };
enum class LimitStatus { allowed, denied, unavailable };
struct LimitDecision {
    LimitStatus status = LimitStatus::unavailable;
    std::int64_t remaining = 0;
    std::int64_t retry_ms = 0;
};

// One short-lived asynchronous Redis channel per check. The existing downstream
// connection bound also bounds these operations; no pool or hidden wait queue.
class RedisCheck : public std::enable_shared_from_this<RedisCheck> {
public:
    using Callback = std::function<void(LimitDecision)>;
    static std::shared_ptr<RedisCheck> start(boost::asio::any_io_executor executor,
        const RedisSettings& settings, RequestClass category, bool probe, Callback callback);
    ~RedisCheck();
    void cancel();

private:
    struct State;
    std::unique_ptr<State> state_;
    RedisCheck(boost::asio::any_io_executor executor, Callback callback);
    void run(const RedisSettings& settings, RequestClass category, bool probe);
    void complete(LimitDecision decision);
};

} // namespace gateway
