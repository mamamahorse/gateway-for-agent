#include "admission.hpp"

#include <algorithm>
#include <chrono>
#include <utility>

namespace gateway {
namespace net = boost::asio;

Permit::Permit(Permit&& other) noexcept : owner_(std::move(other.owner_)) {}
Permit& Permit::operator=(Permit&& other) noexcept {
    if (this != &other) {
        reset();
        owner_ = std::move(other.owner_);
    }
    return *this;
}
Permit::~Permit() { reset(); }
void Permit::reset() {
    if (auto owner = std::move(owner_)) owner->release();
}

struct AdmissionController::Waiter {
    Ticket ticket;
    std::size_t bytes;
    net::steady_timer deadline;
    Callback callback;
    Waiter(net::any_io_executor executor, Ticket id, std::size_t size, Callback cb)
        : ticket(id), bytes(size), deadline(executor), callback(std::move(cb)) {}
};

AdmissionController::AdmissionController(net::any_io_executor executor, AdmissionSettings settings)
    : executor_(std::move(executor)), settings_(settings) {}

void AdmissionController::deliver(Callback callback, AdmissionStatus status, Permit permit) {
    // Never re-enter a session while acquire/cancel/drain is modifying the queue.
    net::post(executor_, [callback = std::move(callback), status, permit = std::move(permit)]() mutable {
        callback(status, std::move(permit));
    });
}

AdmissionController::Ticket AdmissionController::acquire(std::size_t bytes, Callback callback) {
    if (stopped_) {
        deliver(std::move(callback), AdmissionStatus::stopped);
    } else if (active_ < settings_.max_active && queue_.empty()) {
        ++active_;
        deliver(std::move(callback), AdmissionStatus::granted, Permit(shared_from_this()));
    } else if (queue_.size() >= settings_.max_queued || bytes > settings_.max_queue_bytes - queued_bytes_) {
        deliver(std::move(callback), AdmissionStatus::full);
    } else {
        const auto ticket = ++next_ticket_;
        auto waiter = std::make_shared<Waiter>(executor_, ticket, bytes, std::move(callback));
        waiter->deadline.expires_after(std::chrono::milliseconds(settings_.queue_timeout_ms));
        waiter->deadline.async_wait([weak = weak_from_this(), ticket](boost::system::error_code error) {
            if (!error) if (auto owner = weak.lock()) owner->expire(ticket);
        });
        queue_.push_back(waiter);
        queued_bytes_ += bytes;
        return ticket;
    }
    return 0;
}

void AdmissionController::cancel(Ticket ticket) {
    const auto found = std::find_if(queue_.begin(), queue_.end(),
        [ticket](const auto& waiter) { return waiter->ticket == ticket; });
    if (found == queue_.end()) return;
    auto waiter = *found;
    queue_.erase(found);
    queued_bytes_ -= waiter->bytes;
    waiter->deadline.cancel();
}

void AdmissionController::expire(Ticket ticket) {
    const auto found = std::find_if(queue_.begin(), queue_.end(),
        [ticket](const auto& waiter) { return waiter->ticket == ticket; });
    if (found == queue_.end()) return;
    auto waiter = *found;
    cancel(ticket);
    deliver(std::move(waiter->callback), AdmissionStatus::timed_out);
}

void AdmissionController::release() {
    --active_;
    drain();
}

void AdmissionController::drain() {
    while (!stopped_ && active_ < settings_.max_active && !queue_.empty()) {
        auto waiter = queue_.front();
        const bool expired = waiter->deadline.expiry() <= net::steady_timer::clock_type::now();
        cancel(waiter->ticket);
        if (expired) {
            deliver(std::move(waiter->callback), AdmissionStatus::timed_out);
        } else {
            ++active_;
            deliver(std::move(waiter->callback), AdmissionStatus::granted, Permit(shared_from_this()));
        }
    }
}

void AdmissionController::stop() {
    stopped_ = true;
    while (!queue_.empty()) {
        auto waiter = queue_.front();
        cancel(waiter->ticket);
        deliver(std::move(waiter->callback), AdmissionStatus::stopped);
    }
}

} // namespace gateway
