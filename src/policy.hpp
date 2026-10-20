#pragma once

#include <boost/beast/http.hpp>
#include <optional>
#include <string_view>

namespace gateway {

struct Route {
    std::string_view pattern;
    bool model_request;
    bool stream_request = false;
    bool upload_request = false;
};

std::optional<Route> route_for(boost::beast::http::verb method, std::string_view path);
bool key_matches(std::string_view supplied, std::string_view expected);
void remove_hop_headers(boost::beast::http::fields& fields);

} // namespace gateway
