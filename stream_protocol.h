#ifndef STREAM_PROTOCOL_H
#define STREAM_PROTOCOL_H

#include <cstddef>

namespace stream_protocol {

enum class JsonMessageType {
    Error,
    SpeechStarted,
    SpeechStopped,
    AudioDelta,
    AudioDone,
    Unhandled,
};

JsonMessageType classify_json_message(const char *type);
bool json_depth_exceeded(const char *json, int max_depth);
bool validate_ws_uri(const char *url, char *destination, std::size_t destination_size);
bool is_valid_utf8(const char *text);

} // namespace stream_protocol

#endif // STREAM_PROTOCOL_H
