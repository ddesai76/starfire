-- signet: in float::k1, in series::time::float::s1, out series::time::float::s2
import Signet

main :: IO ()
main = do
  k1 <- getFloat "k1"
  s1 <- getSeries "s1"
  put "s2" s1 { seriesValues = map (k1 *) (seriesValues s1) } ""
