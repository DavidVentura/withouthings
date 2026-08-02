-- ECG timestamps were once written in seconds while the rest of the schema
-- used milliseconds. Anything below this is a date in 1970, so it is the old
-- form.
UPDATE ecg SET measured_at = measured_at * 1000
 WHERE measured_at > 0 AND measured_at < 100000000000;
