! signet: in float::k1, in series::time::float::s1, out series::time::float::s2
program sf_gain_f90
  use signet
  implicit none
  real(8) :: k1
  type(signet_series) :: s

  call signet_get("k1", k1)
  call signet_get("s1", s)
  s%values = k1 * s%values
  call signet_put("s2", s)
end program sf_gain_f90
