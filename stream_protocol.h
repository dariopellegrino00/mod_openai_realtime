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
// Checks token syntax only; a JSON parser must still validate the document structure.
bool json_tokens_are_valid(const char *json);
bool validate_ws_uri(const char *url, char *destination, std::size_t destination_size);
bool is_valid_utf8(const char *text);

} // namespace stream_protocol

#endif // STREAM_PROTOCOL_H
