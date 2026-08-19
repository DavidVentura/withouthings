package dev.davidv.withoutings.ipc

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import dev.davidv.withoutings.LinkState
import dev.davidv.withoutings.WatchRepository
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import uniffi.wpp_ffi.WatchService

private const val TAG = "WorkoutRequest"

/**
 * Lets any app on the phone put the watch into a workout, so that whatever knows
 * a session has begun - a bike hire, a gym app - can say so without a human
 * finding the phone and the sheet.
 */
class WorkoutRequestReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action
        val activity =
            if (intent.hasExtra(EXTRA_ACTIVITY)) intent.getIntExtra(EXTRA_ACTIVITY, -1) else null
        val ordered = isOrderedBroadcast
        val pending = goAsync()
        CoroutineScope(Dispatchers.IO).launch {
            val outcome =
                runCatching { serve(action, activity) }
                    .onFailure { Log.w(TAG, "$action", it) }
                    .getOrDefault(WorkoutOutcome.Failed)
            Log.i(TAG, "$action ${activity ?: ""} -> $outcome")
            if (ordered) pending.setResultCode(outcome.code)
            pending.finish()
        }
    }

    private fun serve(action: String?, activity: Int?): WorkoutOutcome {
        val service = WatchRepository.get()
        if (service == null || WatchRepository.link.value != LinkState.Ready) {
            return WorkoutOutcome.NoWatch
        }
        val known = service.activities().map { it.id }.toSet()
        return when (val request = parseWorkoutRequest(action, activity, known)) {
            is WorkoutRequest.Refused -> request.outcome
            is WorkoutRequest.Understood -> obey(request.command, service)
        }
    }

    private fun obey(command: WorkoutCommand, service: WatchService): WorkoutOutcome {
        val active = service.snapshot().activeWorkout
        return when (command) {
            is WorkoutCommand.Start -> {
                if (active != null) return WorkoutOutcome.AlreadyRunning
                service.startWorkout(command.activity.value)
                WorkoutOutcome.Accepted
            }
            WorkoutCommand.Stop -> {
                if (active == null) return WorkoutOutcome.NotRunning
                service.stopWorkout()
                WorkoutOutcome.Accepted
            }
        }
    }
}
