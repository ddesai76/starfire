// starfire.cpp:   HARDWARE INTERFACE (builds to starfire-hw)
// AUTHOR:         DANIEL DESAI
// UPDATED:        2026-10-11
// VERSION:        0.2.0
//
// Sends one SCPI line to a device node; a query (contains '?') also reads the reply.
// Prints one JSON line on stdout. Exit: 0 ok, 1 runtime failure, 2 usage error.
//
// Usage:  starfire-hw send [--timeout <sec>] [--opc] <device> <scpi line>
// Build:  g++ -O2 -std=c++17 -o starfire-hw starfire.cpp

#include <cerrno>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <string>
#include <fcntl.h>
#include <sys/time.h>
#include <unistd.h>

namespace {

const int kDefaultTimeoutSec = 5;
const int kMaxTimeoutSec = 60;
const size_t kMaxCommandLen = 1024;

std::string g_mode, g_dev, g_cmd, g_kind;

std::string json_escape(const std::string& s) {
    std::string out;
    for (unsigned char c : s) {
        switch (c) {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (c < 0x20) { char b[8]; std::snprintf(b, sizeof b, "\\u%04x", c); out += b; }
                else out += static_cast<char>(c);
        }
    }
    return out;
}

std::string utc_now() {
    timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    tm tmv;
    gmtime_r(&ts.tv_sec, &tmv);
    char buf[40];
    std::strftime(buf, sizeof buf, "%Y-%m-%dT%H:%M:%S", &tmv);
    char out[64];
    std::snprintf(out, sizeof out, "%s.%03dZ", buf, static_cast<int>(ts.tv_nsec / 1000000));
    return out;
}

std::string result_line(bool ok, const std::string& key, const std::string& value) {
    return "{\"t\":\"" + utc_now() + "\",\"mode\":\"" + json_escape(g_mode) +
           "\",\"dev\":\"" + json_escape(g_dev) + "\",\"cmd\":\"" + json_escape(g_cmd) +
           "\",\"kind\":\"" + g_kind + "\",\"ok\":" + (ok ? "true" : "false") +
           ",\"" + key + "\":\"" + json_escape(value) + "\"}\n";
}

[[noreturn]] void fail(const std::string& message) {
    std::string line = result_line(false, "error", message);
    std::fputs(line.c_str(), stdout);
    std::fflush(stdout);
    std::exit(1);
}

char g_timeout_line[1024];
size_t g_timeout_len = 0;
void on_timeout(int) {
    ssize_t r = write(STDOUT_FILENO, g_timeout_line, g_timeout_len);
    (void)r;
    _exit(1);
}

[[noreturn]] void usage(const char* why) {
    if (why) std::fprintf(stderr, "starfire-hw: %s\n", why);
    std::fprintf(stderr,
        "usage: starfire-hw send [--timeout <sec>] [--opc] <device> <scpi line>\n");
    std::exit(2);
}

bool write_all(int fd, const std::string& data) {
    size_t off = 0;
    while (off < data.size()) {
        ssize_t n = write(fd, data.data() + off, data.size() - off);
        if (n < 0) {
            if (errno == EINTR) continue;
            return false;
        }
        off += static_cast<size_t>(n);
    }
    return true;
}

bool read_reply(int fd, std::string& reply) {
    char buf[65536];
    ssize_t n;
    do { n = read(fd, buf, sizeof buf); } while (n < 0 && errno == EINTR);
    if (n < 0) return false;
    while (n > 0 && (buf[n - 1] == '\n' || buf[n - 1] == '\r')) --n;
    reply.assign(buf, static_cast<size_t>(n));
    return true;
}

bool valid_device(const std::string& dev) {
    return dev.rfind("/dev/", 0) == 0 && dev.find("..") == std::string::npos;
}

bool valid_command(const std::string& cmd) {
    if (cmd.empty() || cmd.size() > kMaxCommandLen) return false;
    for (unsigned char c : cmd)
        if (c < 0x20 || c == 0x7f) return false;
    return true;
}

}

int main(int argc, char* argv[]) {
    if (argc < 2) usage(nullptr);
    g_mode = argv[1];
    if (g_mode != "send") usage("first argument must be 'send'");

    int timeout_sec = kDefaultTimeoutSec;
    bool opc = false;
    std::string positional[2];
    int npos = 0;
    for (int i = 2; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--timeout") {
            if (i + 1 >= argc) usage("--timeout needs a value");
            char* end = nullptr;
            long v = std::strtol(argv[++i], &end, 10);
            if (*end != '\0' || v < 1 || v > kMaxTimeoutSec) usage("--timeout must be 1-60 seconds");
            timeout_sec = static_cast<int>(v);
        } else if (a == "--opc") {
            opc = true;
        } else if (npos < 2) {
            positional[npos++] = a;
        } else {
            usage("too many arguments");
        }
    }
    if (npos != 2) usage("need <device> and <command>");
    g_dev = positional[0];
    g_cmd = positional[1];
    bool is_query = g_cmd.find('?') != std::string::npos;
    g_kind = is_query ? "query" : "command";
    if (opc && is_query) usage("--opc only applies to a command, not a query");

    std::string tl = result_line(false, "error", "timed out after " + std::to_string(timeout_sec) + " s");
    g_timeout_len = tl.size() < sizeof g_timeout_line ? tl.size() : sizeof g_timeout_line - 1;
    std::memcpy(g_timeout_line, tl.data(), g_timeout_len);
    struct sigaction sa;
    std::memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_timeout;
    sigaction(SIGALRM, &sa, nullptr);
    alarm(static_cast<unsigned>(timeout_sec));

    if (!valid_device(g_dev)) fail("device must be a path under /dev/");
    if (!valid_command(g_cmd)) fail("command must be 1-1024 printable characters on one line");

    int fd = open(g_dev.c_str(), O_RDWR);
    if (fd < 0) fail("could not open " + g_dev + ": " + std::strerror(errno));

    if (!write_all(fd, g_cmd + "\n")) {
        std::string e = std::strerror(errno);
        close(fd);
        fail("write to " + g_dev + " failed: " + e);
    }

    std::string reply;
    if (is_query) {
        if (!read_reply(fd, reply)) {
            std::string e = std::strerror(errno);
            close(fd);
            fail("read from " + g_dev + " failed: " + e);
        }
    } else if (opc) {
        if (!write_all(fd, "*OPC?\n") || !read_reply(fd, reply)) {
            std::string e = std::strerror(errno);
            close(fd);
            fail("*OPC? after the command failed: " + e);
        }
        if (reply != "1") {
            close(fd);
            fail("*OPC? returned '" + reply + "', expected '1'");
        }
        reply.clear();
    }
    close(fd);
    alarm(0);

    std::fputs(result_line(true, "reply", reply).c_str(), stdout);
    return 0;
}