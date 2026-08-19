package dev.davidv.withoutings.ipc

import org.junit.Assert.assertEquals
import org.junit.Test

private val KNOWN = setOf(1u, 6u, 16u)

class WorkoutIntentTest {

    @Test
    fun `a known activity starts`() {
        assertEquals(
            WorkoutRequest.Understood(WorkoutCommand.Start(ActivityId(6u))),
            parseWorkoutRequest(ACTION_START_WORKOUT, 6, KNOWN),
        )
    }

    @Test
    fun `stopping needs no activity`() {
        assertEquals(
            WorkoutRequest.Understood(WorkoutCommand.Stop),
            parseWorkoutRequest(ACTION_STOP_WORKOUT, null, KNOWN),
        )
    }

    @Test
    fun `an activity the watch does not have is refused rather than substituted`() {
        assertEquals(
            WorkoutRequest.Refused(WorkoutOutcome.UnknownActivity),
            parseWorkoutRequest(ACTION_START_WORKOUT, 999, KNOWN),
        )
    }

    @Test
    fun `starting without an activity is refused`() {
        assertEquals(
            WorkoutRequest.Refused(WorkoutOutcome.UnknownActivity),
            parseWorkoutRequest(ACTION_START_WORKOUT, null, KNOWN),
        )
    }

    /** An extra that is not an int reads back as the -1 default. */
    @Test
    fun `a negative activity is refused before it is made unsigned`() {
        assertEquals(
            WorkoutRequest.Refused(WorkoutOutcome.UnknownActivity),
            parseWorkoutRequest(ACTION_START_WORKOUT, -1, KNOWN),
        )
    }

    @Test
    fun `some other app's action is refused`() {
        assertEquals(
            WorkoutRequest.Refused(WorkoutOutcome.UnknownAction),
            parseWorkoutRequest("android.intent.action.VIEW", 6, KNOWN),
        )
    }
}
