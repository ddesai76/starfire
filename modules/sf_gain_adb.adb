-- signet: in float::k1, in series::time::float::s1, out series::time::float::s2
with Signet;

procedure Sf_Gain_Adb is
   K1 : constant Long_Float := Signet.Get_Float ("k1");
   S  : Signet.Series := Signet.Get_Series ("s1");
begin
   for A of S.Values loop
      A := K1 * A;
   end loop;
   Signet.Put ("s2", S);
end Sf_Gain_Adb;
