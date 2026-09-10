#ifndef MOD_OPENAI_AUDIO_STREAM_H
#define MOD_OPENAI_AUDIO_STREAM_H

#include <stdint.h>

#include <speex/speex_resampler.h>
#include <switch.h>

#define MY_BUG_NAME "audio_stream"
#define STREAM_PLAYBACK_OWNER "audio_stream_playback_owner"
#define STREAM_DEFAULT_NAME "default"
#define MAX_STREAM_NAME (65)
#define MAX_BUG_NAME (sizeof(MY_BUG_NAME) + MAX_STREAM_NAME)
#define MAX_SESSION_ID (256)
#define MAX_WS_URI (4096)
#define STREAM_MIN_SAMPLING (8000)
#define STREAM_MAX_SAMPLING (48000)

#define EVENT_CONNECT "mod_openai_audio_stream::connect"
#define EVENT_DISCONNECT "mod_openai_audio_stream::disconnect"
#define EVENT_ERROR "mod_openai_audio_stream::error"
#define EVENT_JSON "mod_openai_audio_stream::json"
#define EVENT_PLAY "mod_openai_audio_stream::play"
#define EVENT_OPENAI_SPEECH_STARTED "mod_openai_audio_stream::openai_speech_start"
#define EVENT_OPENAI_SPEECH_STOPPED "mod_openai_audio_stream::openai_speech_stop"

typedef void (*responseHandler_t)(switch_core_session_t *session, const char *stream_name, const char *eventName,
                                  const char *json);

typedef struct {
    const char *name;
    const char *bug_name;
    const char *ws_uri;
    int send_rate;
    int playback_rate;
    int channels;
    switch_bool_t send_audio;
    switch_bool_t receive_audio;
    switch_bool_t start_muted;
    switch_bool_t raw_audio;
} stream_start_options_t;

struct private_data {
    switch_media_bug_t *bug; // Protected by mutex; NULL after CLOSE. Context lives in the session pool.
    switch_mutex_t *mutex;
    char sessionId[MAX_SESSION_ID];
    char bug_name[MAX_BUG_NAME];
    switch_bool_t send_audio;
    switch_bool_t receive_audio;
    SpeexResamplerState *resampler;
    void *cpp_context;
    uint32_t sampling;
    int channels;
    switch_bool_t suppress_log;
    switch_atomic_t audio_paused;
    switch_atomic_t user_audio_muted;
    switch_atomic_t openai_audio_muted;
    switch_atomic_t close_requested;
    switch_buffer_t *sbuffer;
    int rtp_packets;
    switch_buffer_t *playback_buffer;
};

typedef struct private_data private_t;

enum notifyEvent_t { CONNECT_SUCCESS, CONNECT_ERROR, CONNECTION_DROPPED, MESSAGE };

#endif // MOD_OPENAI_AUDIO_STREAM_H
