#!/usr/bin/env python3
"""
Sync new photos from a public iCloud Shared Album to a Frameo frame over ADB.

- Runs once at startup, then every SYNC_INTERVAL_MINUTES (default 60), clock-aligned.
- Only photos not pushed in a previous run are downloaded and pushed.
- State is kept in $STATE_DIR/state.json (mount a volume there).

Two kinds of album links are supported:
- New (iOS 27+):  https://photos.icloud.com/shared/album/<key>   -> CloudKit
- Legacy:         https://www.icloud.com/sharedalbum/#<token>     -> sharedstreams
Both use Apple's undocumented anonymous endpoints (the same ones icloud.com uses).
"""
import io
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from urllib.parse import quote, urlparse

import requests
from PIL import Image, ImageOps

ALBUM_URL = os.environ.get("ALBUM_URL", "").strip()  # required; see .env.example
DEVICE = os.environ.get("DEVICE", "192.168.1.183:5555")
REMOTE_DIR = os.environ.get("REMOTE_DIR", "/sdcard/DCIM").rstrip("/")
STATE_DIR = os.environ.get("STATE_DIR", "/data")
MAX_DIM = int(os.environ.get("MAX_DIM", "1920"))  # longest edge; 0 = no resize
INCLUDE_VIDEOS = os.environ.get("INCLUDE_VIDEOS", "false").lower() == "true"
RUN_ONCE = os.environ.get("RUN_ONCE", "false").lower() == "true"
# How often to check for new photos, in minutes (aligned to the clock).
INTERVAL_MIN = max(1, int(os.environ.get("SYNC_INTERVAL_MINUTES", "60")))

STATE_FILE = os.path.join(STATE_DIR, "state.json")
HEADERS = {
    "Content-Type": "text/plain",
    "Origin": "https://www.icloud.com",
    "User-Agent": "Mozilla/5.0",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("frameo-sync")

# An album item, whichever API it came from:
#   {"guid": str, "date": sortable, "video": bool, "url": str}


def album_token(url: str) -> str:
    """Accepts .../shared/album/<key>/, .../sharedalbum/#<token>, or a bare token."""
    m = re.search(r"[#/]([A-Za-z0-9_-]{10,})/?$", url.strip())
    return m.group(1) if m else url.strip()


def is_cloudkit_key(token: str) -> bool:
    # Legacy sharedstreams tokens are 14-15 chars; CloudKit keys are ~25 and may contain - or _
    return bool(re.fullmatch(r"[0-9A-Za-z_-]{20,40}", token))


# -------------------------------------------------------- CloudKit (new) ---

CK_HOST = "https://ckdatabasews.icloud.com"
CK_DB = "database/1/com.apple.photos.cloud/production"
CK_QUERY_TYPE = "CPLAssetAndMasterByAssetDateWithoutHiddenOrDeleted"
VIDEO_TYPES = {"com.apple.quicktime-movie", "public.mpeg-4", "public.movie",
               "com.apple.m4v-video"}
# Apple's JPEG renditions (already rotated, include the owner's edits).
# Med is ~2048px, plenty for a frame; larger ones are tried if Med is missing.
PHOTO_FIELDS = (["resJPEGMedRes", "resJPEGLargeRes", "resJPEGFullRes"]
                if 0 < MAX_DIM <= 2048 else
                ["resJPEGFullRes", "resJPEGLargeRes", "resJPEGMedRes"])
VIDEO_FIELDS = ["resVidMedRes", "resVidLargeRes", "resVidSmallRes"]


def _ck_post(url: str, body: dict) -> dict:
    r = requests.post(url, data=json.dumps(body), headers=HEADERS, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"CloudKit {r.status_code} {url.split('?')[0]}: {r.text[:300]}")
    return r.json()


def ck_resolve(key: str) -> dict:
    """Album key -> anonymous token (valid ~20 min), partition host, zone."""
    data = _ck_post(
        f"{CK_HOST}/{CK_DB}/public/records/resolve?remapEnums=true&sharing_url_key={quote(key)}",
        {"shortGUIDs": [{"value": key}]},
    )
    res = (data.get("results") or [{}])[0]
    acc = res.get("anonymousPublicAccess") or {}
    if not acc.get("token") or not acc.get("databasePartition") or not res.get("zoneID"):
        raise RuntimeError(
            "could not resolve album; make sure it is shared as 'Anyone with the Link'. "
            f"Response: {json.dumps(data)[:300]}")
    host = urlparse(acc["databasePartition"]).hostname or ""
    if not host.endswith(".icloud.com"):
        raise RuntimeError(f"unexpected partition host {host}")
    return {"key": key, "host": f"https://{host}", "token": acc["token"],
            "zoneID": res["zoneID"]}


def _ck_url(acc: dict, action: str) -> str:
    return (f"{acc['host']}/{CK_DB}/shared/{action}?remapEnums=true"
            f"&sharing_url_key={quote(acc['key'])}"
            f"&publicAccessAuthToken={quote(acc['token'])}")


def ck_query_records(acc: dict) -> list:
    records, start = [], 0
    for _ in range(200):
        data = _ck_post(_ck_url(acc, "records/query"), {
            "query": {
                "recordType": CK_QUERY_TYPE,
                "filterBy": [
                    {"fieldName": "startRank", "comparator": "EQUALS",
                     "fieldValue": {"type": "INT64", "value": start}},
                    {"fieldName": "direction", "comparator": "EQUALS",
                     "fieldValue": {"type": "STRING", "value": "ASCENDING"}},
                ],
            },
            "resultsLimit": 100,
            "zoneID": acc["zoneID"],
        })
        page = data.get("records", [])
        records += page
        assets = sum(1 for r in page if r.get("recordType") == "CPLAsset")
        if not assets:
            break
        start += assets
    return records


def ck_zone_changes(acc: dict) -> list:
    """Fallback: read the zone's change feed (works when the index query is rejected)."""
    records, sync_token = [], None
    for _ in range(500):
        zone = {"zoneID": acc["zoneID"], "desiredRecordTypes": ["CPLAsset", "CPLMaster"],
                "resultsLimit": 200}
        if sync_token:
            zone["syncToken"] = sync_token
        data = _ck_post(_ck_url(acc, "changes/zone"), {"zones": [zone]})
        z = (data.get("zones") or [{}])[0]
        records += z.get("records", [])
        sync_token = z.get("syncToken")
        if not z.get("moreComing"):
            break
    return records


def _f(rec, name):
    return ((rec or {}).get("fields") or {}).get(name, {}).get("value")


def _resource_url(asset, master, field):
    """Edited photos keep renders on CPLAsset, originals on CPLMaster; prefer the asset."""
    for rec in (asset, master):
        res = _f(rec, field)
        if isinstance(res, dict) and res.get("downloadURL"):
            ftype = _f(rec, field.replace("Res", "FileType")) or "file"
            return res["downloadURL"].replace("${f}", quote(ftype))
    return None


def _is_removed(rec) -> bool:
    return bool(rec.get("deleted") or _f(rec, "isDeleted") or _f(rec, "isExpunged")
                or _f(rec, "isHidden"))


def list_cloudkit(key: str) -> list:
    acc = ck_resolve(key)
    try:
        records = ck_query_records(acc)
    except RuntimeError as e:
        log.warning("index query failed (%s); falling back to zone changes", e)
        records = ck_zone_changes(acc)

    masters = {r["recordName"]: r for r in records if r.get("recordType") == "CPLMaster"}
    items = []
    for a in records:
        if a.get("recordType") != "CPLAsset" or _is_removed(a):
            continue
        ref = _f(a, "masterRef") or {}
        m = masters.get(ref.get("recordName"))
        if not m or _is_removed(m):
            continue
        video = (_f(m, "itemType") or "") in VIDEO_TYPES
        fields = VIDEO_FIELDS if video else PHOTO_FIELDS
        url = next((u for u in (_resource_url(a, m, f) for f in fields) if u), None)
        if url:
            date = _f(a, "assetDate") or _f(m, "originalCreationDate") or 0
            items.append({"guid": a["recordName"], "date": date, "video": video, "url": url})
    return items


# ---------------------------------------------------- sharedstreams (old) ---

def _ss_post(host, token, endpoint, body):
    return requests.post(f"https://{host}/{token}/sharedstreams/{endpoint}",
                         data=json.dumps(body), headers=HEADERS, timeout=60,
                         allow_redirects=False)


def list_sharedstreams(token: str) -> list:
    host = "p23-sharedstreams.icloud.com"
    for _ in range(3):
        r = _ss_post(host, token, "webstream", {"streamCtag": None})
        if r.status_code == 330:
            host = r.json().get("X-Apple-MMe-Host")
            continue
        r.raise_for_status()
        stream = r.json()
        break
    else:
        raise RuntimeError("too many sharedstreams redirects")

    picks = {}
    for p in stream.get("photos", []):
        derivs = [(int(d.get("fileSize") or 0), d["checksum"])
                  for k, d in p.get("derivatives", {}).items() if k != "PosterFrame"]
        if p.get("photoGuid") and derivs:
            picks[p["photoGuid"]] = (max(derivs)[1], p.get("mediaAssetType") == "video",
                                     p.get("dateCreated", ""))

    urls, guids = {}, list(picks)
    for i in range(0, len(guids), 25):
        r = _ss_post(host, token, "webasseturls", {"photoGuids": guids[i:i + 25]})
        r.raise_for_status()
        for cs, it in r.json().get("items", {}).items():
            urls[cs] = f"https://{it['url_location']}{it['url_path']}"

    return [{"guid": g, "date": d, "video": v, "url": urls[cs]}
            for g, (cs, v, d) in picks.items() if cs in urls]


def list_album() -> list:
    token = album_token(ALBUM_URL)
    if is_cloudkit_key(token):
        return list_cloudkit(token)
    return list_sharedstreams(token)


# ----------------------------------------------------------------- media ---

def prepare_image(data: bytes) -> bytes:
    """Apply EXIF rotation, resize to MAX_DIM, return JPEG bytes."""
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    if MAX_DIM and max(img.size) > MAX_DIM:
        img.thumbnail((MAX_DIM, MAX_DIM), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90, optimize=True)
    return out.getvalue()


# ------------------------------------------------------------------- adb ---

def adb(*args, timeout=120) -> subprocess.CompletedProcess:
    return subprocess.run(["adb", *args], capture_output=True, text=True, timeout=timeout)


def device_state() -> str:
    """'device', 'offline', 'unauthorized', or '' if not in the list."""
    out = adb("devices", timeout=15).stdout
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == DEVICE:
            return parts[1]
    return ""


ADB_KEY_PUB = os.path.expanduser("~/.android/adbkey.pub")
AUTH_WAIT_S = int(os.environ.get("ADB_AUTH_WAIT_SECONDS", "120"))


def adb_key_fingerprint() -> str:
    """MD5 fingerprint as Android shows it in the 'Allow USB debugging?' prompt."""
    import base64, hashlib
    try:
        blob = open(ADB_KEY_PUB).read().split()[0]
        h = hashlib.md5(base64.b64decode(blob)).hexdigest().upper()
        return ":".join(h[i:i + 2] for i in range(0, len(h), 2))
    except Exception as e:
        return f"(no key at {ADB_KEY_PUB}: {e})"


def ensure_connected(wait_s: int = 45) -> bool:
    """Connect and wait for the ADB handshake to finish.

    `adb connect` returns as soon as TCP is up; the device is 'offline' until the
    auth handshake completes, so poll instead of disconnecting mid-handshake.
    If the frame says 'unauthorized', reconnect once (to show a fresh prompt) and
    wait longer so someone can accept it.
    """
    adb("start-server", timeout=30)
    deadline = time.time() + wait_s
    last_out, state, reprompted = "", "", False
    while time.time() < deadline:
        state = device_state()
        if state == "device":
            return True
        if state == "unauthorized" and not reprompted:
            reprompted = True
            log.warning("frame says unauthorized. Accept 'Allow USB debugging?' on the "
                        "frame and tick 'Always allow'. Key fingerprint: %s",
                        adb_key_fingerprint())
            adb("disconnect", DEVICE, timeout=10)   # a fresh connection re-shows the prompt
            deadline = time.time() + AUTH_WAIT_S
            state = ""
        if state == "":  # not connected yet (or dropped): (re)connect
            r = adb("connect", DEVICE, timeout=20)
            last_out = (r.stdout + r.stderr).strip()
        time.sleep(2)

    log.error("frame not ready: state=%s, last connect output=%r",
              state or "not connected", last_out)
    if state in ("unauthorized", "offline"):
        adb("disconnect", DEVICE, timeout=10)  # start clean next run
    return False


def push(local: str, remote_name: str) -> bool:
    r = adb("-s", DEVICE, "push", local, f"{REMOTE_DIR}/{remote_name}", timeout=300)
    if r.returncode != 0:
        log.error("push failed for %s: %s", remote_name, (r.stdout + r.stderr).strip())
        return False
    return True


# ----------------------------------------------------------------- state ---

def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"synced": [], "last_run": None}


def save_state(state: dict):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_FILE)


# ------------------------------------------------------------------ sync ---

def sync_once():
    state = load_state()
    synced = set(state["synced"])

    items = list_album()
    new = [i for i in items if i["guid"] not in synced and (INCLUDE_VIDEOS or not i["video"])]
    log.info("album: %d items, %d already synced, %d new", len(items), len(synced), len(new))

    if new and not ensure_connected():
        log.error("frame unreachable at %s; will retry next run", DEVICE)
        return

    pushed = 0
    new.sort(key=lambda i: str(i["date"]).zfill(20))  # oldest first
    with tempfile.TemporaryDirectory() as tmp:
        for it in new:
            try:
                r = requests.get(it["url"], timeout=300)
                r.raise_for_status()
                if it["video"]:
                    data, name = r.content, f"{it['guid']}.mp4"
                else:
                    data, name = prepare_image(r.content), f"{it['guid']}.jpg"
                local = os.path.join(tmp, name)
                with open(local, "wb") as f:
                    f.write(data)
                if push(local, name):
                    synced.add(it["guid"])
                    pushed += 1
                    state["synced"] = sorted(synced)
                    save_state(state)  # save after each photo so a crash loses nothing
                    log.info("pushed %s", name)
            except Exception as e:  # keep going; failed items retry next run
                log.error("failed %s: %s", it["guid"], e)

    state["last_run"] = datetime.now().isoformat(timespec="seconds")
    save_state(state)
    if new:
        log.info("done: %d/%d new items pushed", pushed, len(new))


def seconds_until_next_run() -> float:
    """Next clock-aligned slot, counted from midnight (e.g. 15 -> :00 :15 :30 :45)."""
    now = datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = (now - midnight).total_seconds()
    step = INTERVAL_MIN * 60
    nxt = midnight + timedelta(seconds=(elapsed // step + 1) * step)
    return max(1.0, (nxt - now).total_seconds())


def main():
    if not ALBUM_URL:
        log.error("ALBUM_URL is not set. Put it in .env (see .env.example).")
        sys.exit(1)
    log.info("frameo-sync starting: device=%s dest=%s interval=%d min",
             DEVICE, REMOTE_DIR, INTERVAL_MIN)
    while True:
        try:
            sync_once()
        except Exception as e:
            log.exception("sync failed: %s", e)
        if RUN_ONCE:
            break
        wait = seconds_until_next_run()
        log.info("next run at %s", (datetime.now() + timedelta(seconds=wait)).strftime("%H:%M"))
        time.sleep(wait)


if __name__ == "__main__":
    main()
