// signet: in float::k1, in series::time::float::s1, out series::time::float::s2
#include <signet.hpp>

int main() {
    double k1 = signet::in<double>("k1");
    auto s = signet::in<signet::Series>("s1");
    for (double& a : s.values) {
        a *= k1;
    }
    signet::out("s2", s);
}
