-- Where a second frame starts inside this one, when it does: the mark of a
-- notification that went missing rather than a frame we cannot read.
ALTER TABLE undecoded_frame ADD COLUMN splice_at INTEGER;
