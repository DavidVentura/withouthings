package dev.davidv.withoutings.ui

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.foundation.gestures.waitForUpOrCancellation
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.Info
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.input.pointer.PointerEventPass
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.rotate
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.input.pointer.positionChange
import androidx.compose.ui.layout.Layout
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Constraints
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import dev.davidv.withoutings.LinkState
import dev.davidv.withoutings.ui.theme.AppTheme
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.floor
import kotlin.math.hypot
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sin
import uniffi.wpp_ffi.ClockSetState
import uniffi.wpp_ffi.HandsPhase
import uniffi.wpp_ffi.HandsSession
import uniffi.wpp_ffi.WatchClockView
import uniffi.wpp_ffi.WatchHand

/**
 * Hand steps per degree the dial is turned. The watch has 360 steps a turn,
 * so at one the hand follows the dial exactly: a full turn of the dial is a
 * full turn of the hand.
 */
private const val STEPS_PER_DEGREE = 1f

/** Turned steps are gathered and handed over this often, never per pixel. */
private const val DIAL_FLUSH_MS = 120L

/** A light phone tick every this many degrees (one minute on the minute hand). */
private const val TICK_DEGREES = 6f

/** Anything still unanswered this long is shown as a failure with Retry. */
private const val STUCK_AFTER_MS = 10_000L

private const val REPEAT_AFTER_MS = 420L
private const val REPEAT_EVERY_MS = 110L

/**
 * Puts the watch's hands back on the dial. The session opens as the screen
 * does: the watch swings its hands to where it believes 12 o'clock is, and
 * turning the dial here moves the chosen hand from there, the hand following
 * the dial degree for degree. Done keeps the new 12; leaving any other way
 * puts the old one back.
 */
@Composable
fun SetHandsScreen(
    link: LinkState,
    session: HandsSession?,
    onStart: () -> Unit,
    onNudge: (WatchHand, Int) -> Unit,
    onSave: () -> Unit,
    onCancel: () -> Unit,
    onLeave: () -> Unit,
    onBack: () -> Unit,
    clock: WatchClockView? = null,
    clockSetAsked: Boolean = false,
    onSetClock: () -> Unit = {},
    onSetCustomClock: (Long) -> Unit = {},
    onReadClock: () -> Unit = {},
    now: () -> Long = System::currentTimeMillis,
    onOpen: () -> Unit = {},
) {
    val phase = session?.phase
    var hand by rememberSaveable { mutableStateOf(WatchHand.MINUTE) }
    var details by rememberSaveable { mutableStateOf(false) }
    var savedHere by remember { mutableStateOf(false) }
    var attempt by remember { mutableIntStateOf(0) }
    var stuck by remember { mutableStateOf(false) }
    // Hands are only adjusted once asked for: until then (and again after
    // Done or Cancel) the time can be set without a session holding it.
    // `asked` covers the gap between the tap and the session's first answer.
    var asked by remember { mutableStateOf(false) }
    val known = phase != null && phase != HandsPhase.IDLE
    LaunchedEffect(known) { if (known) asked = false }
    val engaged = asked || phase == HandsPhase.WAITING_FOR_WATCH || phase == HandsPhase.STARTING ||
        phase == HandsPhase.ADJUSTING || phase == HandsPhase.SAVING || phase == HandsPhase.CANCELLING

    // A session belongs to this visit and is undone unless saved however the
    // screen goes away.
    LaunchedEffect(Unit) { onOpen() }
    DisposableEffect(Unit) { onDispose(onLeave) }
    val lifecycle = LocalLifecycleOwner.current.lifecycle
    val cancel by rememberUpdatedState(onCancel)
    val saved by rememberUpdatedState(savedHere)
    DisposableEffect(lifecycle) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_STOP && !saved) cancel()
        }
        lifecycle.addObserver(observer)
        onDispose { lifecycle.removeObserver(observer) }
    }

    // Never sit on "starting" for ever: whatever the reason (no service, a
    // watch that never links), ten seconds without a session is an error.
    val starting = asked || phase == HandsPhase.WAITING_FOR_WATCH || phase == HandsPhase.STARTING
    LaunchedEffect(starting, attempt) {
        stuck = false
        if (starting) {
            delay(STUCK_AFTER_MS)
            stuck = true
        }
    }
    // Saved, cancelled or lost, the session is over and the screen is back
    // to idle, with the outcome in the subtitle.

    val active = engaged && !stuck
    val adjustable = active && (phase == HandsPhase.ADJUSTING || phase == HandsPhase.STARTING)
    val recheck = clockSetAsked && clock?.setState == ClockSetState.SET && !engaged
    val begin = {
        stuck = false
        savedHere = false
        asked = true
        attempt += 1
        onStart()
    }

    DetailScaffold(
        title = "Set time and align hands",
        subtitle = when {
            stuck -> "No answer · try again"
            known -> statusLine(phase, false, link, session)
            asked -> "Starting…"
            link == LinkState.Ready -> "Hands not being adjusted"
            else -> "Watch connecting…"
        },
        onBack = onBack,
        gap = 0.dp,
        trailing = {
            GlyphButton(Icons.Rounded.Info, "How this works") { details = !details }
        },
    ) {
        // The dial gets whatever height the other controls leave, within
        // limits; when even its smallest size won't fit, the page scrolls.
        BoxWithConstraints(Modifier.weight(1f).fillMaxWidth()) {
            DialLayout(
                room = constraints.maxHeight,
                top = {
                    Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                        AnimatedVisibility(details) { Details(clock, now) }
                        WatchClockCard(
                            clock = clock,
                            setAskedHere = clockSetAsked,
                            canSet = true,
                            onSet = onSetClock,
                            onSetCustom = onSetCustomClock,
                            onRefresh = onReadClock,
                            now = now,
                        )
                        Segmented(
                            listOf(
                                WatchHand.MINUTE to "Minute",
                                WatchHand.HOUR to "Hour",
                                WatchHand.ACTIVITY to "Activity",
                            ),
                            hand,
                            modifier = Modifier.inert(!active),
                            onSelect = { if (active) hand = it },
                        )
                        Text(
                            if (active) instruction(hand) else "Tap Adjust hands to move them",
                            Modifier.fillMaxWidth(),
                            style = AppTheme.type.rowMeta,
                            color = AppTheme.colors.onSurfaceTertiary,
                            textAlign = TextAlign.Center,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                        )
                    }
                },
                dial = {
                    HandDial(
                        enabled = adjustable,
                        hand = hand,
                        session = session,
                        onSteps = { onNudge(hand, it) },
                        modifier = Modifier.fillMaxSize(),
                    )
                },
                bottom = {
                    Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                        Row(
                            Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.spacedBy(16.dp, Alignment.CenterHorizontally),
                        ) {
                            StepPill("−1°", adjustable) { onNudge(hand, -1) }
                            StepPill("+1°", adjustable) { onNudge(hand, 1) }
                        }
                        if (!engaged || stuck) {
                            FilledAction(
                                when {
                                    stuck -> "Try again"
                                    recheck -> "Check hands again"
                                    else -> "Adjust hands"
                                },
                                Modifier.fillMaxWidth(),
                                shape = AppTheme.pill,
                                container = MaterialTheme.colorScheme.primaryContainer,
                                content = MaterialTheme.colorScheme.onPrimaryContainer,
                                onClick = begin,
                            )
                        }
                        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                            OutlineAction(
                                "Cancel",
                                Modifier.weight(1f).padding(vertical = 2.dp),
                                enabled = engaged && (adjustable || phase == HandsPhase.WAITING_FOR_WATCH || stuck),
                                onClick = {
                                    onCancel()
                                    if (stuck) {
                                        stuck = false
                                        asked = false
                                    }
                                },
                            )
                            FilledAction(
                                "Done",
                                Modifier.weight(1f),
                                shape = AppTheme.pill,
                                enabled = adjustable && phase == HandsPhase.ADJUSTING,
                                onClick = {
                                    savedHere = true
                                    onSave()
                                },
                            )
                        }
                    }
                },
            )
        }
    }
}

/** Greys out and swallows every touch when [inert]. */
private fun Modifier.inert(inert: Boolean): Modifier =
    if (!inert) this else this
        .alpha(0.38f)
        .pointerInput(Unit) {
            awaitPointerEventScope {
                while (true) {
                    awaitPointerEvent(PointerEventPass.Initial).changes.forEach { it.consume() }
                }
            }
        }

private val DIAL_MIN = 140.dp
private val DIAL_MAX = 320.dp
private val DIAL_GAP = 12.dp

/**
 * Top controls, the dial, bottom controls. Everything but the dial is
 * measured first; the dial is a square of what is left (at most most of the
 * width and [DIAL_MAX], at least [DIAL_MIN]). The bottom controls sit at the
 * bottom of [room]; if the minimum dial doesn't fit, the whole thing grows
 * past [room] and scrolls instead of anything overlapping.
 */
@Composable
private fun DialLayout(
    room: Int,
    top: @Composable () -> Unit,
    dial: @Composable () -> Unit,
    bottom: @Composable () -> Unit,
) {
    Layout(
        contents = listOf(top, dial, bottom),
        modifier = Modifier.fillMaxWidth().verticalScroll(rememberScrollState()),
    ) { (topM, dialM, bottomM), constraints ->
        val width = constraints.maxWidth
        val loose = Constraints(maxWidth = width)
        val t = topM.map { it.measure(loose) }
        val b = bottomM.map { it.measure(loose) }
        val topH = t.maxOfOrNull { it.height } ?: 0
        val bottomH = b.maxOfOrNull { it.height } ?: 0
        val gap = DIAL_GAP.roundToPx()
        val cap = min((width * 0.86f).toInt(), DIAL_MAX.roundToPx())
        val least = min(DIAL_MIN.roundToPx(), cap)
        val size = (room - topH - bottomH - 2 * gap).coerceIn(least, cap)
        val d = dialM.map { it.measure(Constraints.fixed(size, size)) }
        val height = max(room, topH + size + bottomH + 2 * gap)
        layout(width, height) {
            t.forEach { it.place(0, 0) }
            val free = height - topH - bottomH - size
            d.forEach { it.place((width - size) / 2, topH + free / 2) }
            b.forEach { it.place(0, height - bottomH) }
        }
    }
}

/**
 * A knob turned with one finger anywhere on its face. The angle swept is
 * measured around its centre, so either direction works and a full circle is
 * a full turn of the hand; clockwise on the phone is clockwise on the watch.
 */
@Composable
private fun HandDial(
    enabled: Boolean,
    hand: WatchHand,
    session: HandsSession?,
    onSteps: (Int) -> Unit,
    modifier: Modifier = Modifier,
) {
    val haptics = LocalHapticFeedback.current
    val steps by rememberUpdatedState(onSteps)
    var rotation by remember { mutableFloatStateOf(0f) }
    var turning by remember { mutableStateOf(false) }
    var pending by remember { mutableIntStateOf(0) }
    var carry by remember { mutableFloatStateOf(0f) }

    // Steps are handed over in batches; the core then sends one move at a
    // time and waits for the watch, so a fast spin never floods the link.
    LaunchedEffect(Unit) {
        while (true) {
            delay(DIAL_FLUSH_MS)
            val batch = pending
            if (batch != 0) {
                pending = 0
                steps(batch)
            }
        }
    }

    val ring = if (enabled) MaterialTheme.colorScheme.primaryContainer else MaterialTheme.colorScheme.surfaceContainerHigh
    val face = MaterialTheme.colorScheme.surfaceContainer
    val mark = if (enabled) MaterialTheme.colorScheme.onPrimaryContainer else AppTheme.colors.onSurfaceDim
    val accent = if (enabled) MaterialTheme.colorScheme.primary else AppTheme.colors.onSurfaceDim
    val outline = if (turning) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.outlineVariant

    Box(
        modifier
            .clip(CircleShape)
            .pointerInput(enabled) {
                if (!enabled) return@pointerInput
                awaitEachGesture {
                    val down = awaitFirstDown(requireUnconsumed = false)
                    turning = true
                    val centre = Offset(size.width / 2f, size.height / 2f)
                    val dead = size.width * 0.12f
                    var last = down.position
                    while (true) {
                        val event = awaitPointerEvent()
                        val change = event.changes.firstOrNull { it.id == down.id } ?: break
                        if (!change.pressed) break
                        val now = change.position
                        if (change.positionChange() != Offset.Zero) change.consume()
                        val before = last - centre
                        val after = now - centre
                        last = now
                        // Near the centre a tiny slip is a huge angle; ignore it.
                        if (hypot(after.x, after.y) < dead || hypot(before.x, before.y) < dead) continue
                        var delta = Math.toDegrees(
                            (atan2(after.y, after.x) - atan2(before.y, before.x)).toDouble()
                        ).toFloat()
                        if (delta > 180f) delta -= 360f
                        if (delta < -180f) delta += 360f
                        val ticksBefore = floor(rotation / TICK_DEGREES)
                        rotation += delta
                        if (floor(rotation / TICK_DEGREES) != ticksBefore) {
                            haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                        }
                        carry += delta * STEPS_PER_DEGREE
                        val whole = carry.toInt()
                        if (whole != 0) {
                            carry -= whole
                            pending += whole
                        }
                    }
                    turning = false
                }
            },
        contentAlignment = Alignment.Center,
    ) {
        Canvas(Modifier.fillMaxSize()) {
            val r = size.minDimension / 2f
            drawCircle(ring, r)
            drawCircle(outline, r - 1.dp.toPx(), style = Stroke(2.dp.toPx()))
            drawCircle(face, r * 0.62f)
            rotate(rotation) {
                for (i in 0 until 60) {
                    val a = (i * 6f - 90f) * (PI / 180f).toFloat()
                    val long = i % 5 == 0
                    val outer = r * 0.93f
                    val inner = if (long) r * 0.76f else r * 0.83f
                    drawLine(
                        mark.copy(alpha = if (long) 0.9f else 0.45f),
                        Offset(center.x + cos(a) * inner, center.y + sin(a) * inner),
                        Offset(center.x + cos(a) * outer, center.y + sin(a) * outer),
                        strokeWidth = if (long) 3.dp.toPx() else 1.5.dp.toPx(),
                        cap = StrokeCap.Round,
                    )
                }
                // The grip: where your finger was, it stays.
                drawCircle(accent, r * 0.075f, Offset(center.x, center.y - r * 0.69f))
            }
        }
        // The centre text scales with the dial, not the font setting, so it
        // always fits the face; small dials keep just the number.
        BoxWithConstraints(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            val d = min(maxWidth.value, maxHeight.value).dp
            val density = LocalDensity.current
            val big = with(density) { (d * 0.15f).toSp() }
            val small = with(density) { (d * 0.045f).coerceIn(9.dp, 13.dp).toSp() }
            val roomy = d >= 200.dp
            val moved = movedSteps(hand, session)
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                if (roomy) {
                    Text(
                        handName(hand),
                        style = AppTheme.type.eyebrow.copy(fontSize = small, lineHeight = small * 1.3f),
                        color = AppTheme.colors.onSurfaceTertiary,
                        maxLines = 1,
                    )
                }
                Text(
                    signed(moved) + "°",
                    style = AppTheme.type.focalLarge.copy(
                        fontSize = big,
                        lineHeight = big * 1.15f,
                        letterSpacing = 0.sp,
                        fontWeight = FontWeight.SemiBold,
                    ),
                    color = MaterialTheme.colorScheme.onSurface,
                    maxLines = 1,
                )
                if (roomy) {
                    Text(
                        timeEquivalent(hand, moved),
                        style = AppTheme.type.rowMeta.copy(fontSize = small, lineHeight = small * 1.3f),
                        color = AppTheme.colors.onSurfaceTertiary,
                        textAlign = TextAlign.Center,
                        maxLines = 1,
                    )
                }
            }
        }
    }
}

@Composable
private fun StepPill(label: String, enabled: Boolean, onStep: () -> Unit) {
    val haptics = LocalHapticFeedback.current
    val scope = rememberCoroutineScope()
    val step by rememberUpdatedState(onStep)
    var pressed by remember { mutableStateOf(false) }
    Box(
        Modifier
            .size(width = 64.dp, height = 44.dp)
            .clip(AppTheme.pill)
            .background(
                when {
                    !enabled -> MaterialTheme.colorScheme.surfaceContainerHigh
                    pressed -> MaterialTheme.colorScheme.primary
                    else -> MaterialTheme.colorScheme.surfaceContainerHigh
                }
            )
            .pointerInput(enabled) {
                if (!enabled) return@pointerInput
                awaitEachGesture {
                    awaitFirstDown()
                    pressed = true
                    haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                    step()
                    val repeat = scope.launch {
                        delay(REPEAT_AFTER_MS)
                        while (true) {
                            step()
                            delay(REPEAT_EVERY_MS)
                        }
                    }
                    waitForUpOrCancellation()
                    repeat.cancel()
                    pressed = false
                }
            },
        contentAlignment = Alignment.Center,
    ) {
        Text(
            label,
            style = AppTheme.type.buttonLabel.copy(fontSize = 17.sp, fontWeight = FontWeight.SemiBold),
            color = when {
                !enabled -> AppTheme.colors.onSurfaceDim
                pressed -> MaterialTheme.colorScheme.onPrimary
                else -> MaterialTheme.colorScheme.onSurfaceVariant
            },
        )
    }
}

private fun instruction(hand: WatchHand) = when (hand) {
    WatchHand.MINUTE -> "Turn until the minute hand is at 12"
    WatchHand.HOUR -> "Turn until the hour hand is at 12"
    WatchHand.ACTIVITY -> "Turn until the activity hand is at 0%"
}

@Composable
private fun Details(clock: WatchClockView?, now: () -> Long) {
    val lines = listOf(
        "Adjust hands, bring all three to 12 (activity 0%), then Done",
        "Cancel or Back keeps the old setting",
        "1° = 10 s on the minute hand, 2 min on the hour hand",
        "The crown may move the last hand moved",
        "Drifts after charging? Use the Withings charger, no magnets",
    ) + clockDetailLines(clock, now())
    Column(
        Modifier
            .fillMaxWidth()
            .border(1.dp, MaterialTheme.colorScheme.outlineVariant, RoundedCornerShape(AppTheme.radius.card))
            .padding(horizontal = 12.dp, vertical = 8.dp),
        verticalArrangement = Arrangement.spacedBy(3.dp),
    ) {
        lines.forEach { line ->
            Text("· $line", style = AppTheme.type.rowMeta, color = AppTheme.colors.onSurfaceTertiary)
        }
    }
}

private fun statusLine(phase: HandsPhase?, stuck: Boolean, link: LinkState, session: HandsSession?): String =
    when {
        stuck -> "No answer — tap Retry"
        else -> when (phase) {
            null, HandsPhase.IDLE -> "Starting…"
            HandsPhase.WAITING_FOR_WATCH ->
                if (link == LinkState.Ready) "Watch connecting…" else "Waiting for the watch…"
            HandsPhase.TIMED_OUT -> "Watch not reachable"
            HandsPhase.STARTING -> "Hands moving to 12…"
            HandsPhase.ADJUSTING -> if ((session?.pendingSteps ?: 0) != 0) "Moving…" else "Ready"
            HandsPhase.SAVING -> "Saving…"
            HandsPhase.SAVED -> "Saved"
            HandsPhase.CANCELLING -> "Restoring…"
            HandsPhase.CANCELLED -> "Cancelled · old setting kept"
            HandsPhase.INTERRUPTED -> "Disconnected · not saved"
            HandsPhase.FAILED -> "Refused (${session?.failedCommand}/${session?.failedErr})"
        }
    }

private fun movedSteps(hand: WatchHand, session: HandsSession?): Int = when (hand) {
    WatchHand.MINUTE -> session?.minuteSteps
    WatchHand.HOUR -> session?.hourSteps
    WatchHand.ACTIVITY -> session?.activitySteps
} ?: 0

private fun handName(hand: WatchHand) = when (hand) {
    WatchHand.MINUTE -> "minute hand"
    WatchHand.HOUR -> "hour hand"
    WatchHand.ACTIVITY -> "activity hand"
}

private fun signed(value: Int): String = when {
    value > 0 -> "+$value"
    value < 0 -> "−${abs(value)}"
    else -> "0"
}

private fun timeEquivalent(hand: WatchHand, steps: Int): String {
    if (steps == 0) return "turn either way"
    val sign = if (steps > 0) "+" else "−"
    val n = abs(steps)
    return when (hand) {
        WatchHand.MINUTE -> if (n * 10 < 60) "$sign${n * 10} s" else "$sign${n * 10 / 60} min ${n * 10 % 60} s"
        WatchHand.HOUR -> if (n * 2 < 60) "$sign${n * 2} min" else "$sign${n * 2 / 60} h ${n * 2 % 60} min"
        WatchHand.ACTIVITY -> "clockwise is +"
    }.replace(" 0 s", "").replace(" 0 min", "")
}
