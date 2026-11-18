# frameo-sync

Automatically pushes new photos from an iCloud Shared Album to a Frameo
digital photo frame over ADB (Wi-Fi). Runs in Docker, checks on a schedule,
and only sends photos it hasn't sent before.

```
iCloud shared album  ──(public link, anonymous API)──>  container  ──(adb push)──>  Frameo /sdcard/DCIM
```

## How it works

1. **List the album.** On every run the script fetches the full album listing
   using Apple's anonymous web endpoints (the same ones icloud.com uses for
   people who aren't signed in):
   - **iOS 27+ albums** (`photos.icloud.com/shared/album/<key>`): CloudKit
     (`records/resolve` → `records/query`, falling back to the zone change feed
     if Apple rejects the index query).
   - **Older albums** (`icloud.com/sharedalbum/#<token>`): the legacy
     `sharedstreams` API.
2. **Find new photos.** Every photo ID that isn't already in
   `data/state.json` counts as new. Hidden and deleted photos are skipped, and
   so are videos unless you enable them.
3. **Download and resize.** It takes Apple's JPEG rendition (which includes
   the owner's edits), applies EXIF rotation, and shrinks the longest edge to
   `MAX_DIM`.
4. **Push.** It runs `adb push <ID>.jpg /sdcard/DCIM/`. Only after a
   successful push is the ID recorded. Failed items are retried on the next run.
5. **Wait** for the next clock-aligned slot (`SYNC_INTERVAL_MINUTES`).

It never deletes anything, either from the frame or from the album.

## Requirements

- Docker with Compose
- The Frameo frame with ADB enabled over TCP:
  **Settings → About → Beta Program on → ADB Access on**, then (once, over
  USB) `adb tcpip 5555`. To keep TCP mode after reboots, use a rooted frame:
  `adb shell su -c 'setprop persist.adb.tcp.port 5555'`
- The album shared by link: **Anyone with the Link** (iOS 27+), or
  **Public Website** (older albums)
- A DHCP reservation for the frame so its IP doesn't change

## Setup

```bash
cp .env.example .env        # then set ALBUM_URL (and DEVICE if different)
mkdir -p adbkeys data
```

### ADB key (important)

The frame has to trust the container's ADB key. Frameo's UI can hide the
"Allow USB debugging?" prompt, so the reliable route is to reuse a key the
frame already trusts, such as your desktop's:

| OS | Key location |
|---|---|
| Windows | `%USERPROFILE%\.android\` |
| macOS / Linux | `~/.android/` |

Copy `adbkey` and `adbkey.pub` from there into `./adbkeys/`.

Then start it:

```bash
docker compose up -d --build
docker compose logs -f
```

## Configuration

Values in `.env` are read by `docker-compose.yml`. Other settings live
directly in the compose file.

| Variable | Default | Description |
|---|---|---|
| `ALBUM_URL` | **required** | Public link to the shared album. Treat it as a secret |
| `DEVICE` | `IP:5555` | Frame's ADB address |
| `SYNC_INTERVAL_MINUTES` | `60` | Clock-aligned: `15` → :00/:15/:30/:45; `120` → every 2 h from midnight |
| `REMOTE_DIR` | `/sdcard/DCIM` | Destination on the frame |
| `MAX_DIM` | `1920` | Longest edge in px; `0` = no resize |
| `INCLUDE_VIDEOS` | `false` | Also push videos (`.mp4`) |
| `ADB_AUTH_WAIT_SECONDS` | `120` | How long to wait if the frame reports `unauthorized` |
| `RUN_ONCE` | `false` | Run a single sync and exit (for testing) |
| `TZ` | `America/New_York` | Used for schedule alignment and log times |

## Sync state

`./data/state.json`:

```json
{ "synced": ["<photo-id>", "..."], "last_run": "2026-11-18T15:00:02" }
```

- IDs are Apple's stable per-photo IDs: the CPLAsset record name for iOS 27
  albums, or `photoGuid` for older ones. The file on the frame is `<ID>.jpg`.
- `last_run` is for your information only. Filtering is by ID, so photos
  added with old dates still sync.
- **Re-push everything:** stop the container, delete `state.json`, then start it.
- **Re-push one photo:** remove its ID from the list.
- Deleting a photo on the frame does **not** bring it back. Removing a
  photo from the album and adding it again gives it a new ID, so it's pushed again.

## Volumes

| Host | Container | Contents |
|---|---|---|
| `./data` | `/data` | `state.json` |
| `./adbkeys` | `/root/.android` | ADB key pair the frame trusts |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ALBUM_URL is not set` | Create `.env` from `.env.example` |
| `CloudKit 4xx … could not resolve album` | Album isn't shared by link; turn on **Anyone with the Link** |
| `sharedstreams` 404 | You're using an iOS 27 album link with an old-style token. Copy the link again from Photos |
| `state=not connected` | Check from the container: `docker exec frameo-sync python -c "import socket; socket.create_connection(('IP',5555),5)"`. On Linux try `network_mode: host`; check firewall/VLAN rules allow the Docker host |
| `state=unauthorized` | Copy a trusted `adbkey`/`adbkey.pub` into `./adbkeys`, then `docker compose restart` |
| `state=offline` repeatedly | Disconnect other ADB hosts from the frame (`adb disconnect`, `adb kill-server` on your desktop) |
| Pushed but not on the slideshow | Frameo imports from DCIM only when **Settings → Manage photos → Transfer from computer** is toggled on |

## Security notes

- The album link gives read access to the whole album, even though Apple
  calls it "public". Keep `.env` out of git (it's in `.gitignore`). You can
  revoke the link from the album's sharing settings.
- `./adbkeys` grants a shell, often a root shell, on the frame. Keep it private.
- ADB over TCP has no network-level authentication, and Android 8.1 no longer
  gets security patches. Put the frame on an isolated IoT VLAN and allow
  port 5555 only from the Docker host.
- This relies on Apple's **undocumented** endpoints and may break if Apple
  changes them.

## Files

```
sync.py              the sync service
Dockerfile           python:3.12-slim + adb + requests/Pillow
docker-compose.yml   service definition
.env.example         template for .env (album link, device, interval)
.gitignore           excludes .env, data/, adbkeys/
```
