-- What the watch says it is in its probe reply. The name and model columns
-- have been here since the first migration and were never written; the rest of
-- the reply's version fields join them.
--
-- The probe reply also carries the association secret. It is not stored.
ALTER TABLE device ADD COLUMN firmware INTEGER;
ALTER TABLE device ADD COLUMN bootloader INTEGER;
-- Null where the watch sends 0xFFFFFF, which is what it says when it has no
-- version to report rather than a version of 16777215.
ALTER TABLE device ADD COLUMN hardware INTEGER;
ALTER TABLE device ADD COLUMN rescue INTEGER;
