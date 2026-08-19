package dev.davidv.withoutings.ipc

const val ACTION_START_WORKOUT = "dev.davidv.withoutings.action.START_WORKOUT"
const val ACTION_STOP_WORKOUT = "dev.davidv.withoutings.action.STOP_WORKOUT"

/** An activity id as listed by the watch service: 6 is Cycling, 16 is Weights. */
const val EXTRA_ACTIVITY = "dev.davidv.withoutings.extra.ACTIVITY"

/**
 * What became of a request. The code goes back to senders that used
 * `sendOrderedBroadcast`; the rest never see it. None of them is zero or -1,
 * which are the codes an ordered broadcast carries before anyone has answered.
 */
enum class WorkoutOutcome(val code: Int) {
    Accepted(1),
    UnknownAction(2),
    UnknownActivity(3),
    NoWatch(4),
    AlreadyRunning(5),
    NotRunning(6),
    Failed(7),
}

@JvmInline value class ActivityId(val value: UInt)

sealed interface WorkoutCommand {
    data class Start(val activity: ActivityId) : WorkoutCommand

    data object Stop : WorkoutCommand
}

sealed interface WorkoutRequest {
    data class Understood(val command: WorkoutCommand) : WorkoutRequest

    data class Refused(val outcome: WorkoutOutcome) : WorkoutRequest
}

fun parseWorkoutRequest(action: String?, activity: Int?, known: Set<UInt>): WorkoutRequest {
    if (action == ACTION_STOP_WORKOUT) return WorkoutRequest.Understood(WorkoutCommand.Stop)
    if (action != ACTION_START_WORKOUT) {
        return WorkoutRequest.Refused(WorkoutOutcome.UnknownAction)
    }
    if (activity == null || activity < 0 || activity.toUInt() !in known) {
        return WorkoutRequest.Refused(WorkoutOutcome.UnknownActivity)
    }
    return WorkoutRequest.Understood(WorkoutCommand.Start(ActivityId(activity.toUInt())))
}
