# signet: in float::k1, in series::time::float::s1, out series::time::float::s2
. "$SIGNET_HELPERS/signet.sh"

k1=$(signet_get k1)
signet_get s1 | awk -v k="$k1" '{ printf "%.17g %.17g\n", $1, k * $2 }' | signet_put_series s2 "$(signet_domain s1)"
