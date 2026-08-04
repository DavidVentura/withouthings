package dev.davidv.withoutings.ui

import uniffi.wpp_ffi.Marker

// The store anchors its downsample buckets at the start of the range it is
// given, so shifting that range at all reshuffles every point it returns. Loads
// are snapped and padded past the screen to keep a pan or a pinch on the set
// already in hand.
private const val LOAD_BUCKETS = 3600L
private const val PAD_FACTOR = 3L
private const val MIN_SPAN_MS = 10_000L

// The store emits the minimum and the maximum of each bucket.
val LOAD_POINTS: UInt = (LOAD_BUCKETS * 2).toUInt()

@JvmInline
value class LoadWindow(val range: LongRange) {
    val spanMs: Long get() = range.last - range.first

    fun covers(window: LongRange): Boolean =
        window.first >= range.first && window.last <= range.last
}

data class MetricSeries(
    val style: MetricStyle,
    val load: LoadWindow,
    val points: List<ChartPoint> = emptyList(),
    val charging: List<Marker> = emptyList(),
)

fun loadWindow(visible: LongRange): LoadWindow {
    val span = (visible.last - visible.first).coerceAtLeast(MIN_SPAN_MS)
    val padded = ceilPowerOfTwo(span * PAD_FACTOR)
    val bucketMs = (padded / LOAD_BUCKETS).coerceAtLeast(1L)
    val centre = visible.first + span / 2
    val first = Math.floorDiv(centre - padded / 2, bucketMs) * bucketMs
    return LoadWindow(first..(first + padded))
}

fun reloadFor(loaded: LoadWindow, visible: LongRange): LoadWindow? {
    val want = loadWindow(visible)
    if (loaded.spanMs == want.spanMs && loaded.covers(visible)) return null
    return want
}

private fun ceilPowerOfTwo(value: Long): Long {
    if (value <= 1) return 1
    return java.lang.Long.highestOneBit(value - 1) shl 1
}
