/*
 * OpenAI mod_openai_audio_stream FreeSWITCH module to stream audio to WebSocket and receive responses from OpenAI
 * Realtime API.
 */
#include "mod_openai_audio_stream.h"
#include "openai_audio_streamer_glue.h"
#include <strings.h>

SWITCH_MODULE_SHUTDOWN_FUNCTION(mod_openai_audio_stream_shutdown);
SWITCH_MODULE_LOAD_FUNCTION(mod_openai_audio_stream_load);

SWITCH_MODULE_DEFINITION(mod_openai_audio_stream, mod_openai_audio_stream_load, mod_openai_audio_stream_shutdown, NULL);

static switch_thread_rwlock_t *module_rwlock;

/* freeing a subclass that was not reserved by this module is a harmless no-op */
static void free_event_subclasses(void) {
    switch_event_free_subclass(EVENT_JSON);
    switch_event_free_subclass(EVENT_CONNECT);
    switch_event_free_subclass(EVENT_DISCONNECT);
    switch_event_free_subclass(EVENT_ERROR);
    switch_event_free_subclass(EVENT_PLAY);
    switch_event_free_subclass(EVENT_OPENAI_SPEECH_STARTED);
    switch_event_free_subclass(EVENT_OPENAI_SPEECH_STOPPED);
}

static switch_bool_t suppress_sensitive_logs(switch_core_session_t *session, const char *bug_name) {
    if (!session) {
        return SWITCH_FALSE;
    }

    switch_channel_t *channel = switch_core_session_get_channel(session);
    private_t *tech_pvt = channel ? switch_channel_get_private(channel, bug_name) : NULL;

    if (tech_pvt) {
        return tech_pvt->suppress_log;
    }
    return channel ? switch_channel_var_true(channel, "STREAM_SUPPRESS_LOG") : SWITCH_FALSE;
}

/* The responseHandler_t callback signature fixes the order of these string parameters. */
/* NOLINTNEXTLINE(bugprone-easily-swappable-parameters) */
static void responseHandler(switch_core_session_t *session, const char *stream_name, const char *eventName,
                            const char *json) {
    switch_event_t *event;
    switch_channel_t *channel = switch_core_session_get_channel(session);
    if (switch_event_create_subclass(&event, SWITCH_EVENT_CUSTOM, eventName) != SWITCH_STATUS_SUCCESS) {
        switch_log_printf(SWITCH_CHANNEL_SESSION_LOG(session), SWITCH_LOG_ERROR, "failed to create %s event\n",
                          eventName);
        return;
    }
    switch_channel_event_set_data(channel, event);
    switch_event_add_header_string(event, SWITCH_STACK_BOTTOM, "Stream-Name", stream_name);
    if (json)
        switch_event_add_body(event, "%s", json);
    switch_event_fire(&event);
}

static switch_bool_t capture_callback(switch_media_bug_t *bug, void *user_data, switch_abc_type_t type) {
    switch_core_session_t *session = switch_core_media_bug_get_session(bug);
    private_t *tech_pvt = (private_t *)user_data;

    switch (type) {
        case SWITCH_ABC_TYPE_INIT:
            /* Keep the module loaded through CLOSE, including the WebSocket thread join. */
            return switch_thread_rwlock_rdlock(module_rwlock) == SWITCH_STATUS_SUCCESS;

        case SWITCH_ABC_TYPE_CLOSE:
            stream_session_close(session, user_data);
            switch_thread_rwlock_unlock(module_rwlock);
            return SWITCH_TRUE;

        case SWITCH_ABC_TYPE_READ:
            if (switch_atomic_read(&tech_pvt->close_requested)) {
                return SWITCH_FALSE;
            }
            return stream_frame(bug);
        case SWITCH_ABC_TYPE_READ_PING:
            return switch_atomic_read(&tech_pvt->close_requested) ? SWITCH_FALSE : SWITCH_TRUE;
        case SWITCH_ABC_TYPE_WRITE_REPLACE:
            if (switch_atomic_read(&tech_pvt->close_requested)) {
                return SWITCH_FALSE;
            }
            write_frame(session, bug);
            break;

        case SWITCH_ABC_TYPE_WRITE:
        default:
            break;
    }

    return SWITCH_TRUE;
}

/* Strict parse: shortcuts or a plain decimal number, no suffixes. Returns -1 on invalid input. */
static int parse_sampling_rate(const char *str) {
    char *endptr = NULL;
    long value;

    if (0 == strcmp(str, "16k")) {
        return 16000;
    } else if (0 == strcmp(str, "8k")) {
        return 8000;
    } else if (0 == strcmp(str, "24k")) {
        return 24000;
    }

    value = strtol(str, &endptr, 10);
    if (endptr == str || *endptr != '\0' || value <= 0 || value > STREAM_MAX_SAMPLING) {
        return -1;
    }
    return (int)value;
}

static switch_status_t start_capture(switch_core_session_t *session, switch_media_bug_flag_t flags,
                                     const stream_start_options_t *options, stream_error_t *error) {
    switch_channel_t *channel = switch_core_session_get_channel(session);
    switch_media_bug_t *bug;
    switch_status_t status;
    switch_codec_t *read_codec;

    void *pUserData = NULL;
    if (switch_channel_get_private(channel, options->bug_name)) {
        switch_log_printf(SWITCH_CHANNEL_SESSION_LOG(session), SWITCH_LOG_ERROR,
                          "mod_openai_audio_stream: bug already attached!\n");
        return stream_fail(error, "Stream already exists");
    }

    private_t *playback_owner = switch_channel_get_private(channel, STREAM_PLAYBACK_OWNER);
    if (options->receive_audio && playback_owner) {
        switch_log_printf(SWITCH_CHANNEL_SESSION_LOG(session), SWITCH_LOG_ERROR,
                          "channel already has a stream with playback enabled\n");
        const char *owner_name = strcmp(playback_owner->bug_name, MY_BUG_NAME) == 0
                                     ? STREAM_DEFAULT_NAME
                                     : playback_owner->bug_name + sizeof(MY_BUG_NAME);
        /* The owner and its name remain valid in the session pool while CLOSE releases the slot. */
        switch_snprintf(error->message, sizeof(error->message), "Playback is already enabled by stream '%s'",
                        owner_name);
        return SWITCH_STATUS_FALSE;
    }

    if (switch_channel_pre_answer(channel) != SWITCH_STATUS_SUCCESS) {
        switch_log_printf(
            SWITCH_CHANNEL_SESSION_LOG(session), SWITCH_LOG_ERROR,
            "mod_openai_audio_stream: channel must have reached pre-answer status before calling start!\n");
        return stream_fail(error, "Channel could not be pre-answered");
    }

    read_codec = switch_core_session_get_read_codec(session);
    if (!read_codec || !read_codec->implementation) {
        switch_log_printf(SWITCH_CHANNEL_SESSION_LOG(session), SWITCH_LOG_ERROR,
                          "mod_openai_audio_stream: channel has no read codec ready.\n");
        return stream_fail(error, "Channel has no read codec ready");
    }

    if (SWITCH_STATUS_FALSE == stream_session_init(session, responseHandler,
                                                   read_codec->implementation->actual_samples_per_second, options,
                                                   &pUserData, error)) {
        switch_log_printf(SWITCH_CHANNEL_SESSION_LOG(session), SWITCH_LOG_ERROR,
                          "Error initializing mod_openai_audio_stream session.\n");
        if (!error || !error->message[0]) {
            stream_fail(error, "Failed to initialize stream");
        }
        return SWITCH_STATUS_FALSE;
    }
    private_t *tech_pvt = (private_t *)pUserData;
    /* CLOSE may run as soon as add publishes the bug. Keep its context alive until startup completes. */
    switch_mutex_lock(tech_pvt->mutex);
    if (switch_core_media_bug_add(session, options->bug_name, NULL, capture_callback, pUserData, 0, flags, &bug) !=
        SWITCH_STATUS_SUCCESS) {
        switch_mutex_unlock(tech_pvt->mutex);
        switch_log_printf(SWITCH_CHANNEL_SESSION_LOG(session), SWITCH_LOG_ERROR, "Error adding media bug.\n");
        stream_session_release(pUserData);
        return stream_fail(error, "Failed to attach stream to channel");
    }
    tech_pvt->bug = bug;
    switch_channel_set_private(channel, options->bug_name, tech_pvt);
    if (options->receive_audio) {
        switch_channel_set_private(channel, STREAM_PLAYBACK_OWNER, tech_pvt);
    }

    status = switch_channel_ready(channel) ? stream_session_start(pUserData) : SWITCH_STATUS_FALSE;
    switch_mutex_unlock(tech_pvt->mutex);
    if (status != SWITCH_STATUS_SUCCESS) {
        switch_log_printf(SWITCH_CHANNEL_SESSION_LOG(session), SWITCH_LOG_ERROR, "Error starting stream.\n");
        stream_session_cleanup(session, options->bug_name, NULL, NULL);
        return stream_fail(error, "Failed to start stream");
    }

    return SWITCH_STATUS_SUCCESS;
}

static switch_status_t do_stop(switch_core_session_t *session, const char *bug_name, char *json,
                               stream_error_t *error) {
    if (json) {
        if (suppress_sensitive_logs(session, bug_name)) {
            switch_log_printf(SWITCH_CHANNEL_SESSION_LOG(session), SWITCH_LOG_INFO,
                              "mod_openai_audio_stream: stop w/ final json (payload suppressed)\n");
        } else {
            switch_log_printf(SWITCH_CHANNEL_SESSION_LOG(session), SWITCH_LOG_INFO,
                              "mod_openai_audio_stream: stop w/ final json %s\n", json);
        }
    } else {
        switch_log_printf(SWITCH_CHANNEL_SESSION_LOG(session), SWITCH_LOG_INFO, "mod_openai_audio_stream: stop\n");
    }
    return stream_session_cleanup(session, bug_name, json, error);
}

static switch_status_t do_pauseresume(switch_core_session_t *session, const char *bug_name, int pause,
                                      stream_error_t *error) {
    switch_log_printf(SWITCH_CHANNEL_SESSION_LOG(session), SWITCH_LOG_INFO, "mod_openai_audio_stream: %s\n",
                      pause ? "pause" : "resume");
    return stream_session_pauseresume(session, bug_name, pause, error);
}

static switch_status_t do_audio_mute(switch_core_session_t *session, const char *bug_name, const char *target, int mute,
                                     stream_error_t *error) {
    switch_status_t status = SWITCH_STATUS_FALSE;

    switch_log_printf(SWITCH_CHANNEL_SESSION_LOG(session), SWITCH_LOG_INFO,
                      "mod_openai_audio_stream [%s]: %s %s audio\n", bug_name, mute ? "mute" : "unmute", target);

    if (!strcasecmp(target, "user")) {
        status = stream_session_set_user_mute(session, bug_name, mute, error);
    } else if (!strcasecmp(target, "openai")) {
        status = stream_session_set_openai_mute(session, bug_name, mute, error);
    } else if (!strcasecmp(target, "all") || !strcasecmp(target, "both")) {
        switch_channel_t *channel = switch_core_session_get_channel(session);
        private_t *tech_pvt = switch_channel_get_private(channel, bug_name);
        if (!tech_pvt) {
            return stream_fail(error, "Stream not found");
        }
        /* Capabilities are immutable and the context lives in the session pool. */
        switch_status_t user_status =
            tech_pvt->send_audio ? stream_session_set_user_mute(session, bug_name, mute, error) : SWITCH_STATUS_SUCCESS;
        switch_status_t openai_status = tech_pvt->receive_audio
                                            ? stream_session_set_openai_mute(session, bug_name, mute, error)
                                            : SWITCH_STATUS_SUCCESS;
        status = (user_status == SWITCH_STATUS_SUCCESS && openai_status == SWITCH_STATUS_SUCCESS)
                     ? SWITCH_STATUS_SUCCESS
                     : SWITCH_STATUS_FALSE;
    } else {
        switch_log_printf(SWITCH_CHANNEL_SESSION_LOG(session), SWITCH_LOG_ERROR,
                          "mod_openai_audio_stream: invalid mute target '%s', expected user|openai|all\n", target);
        status = stream_fail(error, "Invalid mute target; expected user, openai or all");
    }

    return status;
}

static switch_status_t send_json(switch_core_session_t *session, const char *bug_name, char *json,
                                 stream_error_t *error) {
    switch_status_t status = SWITCH_STATUS_FALSE;
    switch_channel_t *channel = switch_core_session_get_channel(session);
    if (switch_channel_get_private(channel, bug_name)) {
        status = stream_session_send_json(session, bug_name, json, error);
    } else {
        switch_log_printf(SWITCH_CHANNEL_SESSION_LOG(session), SWITCH_LOG_ERROR,
                          "mod_openai_audio_stream: no bug, failed sending json\n");
        status = stream_fail(error, "Stream not found");
    }
    return status;
}

#define STREAM_API_SYNTAX_BODY(api_name)                                                                               \
    "USAGE (stream defaults to 'default', direction defaults to both):\n" api_name                                     \
    " <uuid> start <ws-uri> <mono|mixed|stereo> [send_rate] [playback_rate] [mute_user] [both] "                       \
    "[stream=<name>]\n" api_name                                                                                       \
    " <uuid> start <ws-uri> <mono|mixed|stereo> [send_rate] [mute_user] send [stream=<name>]\n" api_name               \
    " <uuid> start <ws-uri> recv [playback_rate] [stream=<name>]\n"                                                    \
    "  Rates: 8k|16k|24k or a decimal multiple of 8000 up to 48000; default 24k.\n"                                    \
    "  Names: 1-64 lowercase letters, digits, '_' or '-'. Only one recv/both stream per channel.\n" api_name           \
    " <uuid> stop [base64json] [stream=<name>]\n" api_name " <uuid> <pause|resume> [stream=<name>]\n" api_name         \
    " <uuid> <mute|unmute> [user|openai|all] [stream=<name>]\n" api_name                                               \
    " <uuid> send_json <base64json> [stream=<name>]\n"

#define STREAM_API_SYNTAX STREAM_API_SYNTAX_BODY("uuid_openai_audio_stream")
#define RAW_STREAM_API_SYNTAX STREAM_API_SYNTAX_BODY("uuid_raw_audio_stream")

typedef struct {
    const char *api_name;
    const char *syntax;
    switch_bool_t force_raw_audio_mode;
} stream_api_config_t;

typedef enum {
    STREAM_CMD_UNKNOWN,
    STREAM_CMD_START,
    STREAM_CMD_STOP,
    STREAM_CMD_SEND_JSON,
    STREAM_CMD_PAUSE,
    STREAM_CMD_RESUME,
    STREAM_CMD_MUTE,
    STREAM_CMD_UNMUTE
} stream_command_t;

static stream_command_t stream_command_from_string(const char *name) {
    if (zstr(name)) {
        return STREAM_CMD_UNKNOWN;
    }
    if (!strcasecmp(name, "start")) {
        return STREAM_CMD_START;
    }
    if (!strcasecmp(name, "stop")) {
        return STREAM_CMD_STOP;
    }
    if (!strcasecmp(name, "send_json")) {
        return STREAM_CMD_SEND_JSON;
    }
    if (!strcasecmp(name, "pause")) {
        return STREAM_CMD_PAUSE;
    }
    if (!strcasecmp(name, "resume")) {
        return STREAM_CMD_RESUME;
    }
    if (!strcasecmp(name, "mute")) {
        return STREAM_CMD_MUTE;
    }
    if (!strcasecmp(name, "unmute")) {
        return STREAM_CMD_UNMUTE;
    }
    return STREAM_CMD_UNKNOWN;
}

/* Lowercase ASCII keeps channel keys and FreeSWITCH's case-insensitive bug lookup consistent. */
static switch_bool_t valid_stream_name(const char *name) {
    size_t length = strlen(name);
    if (!length || length >= MAX_STREAM_NAME) {
        return SWITCH_FALSE;
    }
    for (size_t i = 0; i < length; ++i) {
        char c = name[i];
        if (!(c >= 'a' && c <= 'z') && !(c >= '0' && c <= '9') && c != '_' && c != '-') {
            return SWITCH_FALSE;
        }
    }
    return SWITCH_TRUE;
}

static void remove_argument(char *argv[], unsigned int *argc, unsigned int index) {
    for (unsigned int i = index + 1; i < *argc; ++i) {
        argv[i - 1] = argv[i];
    }
    argv[--*argc] = NULL;
}

static switch_status_t start_stream(switch_core_session_t *session, unsigned int argc, char *argv[],
                                    stream_start_options_t options, stream_error_t *error) {
    switch_bool_t suppress_log = suppress_sensitive_logs(session, options.bug_name);
    options.send_audio = SWITCH_TRUE;
    options.receive_audio = SWITCH_TRUE;
    options.channels = 1;
    switch_bool_t has_direction = SWITCH_FALSE;
    for (unsigned int i = 3; i < argc;) {
        if (strcmp(argv[i], "send") != 0 && strcmp(argv[i], "recv") != 0 && strcmp(argv[i], "both") != 0) {
            ++i;
            continue;
        }
        if (has_direction) {
            return stream_fail(error, "Specify only one audio direction: send, recv or both");
        }
        has_direction = SWITCH_TRUE;
        options.send_audio = strcmp(argv[i], "recv") ? SWITCH_TRUE : SWITCH_FALSE;
        options.receive_audio = strcmp(argv[i], "send") ? SWITCH_TRUE : SWITCH_FALSE;
        remove_argument(argv, &argc, i);
    }
    if (argc < (options.send_audio ? 4U : 3U)) {
        switch_log_printf(SWITCH_CHANNEL_SESSION_LOG(session), SWITCH_LOG_ERROR, "start requires a websocket URI%s%s\n",
                          options.send_audio ? " and mix type" : "", suppress_log ? " (arguments suppressed)" : "");
        return stream_fail(error, options.send_audio
                                      ? "Start requires a WebSocket URI and mix type (mono, mixed or stereo)"
                                      : "Start requires a WebSocket URI");
    }
    char wsUri[MAX_WS_URI];
    int sampling = 24000;
    int playback_sampling = 24000;
    const char *sampling_str = NULL;
    const char *playback_sampling_str = NULL;
    switch_media_bug_flag_t flags = SMBF_ONE_ONLY;
    unsigned int next_index = 3;
    if (options.send_audio) {
        flags |= SMBF_READ_STREAM;
        if (strcmp(argv[next_index], "mixed") == 0) {
            flags |= SMBF_WRITE_STREAM;
        } else if (strcmp(argv[next_index], "stereo") == 0) {
            flags |= SMBF_WRITE_STREAM;
            flags |= SMBF_STEREO;
            options.channels = 2;
        } else if (strcmp(argv[next_index], "mono") != 0) {
            switch_log_printf(SWITCH_CHANNEL_SESSION_LOG(session), SWITCH_LOG_ERROR,
                              "invalid mix type: %s, must be mono, mixed, or stereo\n", argv[next_index]);
            return stream_fail(error, "Invalid mix type; expected mono, mixed or stereo");
        }
        ++next_index;
        if (next_index < argc && strcasecmp(argv[next_index], "mute_user") != 0) {
            sampling_str = argv[next_index++];
            sampling = parse_sampling_rate(sampling_str);
        }
    } else {
        /* A read ping keeps terminal cleanup active without buffering captured audio. */
        flags |= SMBF_READ_PING;
    }
    if (options.receive_audio) {
        flags |= SMBF_WRITE_REPLACE;
        if (next_index < argc && strcasecmp(argv[next_index], "mute_user") != 0) {
            playback_sampling_str = argv[next_index++];
            playback_sampling = parse_sampling_rate(playback_sampling_str);
        }
    }
    if (options.send_audio && next_index < argc && !strcasecmp(argv[next_index], "mute_user")) {
        options.start_muted = SWITCH_TRUE;
        ++next_index;
    }
    if (next_index < argc) {
        switch_log_printf(SWITCH_CHANNEL_SESSION_LOG(session), SWITCH_LOG_ERROR, "unexpected argument: %s\n",
                          argv[next_index]);
        return stream_fail(error, "Unexpected start argument; check rates, direction and mute_user");
    }

    if (!validate_ws_uri(argv[2], &wsUri[0])) {
        if (suppress_log) {
            switch_log_printf(SWITCH_CHANNEL_SESSION_LOG(session), SWITCH_LOG_ERROR,
                              "invalid websocket uri (details suppressed)\n");
        } else {
            switch_log_printf(SWITCH_CHANNEL_SESSION_LOG(session), SWITCH_LOG_ERROR, "invalid websocket uri: %s\n",
                              argv[2]);
        }
        return stream_fail(error, "Invalid WebSocket URI; expected ws:// or wss:// with a valid host and port");
    } else if (sampling < STREAM_MIN_SAMPLING || sampling > STREAM_MAX_SAMPLING || sampling % 8000 != 0) {
        if (sampling_str) {
            switch_log_printf(SWITCH_CHANNEL_SESSION_LOG(session), SWITCH_LOG_ERROR,
                              "invalid send sample rate: %s (must be a multiple of 8000 between %d and "
                              "%d)\n",
                              sampling_str, STREAM_MIN_SAMPLING, STREAM_MAX_SAMPLING);
        } else {
            switch_log_printf(SWITCH_CHANNEL_SESSION_LOG(session), SWITCH_LOG_ERROR, "invalid send sample rate: %d\n",
                              sampling);
        }
        return stream_fail(error, "Invalid send sample rate; expected a multiple of 8000 from 8000 to 48000");
    } else if (playback_sampling < STREAM_MIN_SAMPLING || playback_sampling > STREAM_MAX_SAMPLING ||
               playback_sampling % 8000 != 0) {
        if (playback_sampling_str) {
            switch_log_printf(SWITCH_CHANNEL_SESSION_LOG(session), SWITCH_LOG_ERROR,
                              "invalid playback sample rate: %s (must be a multiple of 8000 between %d "
                              "and %d)\n",
                              playback_sampling_str, STREAM_MIN_SAMPLING, STREAM_MAX_SAMPLING);
        } else {
            switch_log_printf(SWITCH_CHANNEL_SESSION_LOG(session), SWITCH_LOG_ERROR,
                              "invalid playback sample rate: %d\n", playback_sampling);
        }
        return stream_fail(error, "Invalid playback sample rate; expected a multiple of 8000 from 8000 to 48000");
    } else {
        options.ws_uri = wsUri;
        options.send_rate = sampling;
        options.playback_rate = playback_sampling;
        return start_capture(session, flags, &options, error);
    }
}

static switch_status_t stream_api_execute(switch_stream_handle_t *stream, switch_core_session_t *session,
                                          const char *cmd, const stream_api_config_t *api_config) {
    char *mycmd = NULL, *argv[16] = {0};
    const char *stream_name = STREAM_DEFAULT_NAME;
    char bug_name[MAX_BUG_NAME];
    unsigned int argc = 0;
    void *lifecycle_guard = NULL;
    stream_error_t error = {0};
    switch_bool_t stream_selected = SWITCH_FALSE;

    switch_status_t status = SWITCH_STATUS_FALSE;

    if (!zstr(cmd)) {
        mycmd = strdup(cmd);
    }
    if (mycmd) {
        argc = switch_separate_string(mycmd, ' ', argv, (sizeof(argv) / sizeof(argv[0])));
    }

    if (zstr(cmd)) {
        stream->write_function(stream, "%s\n", api_config->syntax);
        goto done;
    }
    if (!mycmd) {
        stream_fail(&error, "Failed to allocate command buffer");
        goto respond;
    }
    if (argc < 2 || argc == sizeof(argv) / sizeof(argv[0])) {
        switch_log_printf(SWITCH_CHANNEL_SESSION_LOG(session), SWITCH_LOG_ERROR,
                          "invalid stream command argument count\n");
        stream_fail(&error, argc < 2 ? "Expected a channel UUID and command" : "Too many command arguments");
        goto respond;
    }

    switch_bool_t has_selector = SWITCH_FALSE;
    for (unsigned int i = 2; i < argc;) {
        if (strncmp(argv[i], "stream=", 7) != 0) {
            ++i;
            continue;
        }
        stream_name = argv[i] + 7;
        if (has_selector || !valid_stream_name(stream_name)) {
            stream_fail(&error, has_selector
                                    ? "Specify only one stream selector"
                                    : "Invalid stream name; expected 1-64 lowercase letters, digits, '_' or '-'");
            goto respond;
        }
        has_selector = SWITCH_TRUE;
        remove_argument(argv, &argc, i);
    }
    if (strcmp(stream_name, STREAM_DEFAULT_NAME) == 0) {
        switch_copy_string(bug_name, MY_BUG_NAME, sizeof(bug_name));
    } else {
        switch_snprintf(bug_name, sizeof(bug_name), MY_BUG_NAME ":%s", stream_name);
    }

    stream_selected = SWITCH_TRUE;
    stream_command_t command = stream_command_from_string(argv[1]);

    switch_core_session_t *lsession = switch_core_session_locate(argv[0]);
    if (lsession) {
        lifecycle_guard = stream_session_lifecycle_lock(lsession);
        if (!lifecycle_guard) {
            switch_log_printf(SWITCH_CHANNEL_SESSION_LOG(lsession), SWITCH_LOG_ERROR,
                              "failed to acquire stream lifecycle lock\n");
            stream_fail(&error, "Failed to acquire stream lifecycle lock");
            goto release_session;
        }

        switch_bool_t suppress_log = suppress_sensitive_logs(lsession, bug_name);
        if (command != STREAM_CMD_SEND_JSON) {
            const char *logged_command = suppress_log ? argv[1] : cmd;
            switch_log_printf(SWITCH_CHANNEL_SESSION_LOG(lsession), SWITCH_LOG_DEBUG,
                              "mod_openai_audio_stream %s [%s] cmd: %s%s\n", api_config->api_name, stream_name,
                              logged_command ? logged_command : "", suppress_log ? " (arguments suppressed)" : "");
        }

        switch (command) {
            case STREAM_CMD_STOP:
                if (argc > 3) {
                    switch_log_printf(SWITCH_CHANNEL_SESSION_LOG(lsession), SWITCH_LOG_ERROR,
                                      "stop accepts at most one final json argument\n");
                    stream_fail(&error, "Stop accepts at most one Base64 JSON payload");
                    goto release_session;
                }
                status = do_stop(lsession, bug_name, argc > 2 ? argv[2] : NULL, &error);
                break;
            case STREAM_CMD_PAUSE:
            case STREAM_CMD_RESUME:
                if (argc > 2) {
                    switch_log_printf(SWITCH_CHANNEL_SESSION_LOG(lsession), SWITCH_LOG_ERROR,
                                      "%s does not accept arguments\n", argv[1]);
                    stream_fail(&error, "Pause and resume accept only an optional stream selector");
                    goto release_session;
                }
                status = do_pauseresume(lsession, bug_name, command == STREAM_CMD_PAUSE ? 1 : 0, &error);
                break;
            case STREAM_CMD_SEND_JSON:
                if (argc != 3) {
                    switch_log_printf(SWITCH_CHANNEL_SESSION_LOG(lsession), SWITCH_LOG_ERROR,
                                      "send_json requires exactly one argument specifying json to send\n");
                    stream_fail(&error, "send_json requires exactly one Base64 JSON payload");
                    goto release_session;
                }
                status = send_json(lsession, bug_name, argv[2], &error);
                break;
            case STREAM_CMD_START: {
                stream_start_options_t options = {0};
                options.name = stream_name;
                options.bug_name = bug_name;
                options.raw_audio = api_config->force_raw_audio_mode;
                status = start_stream(lsession, argc, argv, options, &error);
                break;
            }
            case STREAM_CMD_MUTE:
            case STREAM_CMD_UNMUTE: {
                if (argc > 3) {
                    switch_log_printf(SWITCH_CHANNEL_SESSION_LOG(lsession), SWITCH_LOG_ERROR,
                                      "%s accepts at most one target argument (user | openai | all)\n", argv[1]);
                    stream_fail(&error, "Mute and unmute accept at most one target: user, openai or all");
                    goto release_session;
                }
                const char *target = (argc > 2) ? argv[2] : "user";
                status = do_audio_mute(lsession, bug_name, target, command == STREAM_CMD_MUTE ? 1 : 0, &error);
                break;
            }
            case STREAM_CMD_UNKNOWN:
            default:
                switch_log_printf(SWITCH_CHANNEL_SESSION_LOG(lsession), SWITCH_LOG_ERROR,
                                  "unsupported mod_openai_audio_stream cmd: %s\n", argv[1]);
                stream_fail(&error, "Unknown command; expected start, stop, pause, resume, mute, unmute or send_json");
                break;
        }

    release_session:
        stream_session_lifecycle_unlock(lifecycle_guard);
        switch_core_session_rwunlock(lsession);
    } else {
        switch_log_printf(SWITCH_CHANNEL_SESSION_LOG(session), SWITCH_LOG_ERROR, "Error locating session %s\n",
                          argv[0]);
        stream_fail(&error, "Channel not found");
    }

respond:
    if (status == SWITCH_STATUS_SUCCESS) {
        stream->write_function(stream, "+OK Success\n");
    } else if (stream_selected) {
        stream->write_function(stream, "-ERR %s [stream=%s]\n", error.message[0] ? error.message : "Operation failed",
                               stream_name);
    } else {
        stream->write_function(stream, "-ERR %s\n", error.message[0] ? error.message : "Operation failed");
    }

done:
    switch_safe_free(mycmd);
    return SWITCH_STATUS_SUCCESS;
}

static const stream_api_config_t STREAM_API_CONFIG = {"uuid_openai_audio_stream", STREAM_API_SYNTAX, SWITCH_FALSE};
static const stream_api_config_t RAW_STREAM_API_CONFIG = {"uuid_raw_audio_stream", RAW_STREAM_API_SYNTAX, SWITCH_TRUE};

SWITCH_STANDARD_API(stream_function) {
    return stream_api_execute(stream, session, cmd, &STREAM_API_CONFIG);
}

SWITCH_STANDARD_API(raw_stream_function) {
    return stream_api_execute(stream, session, cmd, &RAW_STREAM_API_CONFIG);
}

SWITCH_MODULE_LOAD_FUNCTION(mod_openai_audio_stream_load) {
    switch_api_interface_t *api_interface;

    switch_log_printf(SWITCH_CHANNEL_LOG, SWITCH_LOG_NOTICE, "mod_openai_audio_stream API loading..\n");

    *module_interface = switch_loadable_module_create_module_interface(pool, modname);
    module_rwlock = (*module_interface)->rwlock;

    if (switch_event_reserve_subclass(EVENT_JSON) != SWITCH_STATUS_SUCCESS ||
        switch_event_reserve_subclass(EVENT_CONNECT) != SWITCH_STATUS_SUCCESS ||
        switch_event_reserve_subclass(EVENT_ERROR) != SWITCH_STATUS_SUCCESS ||
        switch_event_reserve_subclass(EVENT_DISCONNECT) != SWITCH_STATUS_SUCCESS ||
        switch_event_reserve_subclass(EVENT_PLAY) != SWITCH_STATUS_SUCCESS ||
        switch_event_reserve_subclass(EVENT_OPENAI_SPEECH_STARTED) != SWITCH_STATUS_SUCCESS ||
        switch_event_reserve_subclass(EVENT_OPENAI_SPEECH_STOPPED) != SWITCH_STATUS_SUCCESS) {
        switch_log_printf(SWITCH_CHANNEL_LOG, SWITCH_LOG_ERROR,
                          "Couldn't register an event subclass for mod_openai_audio_stream API.\n");
        /* roll back the subclasses already reserved by this chain */
        free_event_subclasses();
        return SWITCH_STATUS_TERM;
    }
    SWITCH_ADD_API(api_interface, "uuid_openai_audio_stream", "audio_stream API", stream_function, STREAM_API_SYNTAX);
    SWITCH_ADD_API(api_interface, "uuid_raw_audio_stream", "raw audio_stream API", raw_stream_function,
                   RAW_STREAM_API_SYNTAX);
    switch_console_set_complete("add uuid_openai_audio_stream ::console::list_uuid start ws-uri");
    switch_console_set_complete("add uuid_openai_audio_stream ::console::list_uuid stop");
    switch_console_set_complete("add uuid_openai_audio_stream ::console::list_uuid pause");
    switch_console_set_complete("add uuid_openai_audio_stream ::console::list_uuid resume");
    switch_console_set_complete("add uuid_openai_audio_stream ::console::list_uuid mute");
    switch_console_set_complete("add uuid_openai_audio_stream ::console::list_uuid unmute");
    switch_console_set_complete("add uuid_openai_audio_stream ::console::list_uuid send_json");
    switch_console_set_complete("add uuid_raw_audio_stream ::console::list_uuid start ws-uri");
    switch_console_set_complete("add uuid_raw_audio_stream ::console::list_uuid stop");
    switch_console_set_complete("add uuid_raw_audio_stream ::console::list_uuid pause");
    switch_console_set_complete("add uuid_raw_audio_stream ::console::list_uuid resume");
    switch_console_set_complete("add uuid_raw_audio_stream ::console::list_uuid mute");
    switch_console_set_complete("add uuid_raw_audio_stream ::console::list_uuid unmute");
    switch_console_set_complete("add uuid_raw_audio_stream ::console::list_uuid send_json");

    switch_log_printf(SWITCH_CHANNEL_LOG, SWITCH_LOG_NOTICE, "mod_openai_audio_stream API successfully loaded\n");

    return SWITCH_STATUS_SUCCESS;
}

SWITCH_MODULE_SHUTDOWN_FUNCTION(mod_openai_audio_stream_shutdown) {
    free_event_subclasses();

    return SWITCH_STATUS_SUCCESS;
}
