# Architecture

## Transports

- UVC: 1080p30 preview; settings are locked after connection.
- PTP (`Camera Control`): settings, focus, capture, recording, MJPEG live view.
- SD card: card building, LUT/recipe deployment, offload.
- HDMI capture: optional clean video input.
- USB shell (fpSup): developer-only memory/button access.

## App layers

- `DeviceService` — UVC/PTP/SD discovery and state.
- `CardService` — module catalogue, dependency/conflict checks, write/verify/backup.
- `LutService` — `.cube` parsing, preview, LUT SUP card generation.
- `MediaService` — browse, checksum copy, proxies, CinemaDNG packing.
- `TaskService` — long-running jobs with progress and cancellation.
- `SafetyService` — stock restore, compatibility gates, audit log.
- `PluginHost` — module manifests, permissions, UI slots.

## First milestone

Card Studio only: offline, no camera risk. It reads the fpSup release catalogue,
validates module combinations against firmware 5.02 and loader v4/v6, writes a
card, verifies every file, and backs up the previous card.
