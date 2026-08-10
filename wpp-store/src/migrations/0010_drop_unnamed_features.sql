-- 100 and 105 were carried on the theory that dropping them silenced the
-- activity stream. The capture rules that out: the reference app never sends
-- either, and its activity stream works. Both are account-scoped and inactive
-- in the reference account, so nothing on the watch answers to them.
--
-- The seeded defaults no longer include them, but a device seeded before this
-- keeps its rows, and the set is only ever written whole.
DELETE FROM feature WHERE feature_id IN (100, 105);
