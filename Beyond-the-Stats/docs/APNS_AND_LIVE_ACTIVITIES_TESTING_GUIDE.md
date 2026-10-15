# APNs Push Notifications & Live Activities: Testing & Diagnostics Guide

This guide explains how to test, debug, and monitor Push Notifications and Apple Live Activities end-to-end between the BeyondTheStats backend and your physical iOS device.

---

## 1. Why Might Notifications Not Be Arriving on Your Phone?

The 5 most common causes when testing on a physical iPhone:

### Issue 1: Sandbox vs. Production APNs Mismatch (Most Common!)
- **Xcode Direct Build (USB/Wi-Fi)**: Uses **Sandbox APNs** (`api.development.push.apple.com`).
- **TestFlight / App Store Build**: Uses **Production APNs** (`api.push.apple.com`).
> [!IMPORTANT]
> If you build directly from Xcode onto your phone, the device token is a **Sandbox** token. If the backend is running in Production mode (`APNS_USE_SANDBOX=0`), Apple rejects the push with **`400 BadDeviceToken`**.
>
> **Fix**: Set environment variable `APNS_USE_SANDBOX=1` on your backend during local/Xcode testing, or pass `"sandbox": true` in the test endpoints.

### Issue 2: App Open in Foreground (Suppressed Banners)
- By default in iOS, if an app is currently open and active on the screen when a push notification arrives, iOS **will not display a banner** unless you implement the foreground delegate.
> **Fix**: Implement `userNotificationCenter(_:willPresent:)` in your Swift `AppDelegate`:
> ```swift
> func userNotificationCenter(
>     _ center: UNUserNotificationCenter,
>     willPresent notification: UNNotification
> ) async -> UNNotificationPresentationOptions {
>     return [.banner, .sound, .badge, .list]
> }
> ```

### Issue 3: Missing Xcode Capabilities & Entitlements
- In Xcode -> Your App Target -> **Signing & Capabilities**:
  1. Click **+ Capability** -> Add **Push Notifications**.
  2. Click **+ Capability** -> Add **Background Modes** -> Check **Remote notifications**.
  3. In `Info.plist`, ensure `NSSupportsLiveActivities = YES` (or boolean `true`).

### Issue 4: Device Token Format
- When iOS receives a device token in `didRegisterForRemoteNotificationsWithDeviceToken`, it is raw binary `Data`.
> **Fix**: Convert it to a clean hexadecimal string:
> ```swift
> let tokenString = deviceToken.map { String(format: "%02.2hhx", $0) }.joined()
> ```
> Do not send the `<...>` format or spaces to the backend.

### Issue 5: Missing or Invalid APNs `.p8` Key
- Ensure the backend environment variables are set:
  - `APNS_KEY_ID`: 10-character Key ID from Apple Developer.
  - `APNS_TEAM_ID`: 10-character Team ID from Apple Developer.
  - `APNS_AUTH_KEY_PATH`: Absolute path to the `.p8` private key file.
  - `APNS_TOPIC`: Matches your Xcode Bundle Identifier (e.g. `Yitao.BeyondTheStatsApp`).

---

## 2. Step 1: Check Backend APNs Status & Diagnostics

Call the new diagnostic endpoint:
```http
GET /api/test/notifications/status
```

### Sample Response
```json
{
  "ok": true,
  "configured": true,
  "credentials": {
    "key_id": "2A5***89",
    "key_id_set": true,
    "team_id": "748***12",
    "team_id_set": true,
    "key_path": "C:/keys/AuthKey_2A5XXXXX89.p8",
    "key_path_exists": true,
    "key_file_readable": true,
    "key_file_size_bytes": 284
  },
  "apns_settings": {
    "topic": "Yitao.BeyondTheStatsApp",
    "live_activity_topic": "Yitao.BeyondTheStatsApp.push-type.liveactivity",
    "sandbox": true,
    "target_server": "https://api.development.push.apple.com"
  },
  "jwt": {
    "signing_successful": true,
    "error": null
  },
  "in_memory_state": {
    "total_registered_device_tokens": 1,
    "match_subscriptions_count": 0,
    "active_matches": [],
    "active_live_activities_count": 0,
    "pending_queue_size": 0
  },
  "recent_dispatches": []
}
```
If `key_path_exists` is false or `signing_successful` is false, check your `.env` or system environment variables.

---

## 3. Step 2: Test a Direct Push Alert to Your Phone

You can send a push directly to your device token right now and see Apple's exact response:

```http
POST /api/test/notifications/push
Content-Type: application/json

{
  "device_token": "<YOUR_64_CHAR_HEX_TOKEN>",
  "title": "Beyond the Stats Test Alert",
  "body": "Goal! Arsenal 1 - 0 Chelsea (23' Saka)",
  "sandbox": true
}
```

### If Successful:
```json
{
  "ok": true,
  "total_targets": 1,
  "successful_count": 1,
  "results": [
    {
      "ok": true,
      "status_code": 200,
      "apns_id": "123e4567-e89b-12d3-a456-426614174000",
      "endpoint": "https://api.development.push.apple.com/3/device/...",
      "hint": "APNs successfully accepted the push notification."
    }
  ]
}
```

### If Apple Rejects It (e.g. Sandbox vs Production mismatch):
```json
{
  "ok": false,
  "total_targets": 1,
  "successful_count": 0,
  "results": [
    {
      "ok": false,
      "status_code": 400,
      "reason": "BadDeviceToken",
      "hint": "Apple rejected this token as invalid for the Sandbox APNs server. If running TestFlight/AppStore, ensure sandbox=false..."
    }
  ]
}
```
The `hint` tells you immediately how to fix it!

---

## 4. Step 3: End-to-End Game Simulation (Live Activities & Alerts)

You can run a complete simulated match lifecycle to test how your app behaves during an actual game:

### 1. Start Simulated Match
```http
POST /api/test/simulate-game/start
Content-Type: application/json

{
  "match_id": "sim-999",
  "home_team": "Arsenal",
  "away_team": "Chelsea",
  "device_token": "<YOUR_DEVICE_TOKEN>"
}
```
- Injects `Arsenal vs Chelsea (0 - 0)` into memory.
- Appears immediately in `GET /api/live-scores` and `GET /api/startup`.
- Auto-subscribes your `device_token` to all alerts for this match.

### 2. On Your iPhone: Register Live Activity (Optional)
If testing Live Activities, start the activity in Swift:
```swift
let activity = try Activity<MatchAttributes>.request(...)
for await pushToken in activity.pushTokenUpdates {
    let tokenStr = pushToken.map { String(format: "%02.2hhx", $0) }.joined()
    // Register with backend:
    // POST /api/live-activities/register
    // {"activity_token": tokenStr, "match_id": "sim-999", "competition": "Test/Simulation"}
}
```

### 3. Trigger In-Game Events
Simulate match moments at will:

#### A. Kickoff
```http
POST /api/test/simulate-game/event
Content-Type: application/json

{
  "match_id": "sim-999",
  "event": "kickoff"
}
```

#### B. Home Goal (Arsenal Goal)
```http
POST /api/test/simulate-game/event
Content-Type: application/json

{
  "match_id": "sim-999",
  "event": "goal_home",
  "player": "B. Saka",
  "minute": "23'"
}
```
- Score updates to **1 - 0**.
- Goalscorer added.
- **Regular Push Notification** sent to phone: *"GOAL! Arsenal: Arsenal 1 - 0 Chelsea (23' B. Saka)"*.
- **Live Activity Alert Banner & Sound** presented on Lock Screen and Dynamic Island!

#### C. Away Goal (Chelsea Goal)
```http
POST /api/test/simulate-game/event
Content-Type: application/json

{
  "match_id": "sim-999",
  "event": "goal_away",
  "player": "C. Palmer",
  "minute": "45+1'"
}
```

#### D. Half Time
```http
POST /api/test/simulate-game/event
Content-Type: application/json

{
  "match_id": "sim-999",
  "event": "ht"
}
```

#### E. Red Card
```http
POST /api/test/simulate-game/event
Content-Type: application/json

{
  "match_id": "sim-999",
  "event": "red_card_away",
  "player": "M. Caicedo",
  "minute": "62'"
}
```

#### F. Full Time (FT)
```http
POST /api/test/simulate-game/event
Content-Type: application/json

{
  "match_id": "sim-999",
  "event": "ft"
}
```
- Status becomes **FT** (post).
- Sends final whistle push alert.
- Dismisses and ends the Live Activity.

### 4. Reset & Clean Up
```http
POST /api/test/simulate-game/reset
Content-Type: application/json

{
  "match_id": "sim-999"
}
```
Removes the test match and clears subscriptions.

---

## 5. Swift Code Snippet for Receiving Device Token

Add this to your iOS app:

```swift
import UIKit
import UserNotifications

@main
class AppDelegate: UIResponder, UIApplicationDelegate, UNUserNotificationCenterDelegate {

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
    ) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        
        // Request authorization
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .badge, .sound]) { granted, error in
            if granted {
                DispatchQueue.main.async {
                    UIApplication.shared.registerForRemoteNotifications()
                }
            }
        }
        return true
    }

    // Called when Apple returns the device token
    func application(
        _ application: UIApplication,
        didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data
    ) {
        let token = deviceToken.map { String(format: "%02.2hhx", $0) }.joined()
        print("[APNs] Device Push Token: \(token)")
        
        // Save token to UserDefaults
        UserDefaults.standard.set(token, forKey: "apns_device_token")
    }

    func application(
        _ application: UIApplication,
        didFailToRegisterForRemoteNotificationsWithError error: Error
    ) {
        print("[APNs] Failed to register: \(error)")
    }

    // CRITICAL: Present banner even when the app is in the foreground!
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        return [.banner, .sound, .badge, .list]
    }
}
```

