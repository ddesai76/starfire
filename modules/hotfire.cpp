// hotfire.cpp:    EXAMPLE TEST POINT MODULE (ELECTRIC PROPULSION HOT FIRE)
// AUTHOR:         DANIEL DESAI
// UPDATED:        2026-10-13
// VERSION:        0.1.0
//
// Action: Energize HV, check fuel, fire, print voltage/current/accel as CSV (values simulated)
// Build:  g++ -O2 -std=c++17 -o dist/hotfire hotfire.cpp


#include <chrono>
#include <csignal>
#include <cstdint>
#include <cstdio>
#include <thread>
#include <vector>

namespace {

const uint8_t FIRE = 0xFF;              // 0b11111111 fires all 8 thrusters
const float MIN_FUEL = 10.0f;
const float BURN_TIME = 300.0f;         // s
const float SAMPLE_TIME = 1.0f;         // s

volatile std::sig_atomic_t g_break = 0;
void on_term(int) { g_break = 1; }

// Stand-in for the thruster register; on the device, point REG at the mapped address.
volatile uint8_t g_sim_reg = 0;
volatile uint8_t* const REG = &g_sim_reg;
// volatile uint8_t* const REG = reinterpret_cast<volatile uint8_t*>(0x40000000);

// Byte store to the register in assembly (x86-64 and AArch64; plain store elsewhere).
void set_reg(uint8_t v) {
#if defined(__x86_64__)
    asm volatile(
        "movb %1, %0\n\t"
        "mfence"
        : "=m"(*REG)
        : "q"(v)
        : "memory");
#elif defined(__aarch64__)
    asm volatile(
        "strb %w1, %0\n\t"
        "dsb sy"
        : "=m"(*REG)
        : "r"(v)
        : "memory");
#else
    *REG = v;
#endif
}

// xorshift32 jitter in [-1, 1]; avoids <random> on small targets.
float jitter() {
    static uint32_t s = 2463534242u;
    s ^= s << 13; s ^= s >> 17; s ^= s << 5;
    return (s / 2147483648.0f) - 1.0f;
}

}  // namespace

int main() {
    std::signal(SIGTERM, on_term);
    std::signal(SIGINT, on_term);

    // send("HV ON");
    // const auto fuel = query("FUEL?");
    const auto fuel = 90.0f;
    if (fuel < MIN_FUEL) {
        // send("HV OFF");
        std::puts("# NO START: fuel below MIN_FUEL");
        return 1;
    }

    set_reg(FIRE);
    const auto n = __builtin_popcount(*REG);    // thrusters energized
    const auto samples = static_cast<int>(BURN_TIME / SAMPLE_TIME);
    std::vector<float> voltage, current, accel;

    std::puts("time_s,voltage_V,current_uA,accel_um_s2");
    for (auto k = 0; k < samples && !g_break; ++k) {
        // voltage.emplace_back(query("MEAS:VOLT?"));
        // current.emplace_back(query("MEAS:CURR?"));
        // accel.emplace_back(query("STAND:ACCEL?"));
        const auto v = 1500.0f + 15.0f * jitter();      // random: ~1500 V, +/-15 V
        const auto i = n * (50.0f + 1.0f * jitter());   // random: ~50 uA per thruster, +/-1 uA
        const auto a = n * (20.0f + 0.5f * jitter());   // random: ~20 um/s^2 per thruster, +/-0.5
        voltage.emplace_back(v);
        current.emplace_back(i);
        accel.emplace_back(a);
        std::printf("%.1f,%.1f,%.2f,%.2f\n", k * SAMPLE_TIME, voltage[k], current[k], accel[k]);
        std::fflush(stdout);
        std::this_thread::sleep_for(std::chrono::duration<float>(SAMPLE_TIME));
    }

    set_reg(0);
    // send("HV OFF");
    std::puts(g_break ? "# STOPPED" : "# DONE");
    return g_break ? 1 : 0;
}