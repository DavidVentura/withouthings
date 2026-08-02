-- The watch's own staging for the window: 0 awake, 1 light, 2 deep, 3 REM.
-- Stored as the wire value; `wpp::activity::SleepLevel` reads it.
ALTER TABLE activity_minute ADD COLUMN sleep_level INTEGER;
