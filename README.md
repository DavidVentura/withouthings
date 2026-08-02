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


Notes:

- Protocol reverse engineered from app decompile / .so decompile / firmware disassembly
- Enabling notifications lowers battery life (estimated to 20 days instead of 30~35)
