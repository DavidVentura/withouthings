# Withings protocol app

This app handles the protocol for the Withings ScanWatch 2.

Very early in development.

Handles:

- Sensor data (Heart rate, temperature, breath rate, steps, charge state / level)
- Automatic activities (walking, running), segmentation is implemented locally instead of using the API so it's a bit different
- Manual activities (starting an activity from the watch), has a custom UI for the 'live exercise mode'
- ECG
- Most settings
- Notifications (only sends a test notification, does not forward system notifications)

## Screenshots

<table>
  <tr>
    <td><img src="screenshots/now.png" width="220" alt="Now"></td>
    <td><img src="screenshots/activities.png" width="220" alt="Activities"></td>
    <td><img src="screenshots/static_activity.png" width="220" alt="Activity detail"></td>
    <td><img src="screenshots/stats.png" width="220" alt="Stats"></td>
  </tr>
  <tr>
    <td><img src="screenshots/sleep.png" width="220" alt="Sleep"></td>
    <td><img src="screenshots/ecg.png" width="220" alt="ECG"></td>
    <td><img src="screenshots/watch_settings.png" width="220" alt="Watch settings"></td>
    <td></td>
  </tr>
</table>

Notes:

- Protocol reverse engineered from app decompile / .so decompile / firmware disassembly
- Enabling notifications lowers battery life (estimated to 20 days instead of 30~35)

# DEBUG DATA


There's a stream of `DEBUG` data which holds some "unknown" data at a high sampling rate (mostly 1 Hz), which are not used for anything as far as I'm aware

- Maybe 1Hz acceleromter
- unknown
- unknown
- 1Hz Temperature

It also has a debug log and the watch settings' database.

On the original app, this data is sent straight to withings. I'm not sure why they need 1Hz raw sensor data but I don't like it.
