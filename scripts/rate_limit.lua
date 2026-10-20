-- One key / one fixed window starting with its first accepted request.
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local stored = redis.call('GET', KEYS[1])
local count = 0
if stored then
    count = tonumber(stored)
    if not count or count < 0 or count ~= math.floor(count) then
        return redis.error_reply('INVALID_LIMIT_STATE')
    end
end
local ttl = redis.call('PTTL', KEYS[1])
-- Repair orphan counters, including rejected counters without an expiry.
if count > 0 and ttl < 0 then
    redis.call('PEXPIRE', KEYS[1], window)
    ttl = window
end
if count >= limit then
    return {0, 0, math.max(ttl, 1)}
end
count = redis.call('INCR', KEYS[1])
if count == 1 or ttl < 0 then
    redis.call('PEXPIRE', KEYS[1], window)
    ttl = window
end
return {1, limit - count, math.max(ttl, 1)}
