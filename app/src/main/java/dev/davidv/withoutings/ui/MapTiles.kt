package dev.davidv.withoutings.ui

import android.content.Context
import android.graphics.BitmapFactory
import android.util.Log
import android.util.LruCache
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import uniffi.wpp_ffi.MapTile

/**
 * Raster tiles for the map, off the disk where they have been seen before and
 * off the network where they have not. Tiles are the one thing this app
 * fetches, so a route is drawn without them until the setting is switched on.
 *
 * The cache directory is deliberately the disposable one: a tile can always be
 * fetched again, unlike anything in the database next to it.
 */
class TileSource(context: Context, private val template: String) {

    private val context = context.applicationContext
    private val held = object : LruCache<String, ImageBitmap>(MEMORY_BYTES) {
        override fun sizeOf(key: String, value: ImageBitmap) = value.width * value.height * 4
    }

    // A server that refused once will refuse the same tile again for a while,
    // and a map that keeps asking turns one failure into a request per frame.
    private val refused = HashMap<String, Long>()

    fun cached(tile: MapTile): ImageBitmap? = held.get(key(tile))

    suspend fun fetch(tile: MapTile): ImageBitmap? {
        val key = key(tile)
        held.get(key)?.let { return it }

        val since = refused[key]
        if (since != null && System.currentTimeMillis() - since < REFUSED_FOR_MS) return null

        val loaded = withContext(Dispatchers.IO) { read(tile, key) }
        if (loaded == null) {
            refused[key] = System.currentTimeMillis()
            return null
        }
        refused.remove(key)
        held.put(key, loaded)
        return loaded
    }

    private fun read(tile: MapTile, key: String): ImageBitmap? {
        val onDisk = File(context.cacheDir, "tiles/$key.png")
        if (onDisk.exists()) {
            val decoded = BitmapFactory.decodeFile(onDisk.path)
            if (decoded != null) return decoded.asImageBitmap()
            onDisk.delete()
        }

        val url = template
            .replace("{z}", tile.z.toString())
            .replace("{x}", tile.x.toString())
            .replace("{y}", tile.y.toString())
        val bytes = runCatching { download(url) }
            .onFailure { Log.w(TAG, "tile $key: $it") }
            .getOrNull()
            ?: return null

        val decoded = BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
        if (decoded == null) {
            Log.w(TAG, "tile $key came back as something that is not an image")
            return null
        }
        onDisk.parentFile?.mkdirs()
        runCatching { onDisk.writeBytes(bytes) }
            .onFailure { Log.w(TAG, "could not keep tile $key", it) }
        return decoded.asImageBitmap()
    }

    private fun download(url: String): ByteArray? {
        val connection = URL(url).openConnection() as HttpURLConnection
        // OpenStreetMap's tile policy asks that a client identify itself, and
        // serves a refusal to one that does not.
        connection.setRequestProperty("User-Agent", USER_AGENT)
        connection.connectTimeout = TIMEOUT_MS
        connection.readTimeout = TIMEOUT_MS
        try {
            if (connection.responseCode != HttpURLConnection.HTTP_OK) {
                Log.w(TAG, "tile server answered ${connection.responseCode} for $url")
                return null
            }
            return connection.inputStream.use { it.readBytes() }
        } finally {
            connection.disconnect()
        }
    }

    private fun key(tile: MapTile) = "${tile.z}/${tile.x}/${tile.y}"

    companion object {
        private const val TAG = "Tiles"
        private const val MEMORY_BYTES = 32 * 1024 * 1024
        private const val TIMEOUT_MS = 10_000
        private const val REFUSED_FOR_MS = 60_000L
        private const val USER_AGENT =
            "Withoutings/1.0 (+https://github.com/DavidVentura/withouthings)"
    }
}
