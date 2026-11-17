// signet: in float::k1, in series::time::float::s1, out series::time::float::s2
#include <signet.h>

int main(void) {
    double k1 = signet_get_double("k1");
    signet_series s = signet_get_series("s1");
    for (size_t i = 0; i < s.n; i++) {
        s.values[i] *= k1;
    }
    signet_put_series("s2", s, NULL);
    return 0;
}
