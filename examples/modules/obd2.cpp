// <Filename>: <obd2.cpp>
// <Author>:   <DANIEL DESAI>
// <Updated>:  <2026-10-20>
// <Version>:  <0.2.1>
//
// Stand-alone OBD2 / ELM327 query tool, derived from jolt_obd2.hpp.
//
// USAGE
//   obd2 <MODE>[PID] [port] [--clear]
//     01XX  Current data, PID XX          e.g. 010C (RPM)
//     02    Freeze frame                  not supported
//     03    Stored DTCs
//     04    Clear DTCs                    prompts first; --clear skips prompt
//     05    O2 sensor monitoring          not supported
//     06    On-board monitor results      not supported
//     07    Pending DTCs
//     08    On-board system control       not supported
//     09XX  Vehicle info, PID XX          e.g. 0902 (VIN)
//     0A    Permanent DTCs                not supported
//     port  Serial device; omit to auto-detect (e.g. /dev/ttyUSB0)
//
// EXIT CODES
//   0 Ok, 1 Bad args, 2 No adapter, 3 No data, 4 No clear
//
// BUILD
//   g++ -std=c++17 -O2 -o obd2 obd2.cpp

#ifdef _WIN32
#  include <windows.h>
#else
#  include <fcntl.h>
#  include <termios.h>
#  include <unistd.h>
#  include <dirent.h>
#  include <sys/select.h>
#endif

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cstdio>
#include <string>
#include <thread>
#include <vector>

// ── Serial ────────────────────────────────────────────────────────────────────

#ifdef _WIN32
using SerialHandle = HANDLE;
static const SerialHandle INVALID_SERIAL = INVALID_HANDLE_VALUE;
#else
using SerialHandle = int;
static const SerialHandle INVALID_SERIAL = -1;
#endif

static const int ELM_BAUD_RATES[] = { 115200, 38400, 9600, 57600, 0 };

static SerialHandle serial_open(const std::string& port, int baud)
{
#ifdef _WIN32
    std::string path = "\\\\.\\" + port;
    HANDLE h = CreateFileA(path.c_str(), GENERIC_READ | GENERIC_WRITE,
                           0, nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) return INVALID_SERIAL;
    DCB dcb{}; dcb.DCBlength = sizeof(dcb);
    if (!GetCommState(h, &dcb)) { CloseHandle(h); return INVALID_SERIAL; }
    dcb.BaudRate = (DWORD)baud; dcb.ByteSize = 8;
    dcb.Parity = NOPARITY; dcb.StopBits = ONESTOPBIT;
    if (!SetCommState(h, &dcb)) { CloseHandle(h); return INVALID_SERIAL; }
    COMMTIMEOUTS to{50,0,2000,0,2000}; SetCommTimeouts(h, &to);
    return h;
#else
    int fd = ::open(port.c_str(), O_RDWR | O_NOCTTY | O_NONBLOCK);
    if (fd < 0) return INVALID_SERIAL;
    fcntl(fd, F_SETFL, fcntl(fd, F_GETFL, 0) & ~O_NONBLOCK);
    termios tty{};
    if (tcgetattr(fd, &tty) != 0) { ::close(fd); return INVALID_SERIAL; }
    speed_t speed = B115200;
    if      (baud == 38400) speed = B38400;
    else if (baud == 9600)  speed = B9600;
    else if (baud == 57600) speed = B57600;
    cfsetispeed(&tty, speed); cfsetospeed(&tty, speed);
    cfmakeraw(&tty);
    tty.c_cflag |= (CLOCAL | CREAD);
    tty.c_cc[VMIN] = 0; tty.c_cc[VTIME] = 20;
    if (tcsetattr(fd, TCSANOW, &tty) != 0) { ::close(fd); return INVALID_SERIAL; }
    tcflush(fd, TCIOFLUSH);
    return fd;
#endif
}

static void serial_close(SerialHandle h) {
#ifdef _WIN32
    CloseHandle(h);
#else
    ::close(h);
#endif
}

static void serial_flush(SerialHandle h) {
#ifdef _WIN32
    PurgeComm(h, PURGE_RXCLEAR | PURGE_TXCLEAR);
#else
    tcflush(h, TCIOFLUSH);
#endif
}

static bool serial_write(SerialHandle h, const std::string& s) {
#ifdef _WIN32
    DWORD w = 0;
    return WriteFile(h, s.data(), (DWORD)s.size(), &w, nullptr) && w == (DWORD)s.size();
#else
    return ::write(h, s.data(), s.size()) == (ssize_t)s.size();
#endif
}

static std::string serial_read_until_prompt(SerialHandle h, int timeout_ms) {
    std::string buf;
    auto deadline = std::chrono::steady_clock::now()
                  + std::chrono::milliseconds(timeout_ms);
    char c;
#ifdef _WIN32
    DWORD got;
    while (std::chrono::steady_clock::now() < deadline) {
        if (ReadFile(h, &c, 1, &got, nullptr) && got == 1) {
            buf += c; if (c == '>') break;
        }
    }
#else
    while (std::chrono::steady_clock::now() < deadline) {
        fd_set fds; FD_ZERO(&fds); FD_SET(h, &fds);
        auto rem = std::chrono::duration_cast<std::chrono::microseconds>(
                       deadline - std::chrono::steady_clock::now()).count();
        if (rem <= 0) break;
        timeval tv{(long)(rem / 1'000'000), (long)(rem % 1'000'000)};
        if (select(h + 1, &fds, nullptr, nullptr, &tv) <= 0) break;
        if (::read(h, &c, 1) == 1) { buf += c; if (c == '>') break; }
    }
#endif
    return buf;
}

// ── ELM327 ────────────────────────────────────────────────────────────────────

static std::string to_upper(std::string s) {
    for (char& c : s) c = (char)std::toupper((unsigned char)c);
    return s;
}

static int hex_nibble(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    return -1;
}

static int hex_byte(const std::string& s, size_t pos) {
    if (pos + 1 >= s.size()) return -1;
    int hi = hex_nibble(s[pos]), lo = hex_nibble(s[pos + 1]);
    return (hi < 0 || lo < 0) ? -1 : (hi << 4) | lo;
}

static std::string elm_cmd(SerialHandle h, const std::string& cmd, int timeout_ms = 2000) {
    serial_write(h, cmd + "\r");
    std::string resp = serial_read_until_prompt(h, timeout_ms);
    while (!resp.empty() && (resp.back() == '>' || resp.back() == ' '
                          || resp.back() == '\r' || resp.back() == '\n'))
        resp.pop_back();
    return resp;
}

static bool elm_init(SerialHandle h) {
    // First ATZ stops any in-progress command; second does the reset.
    serial_write(h, "ATZ\r");
    std::this_thread::sleep_for(std::chrono::milliseconds(1500));
    serial_read_until_prompt(h, 500);
    serial_flush(h);

    serial_write(h, "ATZ\r");
    std::this_thread::sleep_for(std::chrono::milliseconds(2000));
    if (to_upper(serial_read_until_prompt(h, 500)).find("ELM") == std::string::npos)
        return false;

    elm_cmd(h, "ATE0");   // echo off
    elm_cmd(h, "ATL0");   // linefeeds off
    elm_cmd(h, "ATS0");   // spaces off
    elm_cmd(h, "ATH0");   // headers off
    elm_cmd(h, "ATSP0");  // auto protocol
    return true;
}

static SerialHandle serial_open_probe(const std::string& port) {
    for (int i = 0; ELM_BAUD_RATES[i] != 0; ++i) {
        SerialHandle h = serial_open(port, ELM_BAUD_RATES[i]);
        if (h == INVALID_SERIAL) continue;

        // Interrupt any pending command and drain stale output.
        serial_write(h, "\r");
        std::this_thread::sleep_for(std::chrono::milliseconds(300));
        serial_flush(h);

        if (elm_init(h)) return h;

        serial_read_until_prompt(h, 200);
        serial_flush(h);
        serial_close(h);
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    return INVALID_SERIAL;
}

static std::vector<std::string> candidate_ports() {
    std::vector<std::string> ports;
#ifdef _WIN32
    for (int i = 1; i <= 32; ++i) ports.push_back("COM" + std::to_string(i));
#else
    for (auto& pfx : {"/dev/ttyUSB", "/dev/ttyACM", "/dev/cu.usbserial",
                      "/dev/cu.OBDII", "/dev/rfcomm"})
        for (int i = 0; i < 8; ++i) ports.push_back(pfx + std::to_string(i));
    if (DIR* d = opendir("/dev")) {
        while (dirent* e = readdir(d)) {
            std::string n = e->d_name;
            if (n.rfind("ttyUSB", 0) == 0 || n.rfind("ttyACM", 0) == 0 || n.rfind("cu.", 0) == 0) {
                std::string full = "/dev/" + n;
                if (std::find(ports.begin(), ports.end(), full) == ports.end())
                    ports.push_back(full);
            }
        }
        closedir(d);
    }
#endif
    return ports;
}

// Returns the data bytes after `hdr`, one vector per responding ECU.
// ATL0 means lines are CR-separated; ISO-TP multiframe lines are "N:hex...".
static std::vector<std::vector<int>> query(SerialHandle h, const std::string& cmd,
                                           const std::string& hdr, std::string& raw) {
    raw = elm_cmd(h, cmd, 8000);

    std::vector<std::string> lines;
    std::string cur;
    for (char c : to_upper(raw)) {
        if (c == '\r' || c == '\n') { if (!cur.empty()) lines.push_back(cur); cur.clear(); }
        else if (c != ' ') cur += c;
    }
    if (!cur.empty()) lines.push_back(cur);

    auto is_frame = [](const std::string& l) { return l.size() >= 2 && l[1] == ':'; };
    bool multi = std::any_of(lines.begin(), lines.end(), is_frame);

    std::vector<std::string> hexes;
    if (multi) {
        std::string hex;
        for (auto& l : lines) if (is_frame(l)) hex += l.substr(2);
        hexes.push_back(hex);
    } else {
        for (auto& l : lines) if (l.rfind(hdr, 0) == 0) hexes.push_back(l);
    }

    std::vector<std::vector<int>> out;
    for (auto& hex : hexes) {
        size_t pos = hex.find(hdr);
        if (pos == std::string::npos) continue;
        std::vector<int> bytes;
        for (size_t i = pos + hdr.size(); i + 1 < hex.size(); i += 2) {
            int b = hex_byte(hex, i);
            if (b < 0) break;
            bytes.push_back(b);
        }
        out.push_back(bytes);
    }
    return out;
}

// Modes 03/07: CAN prefixes a count byte (odd length); older protocols pad with 0000.
static std::vector<std::string> decode_dtcs(const std::vector<int>& b) {
    std::vector<std::string> out;
    for (size_t i = b.size() % 2; i + 1 < b.size(); i += 2) {
        if (b[i] == 0 && b[i + 1] == 0) continue;
        char s[6];
        std::snprintf(s, sizeof s, "%c%d%X%02X", "PCBU"[(b[i] >> 6) & 3],
                      (b[i] >> 4) & 3, b[i] & 0xF, b[i + 1] & 0xFF);
        out.push_back(s);
    }
    return out;
}

// ── Mode 01 decoders ──────────────────────────────────────────────────────────

// value = raw * scale + offset;  raw = A (1 byte) or 256A + B (2 bytes)
struct Decoder {
    int pid; const char* name; const char* unit; size_t nbytes; double scale, offset;
};

static const double PCT = 100.0 / 255;

static const Decoder MODE01[] = {
    {0x04, "Engine load",                 "%",    1, PCT,          0},
    {0x05, "Coolant temp",                "C",    1, 1,          -40},
    {0x06, "Short-term fuel trim B1",     "%",    1, 100.0 / 128, -100},
    {0x07, "Long-term fuel trim B1",      "%",    1, 100.0 / 128, -100},
    {0x0A, "Fuel pressure",               "kPa",  1, 3,            0},
    {0x0B, "Intake MAP",                  "kPa",  1, 1,            0},
    {0x0C, "Engine RPM",                  "rpm",  2, 0.25,         0},
    {0x0D, "Vehicle speed",               "km/h", 1, 1,            0},
    {0x0E, "Timing advance",              "deg",  1, 0.5,        -64},
    {0x0F, "Intake air temp",             "C",    1, 1,          -40},
    {0x10, "MAF rate",                    "g/s",  2, 0.01,         0},
    {0x11, "Throttle position",           "%",    1, PCT,          0},
    {0x1F, "Run time since start",        "s",    2, 1,            0},
    {0x21, "Distance with MIL on",        "km",   2, 1,            0},
    {0x23, "Fuel rail pressure",          "kPa",  2, 10,           0},
    {0x2C, "Commanded EGR",               "%",    1, PCT,          0},
    {0x2F, "Fuel level",                  "%",    1, PCT,          0},
    {0x31, "Distance since codes cleared","km",   2, 1,            0},
    {0x33, "Barometric pressure",         "kPa",  1, 1,            0},
    {0x3C, "Catalyst temp B1S1",          "C",    2, 0.1,        -40},
    {0x42, "Control module voltage",      "V",    2, 0.001,        0},
    {0x43, "Absolute load",               "%",    2, PCT,          0},
    {0x45, "Relative throttle position",  "%",    1, PCT,          0},
    {0x46, "Ambient air temp",            "C",    1, 1,          -40},
    {0x52, "Ethanol fuel",                "%",    1, PCT,          0},
    {0x5B, "Hybrid battery remaining",    "%",    1, PCT,          0},
    {0x5C, "Engine oil temp",             "C",    1, 1,          -40},
    {0x5E, "Engine fuel rate",            "L/h",  2, 0.05,         0},
};

// ── Main ──────────────────────────────────────────────────────────────────────

static const char* USAGE =
    "  01XX  Current data, PID XX          e.g. 010C (RPM)\n"
    "  02    Freeze frame                  not supported\n"
    "  03    Stored DTCs\n"
    "  04    Clear DTCs                    prompts first; --clear skips prompt\n"
    "  05    O2 sensor monitoring          not supported\n"
    "  06    On-board monitor results      not supported\n"
    "  07    Pending DTCs\n"
    "  08    On-board system control       not supported\n"
    "  09XX  Vehicle info, PID XX          e.g. 0902 (VIN)\n"
    "  0A    Permanent DTCs                not supported\n"
    "  port  Serial device; omit to auto-detect (e.g. /dev/ttyUSB0)\n";

// Modes 01/09 take a PID; 03/04/07 are bare (pid = -1).
static bool parse_cmd(const std::string& s, int& mode, int& pid) {
    if (s.size() != 2 && s.size() != 4) return false;
    if (!std::all_of(s.begin(), s.end(), [](char c) { return std::isxdigit((unsigned char)c); }))
        return false;
    mode = hex_byte(s, 0);
    pid  = s.size() == 4 ? hex_byte(s, 2) : -1;
    if (pid >= 0) return mode == 0x01 || mode == 0x09;
    return mode == 0x03 || mode == 0x04 || mode == 0x07;
}

int main(int argc, char** argv) {
    bool clear = false;
    std::vector<std::string> args;
    for (int i = 1; i < argc; ++i) {
        if (std::string(argv[i]) == "--clear") clear = true;
        else args.push_back(argv[i]);
    }

    int mode, pid;
    if (args.empty() || args.size() > 2 || !parse_cmd(args[0], mode, pid)) {
        std::fprintf(stderr, "usage: %s <MODE>[PID] [port] [--clear]\n%s", argv[0], USAGE);
        return 1;
    }

    if (mode == 0x04 && !clear) {
        std::printf("Clear DTCs, freeze frame and readiness monitors? "
                    "Engine off, ignition on. [y/N] ");
        std::fflush(stdout);
        int c = std::getchar();
        if (c != 'y' && c != 'Y') { std::printf("Aborted\n"); return 4; }
    }

    bool bare = pid < 0;
    char cmd[5], hdr[5];
    if (bare) {
        std::snprintf(cmd, sizeof cmd, "%02X", mode & 0xFF);
        std::snprintf(hdr, sizeof hdr, "%02X", (mode + 0x40) & 0xFF);
    } else {
        std::snprintf(cmd, sizeof cmd, "%02X%02X", mode & 0xFF, pid & 0xFF);
        std::snprintf(hdr, sizeof hdr, "%02X%02X", (mode + 0x40) & 0xFF, pid & 0xFF);
    }

    SerialHandle h = INVALID_SERIAL;
    std::string port;
    if (args.size() == 2) {
        port = args[1];
        h = serial_open_probe(port);
    } else {
        for (auto& p : candidate_ports())
            if ((h = serial_open_probe(p)) != INVALID_SERIAL) { port = p; break; }
    }
    if (h == INVALID_SERIAL) {
        std::fprintf(stderr, "No ELM327 adapter found%s%s\n",
                     port.empty() ? "" : " on ", port.c_str());
        return 2;
    }

    std::string raw;
    auto resp = query(h, cmd, hdr, raw);
    serial_close(h);

    std::printf("Port:  %s\nCmd:   %s\n", port.c_str(), cmd);
    if (resp.empty() || (!bare && resp[0].empty())) {
        std::string r = raw;
        std::replace(r.begin(), r.end(), '\r', ' ');
        std::printf("No data (adapter said: %s)\n", r.empty() ? "<nothing>" : r.c_str());
        return 3;
    }

    if (mode == 0x04) { std::printf("Codes cleared\n"); return 0; }

    if (bare) {
        std::vector<std::string> codes;
        for (auto& r : resp)
            for (auto& c : decode_dtcs(r)) codes.push_back(c);
        std::printf("%s DTCs:", mode == 0x03 ? "Stored" : "Pending");
        if (codes.empty()) std::printf(" none");
        for (auto& c : codes) std::printf(" %s", c.c_str());
        std::printf("\n");
        return 0;
    }

    const auto& data = resp[0];
    std::printf("Bytes:");
    for (int b : data) std::printf(" %02X", b);
    std::printf("\n");

    if (mode == 0x01) {
        for (auto& d : MODE01) {
            if (d.pid != pid) continue;
            if (data.size() < d.nbytes) { std::printf("Short response for %s\n", d.name); return 3; }
            int raw = d.nbytes == 2 ? 256 * data[0] + data[1] : data[0];
            std::printf("%s: %.3f %s\n", d.name, raw * d.scale + d.offset, d.unit);
        }
    } else if (mode == 0x09 && pid == 0x02) {
        // First byte is the data-item count; VIN follows as ASCII.
        std::string vin;
        for (size_t i = 1; i < data.size() && vin.size() < 17; ++i)
            if (std::isalnum(data[i])) vin += (char)data[i];
        std::printf("VIN:   %s%s\n", vin.c_str(), vin.size() == 17 ? "" : "  (incomplete)");
    }
    return 0;
}