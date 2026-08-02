-- Seconds the reading covers: a minute for an aggregated series, ~37 for an
-- HRV burst. Null for instants and for rows written before it was read.
ALTER TABLE sample ADD COLUMN window_secs INTEGER;

-- `VasistasCbt.attrib` for temperature: 1 normal, 2 asleep, 3 workout,
-- 4 night measure. Null where the stream annotates nothing, and on rows
-- written before it was read, which is not the same thing and cannot be
-- recovered now.
ALTER TABLE sample ADD COLUMN context INTEGER;
