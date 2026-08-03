option(ENABLE_TEST_SANITIZERS "Enable AddressSanitizer and UndefinedBehaviorSanitizer for unit tests" OFF)

function(add_stream_test target)
    add_executable(${target} ${ARGN})
    target_include_directories(${target} PRIVATE ${MODULE_SOURCE_DIR})

    if(ENABLE_TEST_SANITIZERS AND CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
        target_compile_options(${target} PRIVATE -fsanitize=address,undefined -fno-omit-frame-pointer)
        target_link_options(${target} PRIVATE -fsanitize=address,undefined -fno-omit-frame-pointer)
    endif()

    add_test(NAME ${target} COMMAND ${target})
endfunction()

add_stream_test(test_stream_protocol
    test_stream_protocol.cpp
    ${MODULE_SOURCE_DIR}/stream_protocol.cpp
)

add_stream_test(test_base64
    test_base64.cpp
    ${MODULE_SOURCE_DIR}/base64.cpp
)

add_stream_test(test_playback_queue
    test_playback_queue.cpp
    ${MODULE_SOURCE_DIR}/playback_queue.cpp
)
target_link_libraries(test_playback_queue PRIVATE Threads::Threads)
