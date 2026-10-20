#include "policy.hpp"

#include <algorithm>
#include <array>
#include <cctype>
#include <string>
#include <vector>

namespace gateway {
namespace http = boost::beast::http;
namespace {

bool matches(std::string_view pattern, std::string_view path) {
    while (!pattern.empty() && !path.empty()) {
        const auto pattern_end = pattern.find('/');
        const auto path_end = path.find('/');
        const auto p = pattern.substr(0, pattern_end);
        const auto actual = path.substr(0, path_end);
        if (p == ":id") {
            if (actual.empty() || !std::all_of(actual.begin(), actual.end(), [](unsigned char ch) {
                    return std::isalnum(ch) != 0 || ch == '-' || ch == '_';
                })) return false;
        } else if (p != actual) {
            return false;
        }
        if (pattern_end == std::string_view::npos || path_end == std::string_view::npos)
            return pattern_end == path_end;
        pattern.remove_prefix(pattern_end + 1);
        path.remove_prefix(path_end + 1);
    }
    return pattern.empty() && path.empty();
}

std::string lower(std::string_view text) {
    std::string result(text);
    std::transform(result.begin(), result.end(), result.begin(), [](unsigned char c) {
        return static_cast<char>(std::tolower(c));
    });
    return result;
}

} // namespace

std::optional<Route> route_for(http::verb method, std::string_view path) {
    struct Entry {
        http::verb method;
        std::string_view path;
        bool model;
        bool stream = false;
        bool upload = false;
    };
    static constexpr std::array entries{
        Entry{http::verb::get, "/api/documents", false},
        Entry{http::verb::post, "/api/documents", false, false, true},
        Entry{http::verb::get, "/api/documents/:id", false},
        Entry{http::verb::get, "/api/collections", false},
        Entry{http::verb::post, "/api/collections", false},
        Entry{http::verb::get, "/api/collections/:id/outlines", false},
        Entry{http::verb::post, "/api/collections/:id/outlines", true},
        Entry{http::verb::get, "/api/outlines/:id", false},
        Entry{http::verb::post, "/api/outlines/:id/resume", true},
        Entry{http::verb::post, "/api/outlines/:id/pause", false},
        Entry{http::verb::post, "/api/outlines/:id/grouping", true},
        Entry{http::verb::post, "/api/outline-group-runs/:id/retry", true},
        Entry{http::verb::post, "/api/outline-group-runs/:id/pause", false},
        Entry{http::verb::post, "/api/outline-groups/:id/source-reviews", true},
        Entry{http::verb::patch, "/api/outline-groups/:id", false},
        Entry{http::verb::post, "/api/outline-source-reviews/:id/continue", true},
        Entry{http::verb::post, "/api/outline-source-reviews/:id/pause", false},
        Entry{http::verb::post, "/api/outlines/:id/routes", true},
        Entry{http::verb::post, "/api/outline-routes/:id/continue", true},
        Entry{http::verb::post, "/api/outline-routes/:id/pause", false},
        Entry{http::verb::post, "/api/ask", true},
        Entry{http::verb::get, "/api/conversations", false},
        Entry{http::verb::post, "/api/conversations", false},
        Entry{http::verb::get, "/api/conversations/:id", false},
        Entry{http::verb::get, "/api/conversations/:id/messages", false},
        Entry{http::verb::post, "/api/conversations/:id/turns", true},
        Entry{http::verb::post, "/api/conversations/:id/turns/stream", true, true},
        Entry{http::verb::post, "/api/conversations/:id/turns/:id/retry", true},
        Entry{http::verb::post, "/api/conversations/:id/turns/:id/skip", false},
        Entry{http::verb::get, "/api/practice", false},
        Entry{http::verb::post, "/api/practice", true},
        Entry{http::verb::get, "/api/practice/:id", false},
        Entry{http::verb::post, "/api/practice/:id/answers", true},
        Entry{http::verb::post, "/api/practice/:id/grade", true},
        Entry{http::verb::post, "/api/practice/:id/enroll", false},
        Entry{http::verb::get, "/api/learning", false},
        Entry{http::verb::post, "/api/learning/:id/review", true},
    };
    for (const auto& entry : entries)
        if (method == entry.method && matches(entry.path, path))
            return Route{entry.path, entry.model, entry.stream, entry.upload};
    return std::nullopt;
}

bool key_matches(std::string_view supplied, std::string_view expected) {
    if (supplied.size() != expected.size()) return false;
    unsigned int difference = 0;
    for (std::size_t i = 0; i < expected.size(); ++i)
        difference |= static_cast<unsigned char>(supplied[i]) ^ static_cast<unsigned char>(expected[i]);
    return difference == 0;
}

void remove_hop_headers(http::fields& fields) {
    // Connection can nominate additional hop-by-hop headers; collect before erasing.
    std::vector<std::string> nominated;
    for (const auto& field : fields) {
        if (field.name() != http::field::connection) continue;
        std::string_view value(field.value().data(), field.value().size());
        while (!value.empty()) {
            const auto comma = value.find(',');
            auto token = value.substr(0, comma);
            while (!token.empty() && (token.front() == ' ' || token.front() == '\t')) token.remove_prefix(1);
            while (!token.empty() && (token.back() == ' ' || token.back() == '\t')) token.remove_suffix(1);
            if (!token.empty()) nominated.emplace_back(token);
            if (comma == std::string_view::npos) break;
            value.remove_prefix(comma + 1);
        }
    }
    for (const auto& name : nominated) fields.erase(name);
    for (const auto* name : {"Connection", "Keep-Alive", "Proxy-Authenticate", "Proxy-Authorization",
                             "TE", "Trailer", "Transfer-Encoding", "Upgrade", "Content-Length"})
        fields.erase(name);
    // Never trust client-supplied forwarding/identity metadata as application authority.
    std::vector<std::string> identity;
    for (const auto& field : fields) {
        const auto name = lower({field.name_string().data(), field.name_string().size()});
        if (name == "x-api-key" || name == "authorization" || name == "forwarded" ||
            name.starts_with("x-forwarded-") || name.starts_with("x-user-"))
            identity.push_back(name);
    }
    for (const auto& name : identity) fields.erase(name);
}

} // namespace gateway
