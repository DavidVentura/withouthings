package dev.davidv.withoutings.ble

import android.content.Context
import android.graphics.Bitmap as AndroidBitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.util.Log
import androidx.core.content.ContextCompat
import uniffi.wpp_ffi.Bitmap
import uniffi.wpp_ffi.Rasterizer

/**
 * Draws what the watch cannot draw itself.
 *
 * The watch has one font and no app icons, so anything outside that — an
 * emoji, a script its font does not cover, the icon beside a notification —
 * comes back to the phone as a request for a picture at a named size.
 *
 * Everything here renders white on a transparent canvas. Rust reduces that to
 * one bit per pixel on an alpha-weighted threshold, so an antialiased edge
 * falls away and the stroke survives.
 */
class AndroidRasterizer(private val context: Context) : Rasterizer {

    override fun glyph(codepoint: UInt, width: UByte, height: UByte): Bitmap {
        // The codepoint came off the wire, so it need not be one: toChars
        // throws on anything above the Unicode range, and an exception thrown
        // back through the callback takes the process with it.
        val text = runCatching { String(Character.toChars(codepoint.toInt())) }.getOrNull()
        if (text == null) {
            Log.w(TAG, "not a codepoint: $codepoint")
            return empty()
        }
        if (text.isBlank()) return empty()
        Log.i(TAG, "glyph U+%04X at %dx%d".format(codepoint.toInt(), width.toInt(), height.toInt()))
        return draw(width.toInt(), height.toInt()) { canvas, w, _ ->
            val paint = Paint().apply {
                isAntiAlias = true
                color = Color.WHITE
            }
            fitTextSize(paint, text, w, height.toInt())
            // Sit on the baseline of a line box the height of the cell, and
            // centre it, which is what the watch does with the characters it
            // draws itself. A capital fills roughly two thirds of a line box,
            // so anything that puts ink in every row comes out taller than the
            // letters beside it.
            val metrics = paint.fontMetrics
            val x = (w - paint.measureText(text)) / 2f
            canvas.drawText(text, x, -metrics.ascent, paint)
        }
    }

    override fun icon(appId: String, width: UByte, height: UByte): Bitmap {
        Log.i(TAG, "icon for $appId at ${width.toInt()}x${height.toInt()}")
        val drawable = runCatching {
            context.packageManager.getApplicationIcon(appId)
        }.getOrNull() ?: runCatching {
            // The test notification varies its app id to defeat the watch's
            // icon cache, so the id it asks about is not an installed package.
            // Anything under our own name gets our own icon.
            if (appId.startsWith(context.packageName)) {
                context.packageManager.getApplicationIcon(context.packageName)
            } else {
                null
            }
        }.getOrNull()
        if (drawable == null) {
            Log.i(TAG, "no icon for $appId")
            return empty()
        }
        return draw(width.toInt(), height.toInt()) { canvas, w, h ->
            // The watch's screen is one bit deep; tinting first is what decides
            // which parts of a colour icon survive the threshold.
            drawable.setTint(Color.WHITE)
            drawable.setBounds(0, 0, w, h)
            drawable.draw(canvas)
        }
    }

    override fun activityGlyph(activity: UInt, width: UByte, height: UByte): Bitmap {
        val res = ACTIVITY_GLYPHS[activity]
        if (res == null) {
            // A menu entry with no glyph would still take a slot on the watch
            // and show nothing in it, so this is worth seeing in the log.
            Log.w(TAG, "no glyph for activity $activity")
            return empty()
        }
        val drawable = ContextCompat.getDrawable(context, res) ?: return empty()
        Log.i(TAG, "activity $activity glyph at ${width.toInt()}x${height.toInt()}")
        return draw(width.toInt(), height.toInt()) { canvas, w, h ->
            // Same reasoning as an app icon: the watch's screen is one bit
            // deep, so tint before drawing and let the threshold do the rest.
            drawable.setTint(Color.WHITE)
            drawable.setBounds(0, 0, w, h)
            drawable.draw(canvas)
        }
    }

    /**
     * The text size at which a line box fills the cell, found by scaling a
     * measurement rather than searching: both measurements are linear in it.
     *
     * Measured ascender to descender, not on the character's own ink. The
     * watch sizes the cell for a line of its own text, so matching the line
     * box is what makes the result sit at the same height as the letters
     * around it; matching the ink overshoots, because a capital is only part
     * of a line box. Leading is left out — including it renders a shade small.
     */
    private fun fitTextSize(paint: Paint, text: String, width: Int, height: Int) {
        paint.textSize = PROBE_TEXT_SIZE
        val metrics = paint.fontMetrics
        val box = metrics.descent - metrics.ascent
        val advance = paint.measureText(text)
        if (box <= 0f || advance <= 0f) return
        val byHeight = PROBE_TEXT_SIZE * height / box
        // Wide characters still have to fit across the cell.
        val byWidth = PROBE_TEXT_SIZE * width / advance
        paint.textSize = minOf(byHeight, byWidth)
    }

    private fun draw(width: Int, height: Int, block: (Canvas, Int, Int) -> Unit): Bitmap {
        if (width <= 0 || height <= 0) return empty()
        val bitmap = AndroidBitmap.createBitmap(width, height, AndroidBitmap.Config.ARGB_8888)
        block(Canvas(bitmap), width, height)

        val pixels = IntArray(width * height)
        bitmap.getPixels(pixels, 0, width, 0, 0, width, height)
        bitmap.recycle()
        return Bitmap(
            width = width.toUByte(),
            height = height.toUByte(),
            pixels = pixels.map { it.toUInt() },
        )
    }

    /** Nothing to draw. Rust turns this into an empty image on the wire. */
    private fun empty() = Bitmap(width = 0u, height = 0u, pixels = emptyList())

    private companion object {
        const val TAG = "Rasterizer"
        /** Any size works; the result is scaled from what it measures. */
        const val PROBE_TEXT_SIZE = 100f
    }
}
