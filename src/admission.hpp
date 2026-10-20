#pragma once

#include <boost/asio.hpp>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <list>
#include <memory>

namespace gateway {

struct AdmissionSettings {
    bool enabled = false;
    std::size_t max_active = 2;
    std::size_t max_queued = 8;
    std::size_t max_queue_bytes = 4 * 1024 * 1024;
    int queue_timeout_ms = 2000;
};

class AdmissionController;

// One owner, one release. All controller calls run on the gateway's one io_context thread.
class Permit {
public:
    Permit() = default;
    Permit(const Permit&) = delete;
    Permit& operator=(const Permit&) = delete;
    Permit(Permit&& other) noexcept;
    Permit& operator=(Permit&& other) noexcept;
    ~Permit();
    void reset();
private:
    friend class AdmissionController;
    explicit Permit(std::shared_ptr<AdmissionController> owner) : owner_(std::move(owner)) {}
    std::shared_ptr<AdmissionController> owner_;
};

enum class AdmissionStatus { granted, full, timed_out, stopped };

class AdmissionController : public std::enable_shared_from_this<AdmissionController> {
public:
    using Callback = std::function<void(AdmissionStatus, Permit)>;
    using Ticket = std::uint64_t;
    AdmissionController(boost::asio::any_io_executor executor, AdmissionSettings settings);
    Ticket acquire(std::size_t bytes, Callback callback);
    void cancel(Ticket ticket);
    void stop();
    std::size_t active() const { return active_; }
    std::size_t queued() const { return queue_.size(); }
    std::size_t queued_bytes() const { return queued_bytes_; }
private:
    friend class Permit;
    struct Waiter;
    boost::asio::any_io_executor executor_;
    AdmissionSettings settings_;
    std::list<std::shared_ptr<Waiter>> queue_;
    std::size_t active_ = 0;
    std::size_t queued_bytes_ = 0;
    Ticket next_ticket_ = 0;
    bool stopped_ = false;
    void release();
    void expire(Ticket ticket);
    void drain();
    void deliver(Callback callback, AdmissionStatus status, Permit permit = {});
};

} // namespace gateway
