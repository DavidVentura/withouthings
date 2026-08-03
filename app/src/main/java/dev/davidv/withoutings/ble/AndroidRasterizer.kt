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
            drawable.setTint(Color.WHITE)
            drawable.setBounds(0, 0, w, h)
            drawable.draw(canvas)
        }
    }

    override fun activityGlyph(activity: UInt, width: UByte, height: UByte): Bitmap {
        val res = ACTIVITY_GLYPHS[activity]
        if (res == null) {
            Log.w(TAG, "no glyph for activity $activity")
            return empty()
        }
        val drawable = ContextCompat.getDrawable(context, res) ?: return empty()
        Log.i(TAG, "activity $activity glyph at ${width.toInt()}x${height.toInt()}")
        return draw(width.toInt(), height.toInt()) { canvas, w, h ->
            drawable.setTint(Color.WHITE)
            drawable.setBounds(0, 0, w, h)
            drawable.draw(canvas)
        }
    }

    private fun fitTextSize(paint: Paint, text: String, width: Int, height: Int) {
        paint.textSize = PROBE_TEXT_SIZE
        val metrics = paint.fontMetrics
        val box = metrics.descent - metrics.ascent
        val advance = paint.measureText(text)
        if (box <= 0f || advance <= 0f) return
        val byHeight = PROBE_TEXT_SIZE * height / box
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

    private fun empty() = Bitmap(width = 0u, height = 0u, pixels = emptyList())

    private companion object {
        const val TAG = "Rasterizer"
        const val PROBE_TEXT_SIZE = 100f
    }
}
