-- Fencing token for host-circuit writes.
--
-- The breaker is the one row that two independent service processes race over: both can
-- observe an elapsed OPEN circuit and both can try to become the single permitted prober,
-- and a slow writer from a resolved incident can replay its transition over a newer one.
-- In-process locks cannot arbitrate either case. `revision` advances on every persisted
-- write, so a writer that carries the revision it observed only wins while that revision
-- is still current. Existing rows adopt 1 because revision 0 means "no row stored yet".
ALTER TABLE host_circuits ADD COLUMN revision INTEGER NOT NULL DEFAULT 1
