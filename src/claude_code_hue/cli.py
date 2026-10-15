#!/usr/bin/env python3
"""Use a Philips Hue room as a concurrency-safe Claude Code work indicator."""

import argparse
try:
    import fcntl
except ImportError:  # Windows: no POSIX locks; FileLock falls back to msvcrt.
    fcntl = None
    import msvcrt
import hashlib
import hmac
import http.client
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request


DEFAULT_BUSY = {
    "on": True,
    "bri": 110,
    "hue": 46920,
    "sat": 230,
    "transitiontime": 3,
}
DEFAULT_COMPLETE = {
    "on": True,
    "bri": 254,
    "hue": 25500,
    "sat": 254,
    "transitiontime": 1,
}
SUPPORTED_GROUP_TYPES = {"Room", "Zone", "LightGroup"}
SUPPORTED_HOOK_EVENTS = {
    "SessionStart", "SessionEnd", "UserPromptSubmit", "Stop", "StopFailure"
}
INTERRUPT_MARKERS = (
    "[Request interrupted by user]",
    "[Request interrupted by user for tool use]",
)
PROJECT_NAME = "claude-code-hue"
HOOK_ARGUMENTS = ["-m", "claude_code_hue", "hook"]
WINDOWS = os.name == "nt"


class HueError(RuntimeError):
    pass


_bridge_contact = [float("-inf")]


def note_bridge_contact():
    _bridge_contact[0] = time.monotonic()


def last_bridge_contact():
    return _bridge_contact[0]


def data_dir():
    override = os.environ.get("CLAUDE_CODE_HUE_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude" / "hue-indicator"


def config_path():
    return data_dir() / "config.json"


def state_path():
    return data_dir() / "state.json"


def lock_path():
    return data_dir() / "state.lock"


def queue_path():
    return data_dir() / "events.json"


def queue_lock_path():
    return data_dir() / "events.lock"


def worker_lock_path():
    return data_dir() / "worker.lock"


def log_path():
    return data_dir() / "indicator.log"


def claude_config_dir():
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude"


def settings_path():
    return claude_config_dir() / "settings.json"


def ensure_data_dir():
    data_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(data_dir(), 0o700)
    except OSError:
        pass


def log(message):
    try:
        ensure_data_dir()
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with log_path().open("a", encoding="utf-8") as handle:
            handle.write("{} {}\n".format(timestamp, message))
    except OSError:
        pass


def read_json(path, default):
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def replace_file(source, destination):
    if not WINDOWS:
        os.replace(source, destination)
        return
    # Windows refuses to replace a file another process (a reader, antivirus,
    # the search indexer) has open at that instant; such holds are brief.
    for _ in range(40):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            time.sleep(0.05)
    os.replace(source, destination)


def write_json_atomic(path, value):
    ensure_data_dir()
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(path.parent), prefix="." + path.name + ".", text=True
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        replace_file(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


class FileLock:
    def __init__(self, path, nonblocking=False):
        self.path = path
        self.nonblocking = nonblocking

    def __enter__(self):
        ensure_data_dir()
        self.descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        if fcntl is None:
            if not self._lock_windows():
                os.close(self.descriptor)
                self.descriptor = None
                return None
            return self
        flags = fcntl.LOCK_EX
        if self.nonblocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(self.descriptor, flags)
        except BlockingIOError:
            os.close(self.descriptor)
            self.descriptor = None
            return None
        return self

    def _lock_windows(self):
        # Lock the first byte as a whole-file stand-in for flock. LK_LOCK gives
        # up after ten seconds, so a blocking acquire polls LK_NBLCK instead.
        while True:
            try:
                msvcrt.locking(self.descriptor, msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                if self.nonblocking:
                    return False
                time.sleep(0.05)

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()

    def release(self):
        if self.descriptor is None:
            return
        if fcntl is None:
            os.lseek(self.descriptor, 0, os.SEEK_SET)
            msvcrt.locking(self.descriptor, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
        os.close(self.descriptor)
        self.descriptor = None


def StateLock():
    return FileLock(lock_path())


def QueueLock():
    return FileLock(queue_lock_path())


class HueClient:
    def __init__(self, host, username=None, fingerprint=None, port=443, timeout=3.0):
        self.host = host
        self.port = int(port)
        self.username = username
        self.fingerprint = normalize_fingerprint(fingerprint) if fingerprint else None
        self.timeout = float(timeout)

    def request(self, method, path, payload=None):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        connection = http.client.HTTPSConnection(
            self.host, self.port, timeout=self.timeout, context=context
        )
        try:
            connection.connect()
            certificate = connection.sock.getpeercert(binary_form=True)
            actual_fingerprint = hashlib.sha256(certificate).hexdigest()
            if self.fingerprint and not hmac.compare_digest(
                actual_fingerprint, self.fingerprint
            ):
                raise HueError("Hue Bridge certificate fingerprint changed")

            body = None
            headers = {"Accept": "application/json"}
            if payload is not None:
                body = json.dumps(payload).encode("utf-8")
                headers["Content-Type"] = "application/json"
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            note_bridge_contact()
            if response.status < 200 or response.status >= 300:
                raise HueError("Hue Bridge returned HTTP {}".format(response.status))
            try:
                decoded = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise HueError("Hue Bridge returned invalid JSON") from error
            check_hue_errors(decoded)
            return decoded
        finally:
            connection.close()

    def api_path(self, suffix=""):
        if not self.username:
            raise HueError("Hue Bridge username is not configured")
        return "/api/{}{}".format(self.username, suffix)

    def groups(self):
        return self.request("GET", self.api_path("/groups"))

    def group(self, group_id):
        return self.request("GET", self.api_path("/groups/{}".format(group_id)))

    def light(self, light_id):
        return self.request("GET", self.api_path("/lights/{}".format(light_id)))

    def lights(self):
        return self.request("GET", self.api_path("/lights"))

    def group_action(self, group_id, action):
        return self.request(
            "PUT", self.api_path("/groups/{}/action".format(group_id)), action
        )

    def light_state(self, light_id, action):
        return self.request(
            "PUT", self.api_path("/lights/{}/state".format(light_id)), action
        )


def normalize_fingerprint(value):
    return str(value).replace(":", "").strip().lower()


def check_hue_errors(value):
    errors = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and "error" in item:
                error = item["error"]
                errors.append(error.get("description", str(error)))
    elif isinstance(value, dict) and "error" in value:
        error = value["error"]
        errors.append(error.get("description", str(error)))
    if errors:
        raise HueError("; ".join(errors))


def bridge_fingerprint(host, port=443, timeout=3.0):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = http.client.HTTPSConnection(
        host, int(port), timeout=float(timeout), context=context
    )
    try:
        connection.connect()
        certificate = connection.sock.getpeercert(binary_form=True)
        return hashlib.sha256(certificate).hexdigest()
    finally:
        connection.close()


def discover_bridges():
    bridges = []
    seen = set()

    try:
        local_host = socket.gethostbyname("philips-hue.local")
        bridges.append({"id": None, "internalipaddress": local_host, "source": "mDNS"})
        seen.add(local_host)
    except OSError:
        pass

    request = urllib.request.Request(
        "https://discovery.meethue.com/", headers={"User-Agent": "claude-code-hue-indicator/1"}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            discovered = json.load(response)
        for bridge in discovered:
            host = bridge.get("internalipaddress")
            if host and host not in seen:
                item = dict(bridge)
                item["source"] = "Hue discovery service"
                bridges.append(item)
                seen.add(host)
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return bridges


def load_config():
    config = read_json(config_path(), None)
    if not isinstance(config, dict):
        raise HueError("Hue indicator is not configured; run setup first")
    required = ("bridge_host", "certificate_sha256", "username", "group_id")
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise HueError("Hue configuration is incomplete: {}".format(", ".join(missing)))
    return config


def client_from_config(config):
    return HueClient(
        config["bridge_host"],
        username=config["username"],
        fingerprint=config["certificate_sha256"],
        port=config.get("bridge_port", 443),
        timeout=config.get("request_timeout_seconds", 3.0),
    )


def default_state():
    return {"active": {}, "completed": {}, "snapshot": None}


def load_state():
    state = read_json(state_path(), default_state())
    if not isinstance(state, dict):
        return default_state()
    state.setdefault("active", {})
    state.setdefault("completed", {})
    state.setdefault("snapshot", None)
    return state


def save_state(state):
    write_json_atomic(state_path(), state)


def snapshot_room(client, group_id):
    group = client.group(group_id)
    all_lights = client.lights()
    snapshot = {"lights": {}, "captured_at": time.time()}
    for light_id in group.get("lights", []):
        try:
            light = all_lights[str(light_id)]
            snapshot["lights"][str(light_id)] = dict(light.get("state", {}))
        except (HueError, KeyError) as error:
            log("Could not snapshot light {}: {}".format(light_id, error))
    return snapshot


def restorable_state(original, transitiontime=2):
    if not isinstance(original, dict) or "on" not in original:
        return None
    result = {"on": bool(original["on"]), "transitiontime": int(transitiontime)}
    if not original["on"]:
        return result

    if "bri" in original:
        result["bri"] = original["bri"]
    color_mode = original.get("colormode")
    if color_mode == "hs":
        if "hue" in original:
            result["hue"] = original["hue"]
        if "sat" in original:
            result["sat"] = original["sat"]
    elif color_mode == "xy" and "xy" in original:
        result["xy"] = original["xy"]
    elif color_mode == "ct" and "ct" in original:
        result["ct"] = original["ct"]
    if "effect" in original and original["effect"] in ("none", "colorloop"):
        result["effect"] = original["effect"]
    return result


def restore_room(client, snapshot, transitiontime=2, request_gap_seconds=0.12):
    if not snapshot:
        return
    failures = []
    for light_id, original in snapshot.get("lights", {}).items():
        action = restorable_state(original, transitiontime=transitiontime)
        if not action:
            continue
        try:
            client.light_state(light_id, action)
        except HueError as error:
            log("Could not restore light {}: {}".format(light_id, error))
            failures.append(light_id)
        if request_gap_seconds:
            time.sleep(float(request_gap_seconds))
    if failures:
        raise HueError("Room restoration incomplete; saved state retained for retry")


def trim_state(state, stale_after_seconds):
    now = time.time()
    # Age alone is not evidence that a long-running or quiet turn has stopped.
    state["completed"] = {
        key: timestamp
        for key, timestamp in state["completed"].items()
        if now - float(timestamp) < stale_after_seconds
    }
    return []


def event_key(session_id, turn_id):
    return "{}:{}".format(session_id or "unknown-session", turn_id or "unknown-turn")


def active_keys_for_session(state, session_id):
    prefix = "{}:".format(session_id or "unknown-session")
    return [key for key in state["active"] if key.startswith(prefix)]


def busy_timing(config):
    hold = max(0.0, float(config.get("busy_hold_seconds", 1.0)))
    interval = max(hold + 1.0, float(config.get("busy_interval_seconds", 10.0)))
    return hold, interval


def breathing_timing(config):
    ratio = float(config.get("breathing_min_ratio", 0.4))
    period = float(config.get("breathing_period_seconds", 4.0))
    if not math.isfinite(ratio) or not 0 < ratio <= 1:
        raise HueError("breathing_min_ratio must be between 0 (exclusive) and 1")
    if not math.isfinite(period) or period < 2:
        raise HueError("breathing_period_seconds must be at least 2")
    return ratio, period / 2


def busy_resume_delay(config):
    return busy_timing(config)[1] if config.get("busy_mode") == "pulse" else 0.5


def sync_breathing_lights(state, observed):
    """Keep a stable per-light baseline; our dimmed target is never a new base.

    Bridges can report delayed intermediate fade values. Values inside our
    commanded envelope are not evidence of a manual change. Off/newly-on lights
    are released/adopted without sending an on/off command.
    """
    breathing = state["breathing"]
    tracked = breathing.setdefault("lights", {})
    for light_id in list(tracked):
        if light_id not in observed or not observed[light_id].get("on"):
            del tracked[light_id]
    for light_id, actual in observed.items():
        if not actual.get("on") or not isinstance(actual.get("bri"), int):
            continue
        if actual.get("reachable") is False:
            continue
        entry = tracked.get(light_id)
        brightness = actual["bri"]
        if entry is None:
            tracked[light_id] = {"base": brightness, "expected": brightness}
        elif not (min(entry.get("low", entry["base"]), entry["expected"],
                      entry.get("previous", entry["expected"])) - 1 <= brightness
                  <= max(entry["base"], entry["expected"],
                         entry.get("previous", entry["expected"])) + 1):
            entry.update(base=brightness, expected=brightness)
            entry.pop("previous", None)
            entry.pop("low", None)


def set_breathing_brightness(state, client, light_id, target, transitiontime):
    entry = state["breathing"]["lights"][light_id]
    # Persist both possible outcomes before a network call that could fail after
    # reaching the Bridge. A retry must not interpret our own old target as a scene.
    entry["previous"] = entry["expected"]
    entry["expected"] = target
    save_state(state)
    client.light_state(light_id, {"bri": target, "transitiontime": transitiontime})
    entry.pop("previous", None)
    save_state(state)


def restore_breathing(state, config, client):
    if not state.get("breathing"):
        return
    observed = snapshot_room(client, str(config["group_id"]))["lights"]
    sync_breathing_lights(state, observed)
    save_state(state)
    for light_id, entry in state["breathing"]["lights"].items():
        if observed[light_id].get("reachable") is False:
            raise HueError("Waiting for a breathing light to reconnect before restoring")
        if observed[light_id].get("bri") != entry["base"]:
            set_breathing_brightness(state, client, light_id, entry["base"],
                                     int(config.get("restore_transitiontime", 2)))
            time.sleep(float(config.get("restore_request_gap_seconds", 0.12)))
    state["breathing"] = None
    save_state(state)


def prepare_completion_snapshot(state, config, client):
    if not state.get("snapshot"):
        snapshot = snapshot_room(client, str(config["group_id"]))
        if state.get("breathing"):
            sync_breathing_lights(state, snapshot["lights"])
            for light_id, entry in state["breathing"]["lights"].items():
                snapshot["lights"][light_id]["bri"] = entry["base"]
                entry["expected"] = entry["base"]
                entry["previous"] = config.get("complete", DEFAULT_COMPLETE).get("bri", 254)
            state["breathing"].update(phase="down", next_at=time.time() + 0.5)
        state["snapshot"] = snapshot


def advance_breathing_state(state, config, client, allow_start=True):
    if state.get("snapshot"):
        restore_saved_lighting(state, config, client)
        state["next_busy_at"] = time.time() + 0.5
        if state.get("breathing"):
            state["breathing"]["next_at"] = state["next_busy_at"]
        save_state(state)
        return  # Migrate old blue state / finish a green restore before breathing.
    if not state["active"]:
        restore_breathing(state, config, client)
        return
    now = time.time()
    breathing = state.get("breathing")
    if not allow_start or now < (breathing or {}).get("next_at", state.get("next_busy_at", 0)):
        return
    with QueueLock():
        if read_json(queue_path(), []):
            return
    ratio, half_period = breathing_timing(config)
    observed = snapshot_room(client, str(config["group_id"]))["lights"]
    if breathing is None:
        breathing = {"lights": {}, "phase": "down"}
        state["breathing"] = breathing
    sync_breathing_lights(state, observed)
    down = breathing["phase"] == "down"
    deadline = time.time() + half_period
    breathing.update(phase="up" if down else "down", next_at=deadline + 0.1)
    save_state(state)
    failures = []
    for light_id, entry in breathing["lights"].items():
        if observed[light_id].get("reachable") is False:
            continue
        target = max(1, round(entry["base"] * ratio)) if down else entry["base"]
        target = min(entry["base"], target)
        entry["low"] = min(entry.get("low", entry["base"]), target)
        # Stagger local requests but finish all fades at the same boundary.
        transition = max(1, round((deadline - time.time()) * 10))
        try:
            set_breathing_brightness(state, client, light_id, target, transition)
        except (HueError, OSError) as error:
            failures.append(str(error))
        time.sleep(float(config.get("restore_request_gap_seconds", 0.12)))
    if failures:
        raise HueError("Some breathing updates failed; baselines retained for retry")


def restore_saved_lighting(state, config, client):
    restore_room(
        client, state.get("snapshot"),
        transitiontime=config.get("restore_transitiontime", 2),
        request_gap_seconds=config.get("restore_request_gap_seconds", 0.12),
    )
    state.update(snapshot=None, pulse=None, pulse_until=0)
    save_state(state)


def advance_blue_state(state, config, client, allow_start=True):
    """Advance one shared, nonblocking blue pulse; caller holds StateLock."""
    now = time.time()
    hold, interval = busy_timing(config)
    if state.get("snapshot"):
        busy = state.get("pulse") == "busy"
        if busy and state["active"] and now < state.get("pulse_until", 0):
            return
        restore_saved_lighting(state, config, client)
        if busy:
            state["next_busy_at"] = max(state.get("next_busy_at", 0), time.time() + 1.0)
        else:
            # Recover an interrupted restore or migrate an old solid-blue state
            # before taking a fresh snapshot, never snapshot our own color.
            state["next_busy_at"] = time.time() + interval
        save_state(state)
        return  # Give the restore transition time to finish before any snapshot.
    if not state["active"]:
        if state.get("next_busy_at"):
            state["next_busy_at"] = 0
            save_state(state)
        return
    if not allow_start or now < state.get("next_busy_at", 0):
        return
    with QueueLock():
        if read_json(queue_path(), []):
            return  # Queued completions take priority over a scheduled blue pulse.
    snapshot = snapshot_room(client, str(config["group_id"]))
    if not snapshot.get("lights"):
        raise HueError("Cannot pulse the room without a lighting snapshot")
    state.update(snapshot=snapshot, pulse="busy", pulse_until=now + hold,
                 next_busy_at=now + interval)
    save_state(state)
    client.group_action(str(config["group_id"]), config.get("busy", DEFAULT_BUSY))
    state["pulse_until"] = time.time() + hold
    save_state(state)


def advance_busy_state(state, config, client, allow_start=True):
    mode = config.get("busy_mode", "solid")
    if mode == "solid":
        advance_solid_state(state, config, client, allow_start)
    elif mode == "breathe":
        advance_breathing_state(state, config, client, allow_start)
    else:
        if state.get("breathing"):
            restore_breathing(state, config, client)
            state["next_busy_at"] = time.time() + 0.5
            save_state(state)
            return
        advance_blue_state(state, config, client, allow_start)


def set_solid_busy(state, config, client):
    state.update(pulse="solid_pending", breathing=None)
    save_state(state)
    client.group_action(str(config["group_id"]), config.get("busy", DEFAULT_BUSY))
    state["pulse"] = "solid"
    save_state(state)


def advance_solid_state(state, config, client, allow_start=True):
    if not state["active"]:
        restore_breathing(state, config, client)
        if state.get("snapshot"):
            restore_saved_lighting(state, config, client)
        return
    if state.get("snapshot") and state.get("pulse") == "solid":
        return  # Keep steady blue without continuously sending Hue commands.
    if not allow_start and state.get("pulse") != "solid_pending":
        return
    with QueueLock():
        if read_json(queue_path(), []):
            return
    # Also migrates a breathing session without capturing its dimmed brightness.
    prepare_completion_snapshot(state, config, client)
    if not state["snapshot"].get("lights"):
        raise HueError("Cannot set busy lighting without a room snapshot")
    set_solid_busy(state, config, client)


def finish_notification(state, config, client):
    if config.get("busy_mode", "solid") == "solid" and state["active"]:
        set_solid_busy(state, config, client)
        return
    restore_saved_lighting(state, config, client)
    state["next_busy_at"] = time.time() + busy_resume_delay(config)
    if state.get("breathing"):
        state["breathing"]["next_at"] = state["next_busy_at"]
    save_state(state)


def advance_busy_pulse(client=None, allow_start=True):
    config = load_config()
    with StateLock():
        advance_busy_state(load_state(), config, client or client_from_config(config), allow_start)


def process_event(event_name, session_id, turn_id, client=None,
                  transcript_path=None, require_active=False):
    config = load_config()
    if client is None:
        client = client_from_config(config)
    group_id = str(config["group_id"])
    key = event_key(session_id, turn_id)
    stale_after = float(config.get("stale_after_seconds", 86400))

    with StateLock():
        state = load_state()
        if require_active and key not in state["active"]:
            return
        trim_state(state, stale_after)

        if event_name == "SessionStart":
            recovered = active_keys_for_session(state, session_id)
            for active_key in recovered:
                del state["active"][active_key]
            save_state(state)
            if not state["active"]:
                restore_breathing(state, config, client)
            if not state["active"] and state.get("snapshot"):
                restore_room(
                    client,
                    state["snapshot"],
                    transitiontime=config.get("restore_transitiontime", 2),
                    request_gap_seconds=config.get(
                        "restore_request_gap_seconds", 0.12
                    ),
                )
                state["snapshot"] = None
            save_state(state)
            log(
                "Session started; recovered {} interrupted turn(s); {} active".format(
                    len(recovered), len(state["active"])
                )
            )
            return

        if event_name == "SessionEnd":
            recovered = active_keys_for_session(state, session_id)
            for active_key in recovered:
                del state["active"][active_key]
            save_state(state)
            if not state["active"]:
                restore_breathing(state, config, client)
            if not state["active"] and state.get("snapshot"):
                restore_room(
                    client,
                    state["snapshot"],
                    transitiontime=config.get("restore_transitiontime", 2),
                    request_gap_seconds=config.get(
                        "restore_request_gap_seconds", 0.12
                    ),
                )
                state["snapshot"] = None
            save_state(state)
            log(
                "Session ended; cleared {} active turn(s); {} active".format(
                    len(recovered), len(state["active"])
                )
            )
            return

        if event_name == "UserPromptSubmit":
            if key in state["completed"]:
                return
            was_idle = not state["active"]
            superseded = [
                active_key
                for active_key in active_keys_for_session(state, session_id)
                if active_key != key
            ]
            for active_key in superseded:
                del state["active"][active_key]
            if superseded:
                log(
                    "Superseded {} interrupted turn(s) in this session".format(
                        len(superseded)
                    )
                )
            if key not in state["active"]:
                if was_idle:
                    state["next_busy_at"] = 0
                    if state.get("pulse") == "solid":
                        # A leftover snapshot is the real scene; re-assert blue.
                        state["pulse"] = None
                state["active"][key] = {
                    "started_at": time.time(),
                    "session_id": session_id or "",
                    "turn_id": turn_id or "",
                }
            if isinstance(transcript_path, str) and transcript_path:
                state["active"][key]["transcript_path"] = transcript_path
            save_state(state)
            log("Turn started; {} active".format(len(state["active"])))
            advance_busy_state(state, config, client)
            return

        if event_name in ("TurnAborted", "StopFailure"):
            if key not in state["active"]:
                return
            del state["active"][key]
            # A late Stop must not report an interrupted or failed turn as done.
            state["completed"][key] = time.time()
            save_state(state)
            log("{} turn {}; {} active".format(
                "Recovered interrupted" if event_name == "TurnAborted" else "API error ended",
                key, len(state["active"]),
            ))
            return

        if event_name == "Stop":
            if key in state["completed"]:
                log("Ignored duplicate completion for {}".format(key))
                return

            state["active"].pop(key, None)
            state["completed"][key] = time.time()
            save_state(state)
            log("Turn completed; {} active".format(len(state["active"])))
            prepare_completion_snapshot(state, config, client)
            if not state["active"]:
                state["breathing"] = None
            # Green restores the undimmed scene baseline, never the breathing dip.
            state["pulse"] = None
            state["next_busy_at"] = time.time() + busy_resume_delay(config)
            save_state(state)

            client.group_action(group_id, config.get("complete", DEFAULT_COMPLETE))
            time.sleep(float(config.get("completion_hold_seconds", 1.0)))

            finish_notification(state, config, client)
            time.sleep(float(config.get("completion_gap_seconds", 0.35)))
            return

        log("Ignored unsupported hook event {}".format(event_name))


def enqueue_event(event_name, session_id, turn_id, transcript_path=None):
    queued = {
        "id": "{}-{}-{}".format(os.getpid(), time.time_ns(), event_name),
        "event_name": event_name,
        "session_id": session_id or "",
        "turn_id": turn_id or "",
        "transcript_path": transcript_path if isinstance(transcript_path, str) else None,
    }
    with QueueLock():
        queue = read_json(queue_path(), [])
        if not isinstance(queue, list):
            queue = []
        queue.append(queued)
        write_json_atomic(queue_path(), queue)


def spawn_drain_worker():
    command = [sys.executable] + (
        ["-m", "claude_code_hue"] if __package__ else [os.path.abspath(__file__)]
    ) + ["_drain"]
    if os.environ.get("CLAUDE_CODE_HUE_FOREGROUND") == "1":
        drain_events()
        return
    with open(os.devnull, "rb") as stdin_handle, open(
        os.devnull, "ab"
    ) as output_handle:
        if WINDOWS:
            spawn_windows_worker(command, stdin_handle, output_handle)
            return
        subprocess.Popen(
            command,
            stdin=stdin_handle,
            stdout=output_handle,
            stderr=output_handle,
            close_fds=True,
            start_new_session=True,
        )


def spawn_windows_worker(command, stdin_handle, output_handle):
    # The Windows counterpart of start_new_session: no console window, its own
    # process group, and outside the hook's job object when the job allows it.
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    for extra in (subprocess.CREATE_BREAKAWAY_FROM_JOB, 0):
        try:
            subprocess.Popen(
                command,
                stdin=stdin_handle,
                stdout=output_handle,
                stderr=output_handle,
                close_fds=True,
                creationflags=flags | extra,
            )
            return
        except OSError:
            if not extra:
                raise


def dispatch_event(event_name, session_id, turn_id, transcript_path=None):
    enqueue_event(event_name, session_id, turn_id, transcript_path)
    spawn_drain_worker()


class LifecycleReader:
    """Find user interruptions in local Claude Code transcripts, incrementally.

    Claude Code runs no hook when the user interrupts a turn. Transcript JSONL
    is an internal format, not a stable API: only explicit interruption records
    count. Unknown records, missing files, and silence never imply completion.
    """

    READ_BYTES = 4 * 1024 * 1024
    MAX_LINE_BYTES = 1024 * 1024

    def __init__(self):
        self.files = {}
        self.discovery = {}

    def paths_for(self, details):
        session_id = details.get("session_id", "")
        paths = set()
        explicit = details.get("transcript_path")
        if isinstance(explicit, str) and explicit:
            paths.add(Path(explicit).expanduser())
        # Fall back to the default layout: projects/<project>/<session>.jsonl.
        if re.fullmatch(r"[A-Za-z0-9-]+", session_id):
            cached_at, found = self.discovery.get(session_id, (0, set()))
            if time.monotonic() - cached_at >= 30 or not cached_at:
                try:
                    found = set((claude_config_dir() / "projects").glob(
                        "*/{}.jsonl".format(session_id)
                    ))
                except OSError:
                    found = set()
                self.discovery[session_id] = (time.monotonic(), found)
            paths.update(found)
        return paths

    def read(self, path):
        try:
            with path.open("rb") as handle:
                stat = os.fstat(handle.fileno())
                identity = (stat.st_dev, stat.st_ino)
                cached = self.files.get(path)
                if cached is None or cached["identity"] != identity or stat.st_size < cached["offset"]:
                    cached = {"identity": identity, "offset": 0, "pending": b"",
                              "skipping": False, "interrupted": set()}
                    self.files[path] = cached
                handle.seek(cached["offset"])
                chunk = handle.read(self.READ_BYTES)
                cached["offset"] += len(chunk)
        except OSError:
            return None
        lines = (cached["pending"] + chunk).split(b"\n")
        cached["pending"] = lines.pop()
        for line in lines:
            if cached["skipping"]:
                cached["skipping"] = False
                continue
            if len(line) > self.MAX_LINE_BYTES:
                continue
            # Do not deserialize or retain conversation text and tool output.
            if b"[Request interrupted by user" not in line or b'"promptId"' not in line:
                continue
            try:
                record = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(record, dict) or record.get("type") != "user":
                continue
            if record.get("isSidechain"):
                continue
            prompt_id = record.get("promptId")
            message = record.get("message")
            if not isinstance(prompt_id, str) or not prompt_id or not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict):
                text = content[0].get("text") if content[0].get("type") == "text" else None
            else:
                text = content
            if text in INTERRUPT_MARKERS and isinstance(record.get("sessionId"), str):
                cached["interrupted"].add((record["sessionId"], prompt_id))
        if len(cached["pending"]) > self.MAX_LINE_BYTES:
            cached["pending"] = b""
            cached["skipping"] = True
        # Do not act on historical state until caught up to the file's end.
        return cached if cached["offset"] >= stat.st_size else None

    def terminal_event(self, details):
        catching_up = False
        found = False
        for path in self.paths_for(details):
            cached = self.read(path)
            if cached is None and path in self.files and path.exists():
                catching_up = True
            turn = (details.get("session_id"), details.get("turn_id"))
            if cached and turn in cached["interrupted"]:
                found = True
        return "interrupted" if found and not catching_up else None


def reconcile_active_turns(reader, client=None):
    with StateLock():
        active = list(load_state()["active"].values())
    sessions = {details.get("session_id") for details in active}
    explicit = {Path(details["transcript_path"]).expanduser() for details in active
                if isinstance(details.get("transcript_path"), str) and details["transcript_path"]}
    reader.files = {path: data for path, data in reader.files.items()
                    if path.stem in sessions or path in explicit
                    or {session for session, _ in data["interrupted"]} & sessions}
    reader.discovery = {session: data for session, data in reader.discovery.items()
                        if session in sessions}
    for details in active:
        terminal = reader.terminal_event(details)
        if terminal:
            process_event(
                "TurnAborted",
                details.get("session_id", ""), details.get("turn_id", ""),
                client=client, require_active=True,
            )
    advance_busy_pulse(client, allow_start=False)


class WorkerHealth:
    """Back off after failures and notice when the Bridge stays unreachable.

    Only a request the Bridge answered counts as recovery: many loop passes
    succeed without contacting it, which must not reset the failure clock.
    """

    GIVE_UP_SECONDS = 60
    MAX_DELAY_SECONDS = 30
    REPEAT_LOG_SECONDS = 600

    def __init__(self):
        self.retry_at = 0.0
        self.delay = 0.0
        self.failed_at = None
        self.unreachable_since = None
        self.logged = None
        self.logged_at = 0.0

    def ready(self):
        return time.monotonic() >= self.retry_at

    def succeeded(self):
        if self.failed_at is not None and last_bridge_contact() > self.failed_at:
            if self.unreachable_since is not None:
                log("Hue Bridge reachable again")
            self.__init__()

    def failed(self, what, error):
        now = time.monotonic()
        if isinstance(error, OSError) and (
                self.unreachable_since is None or last_bridge_contact() > self.unreachable_since):
            self.unreachable_since = now
        message = "{} failed: {}".format(what, error)
        if message != self.logged or now - self.logged_at >= self.REPEAT_LOG_SECONDS:
            log(message + "; retrying with backoff")
            self.logged, self.logged_at = message, now
        self.failed_at = now
        self.delay = min(self.MAX_DELAY_SECONDS, max(2.0, self.delay * 2))
        self.retry_at = now + self.delay

    def gave_up(self):
        return (self.unreachable_since is not None
                and last_bridge_contact() <= self.unreachable_since
                and time.monotonic() - self.unreachable_since >= self.GIVE_UP_SECONDS)


def drain_events():
    worker_lock = FileLock(worker_lock_path(), nonblocking=True)
    with worker_lock as acquired:
        if acquired is None:
            return
        reader = LifecycleReader()
        health = WorkerHealth()
        next_check = 0
        while True:
            with QueueLock():
                queue = read_json(queue_path(), [])
                queued = dict(queue[0]) if isinstance(queue, list) and queue else None
            if time.monotonic() >= next_check and health.ready():
                try:
                    reconcile_active_turns(reader)
                    health.succeeded()
                except Exception as error:
                    health.failed("Recovery check", error)
                next_check = time.monotonic() + 2
            if queued is None:
                if health.ready():
                    try:
                        advance_busy_pulse()
                        health.succeeded()
                    except Exception as error:
                        health.failed("Busy lighting update", error)
                with StateLock():
                    state = load_state()
                    with QueueLock():
                        idle = (not state["active"] and not state.get("snapshot")
                                and not state.get("breathing"))
                        if not read_json(queue_path(), []) and (idle or health.gave_up()):
                            if not idle:
                                # A process can stay cut off from the local
                                # network; the next hook starts a fresh worker.
                                log("Hue Bridge unreachable for {} s; worker stopping "
                                    "until the next hook".format(health.GIVE_UP_SECONDS))
                            # Release while holding the queue lock so a new
                            # event can start its worker immediately.
                            worker_lock.release()
                            return
                time.sleep(0.1)
                continue
            try:
                process_event(
                    queued.get("event_name", ""),
                    queued.get("session_id", ""),
                    queued.get("turn_id", ""),
                    transcript_path=queued.get("transcript_path"),
                )
                health.succeeded()
            except Exception as error:
                health.failed("{} event".format(queued.get("event_name", "unknown")), error)
            finally:
                with QueueLock():
                    latest = read_json(queue_path(), [])
                    if latest and latest[0].get("id") == queued.get("id"):
                        latest.pop(0)
                        write_json_atomic(queue_path(), latest)


def hook_command():
    # Print nothing: Claude Code adds UserPromptSubmit and SessionStart stdout
    # to the model's context. Only identifiers are read from the payload; the
    # prompt and assistant text it also carries are never logged or stored.
    if WINDOWS and hasattr(sys.stdin, "reconfigure"):
        # Claude Code sends UTF-8; Windows would otherwise decode the ANSI codepage.
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict) or payload.get("agent_id"):
        return 0  # Subagent events never change the room.
    event_name = payload.get("hook_event_name", "")
    if event_name in SUPPORTED_HOOK_EVENTS:
        try:
            dispatch_event(
                event_name, payload.get("session_id", ""), payload.get("prompt_id", ""),
                payload.get("transcript_path"),
            )
        except Exception as error:
            log("Could not dispatch {}: {}".format(event_name, error))
    return 0


def pair(host, port, wait_seconds):
    fingerprint = bridge_fingerprint(host, port=port)
    client = HueClient(host, fingerprint=fingerprint, port=port)
    device_name = "claude_code_hue#{}".format(socket.gethostname().split(".")[0])
    deadline = time.monotonic() + float(wait_seconds)
    while True:
        try:
            response = client.request(
                "POST", "/api", {"devicetype": device_name, "generateclientkey": True}
            )
            for item in response:
                success = item.get("success") if isinstance(item, dict) else None
                if success and success.get("username"):
                    return fingerprint, success["username"]
        except HueError as error:
            if "link button not pressed" not in str(error).lower():
                raise
        if time.monotonic() >= deadline:
            raise HueError("Pairing timed out; press the Bridge button and try setup again")
        time.sleep(1.0)


def selectable_groups(groups):
    result = []
    for group_id, group in groups.items():
        group_type = group.get("type", "")
        if group_type in SUPPORTED_GROUP_TYPES:
            result.append(
                {
                    "id": str(group_id),
                    "name": group.get("name", "Unnamed"),
                    "type": group_type,
                    "lights": list(group.get("lights", [])),
                }
            )
    return sorted(result, key=lambda item: (item["type"] != "Room", item["name"].lower()))


def select_group(groups, selector):
    candidates = selectable_groups(groups)
    selector_folded = str(selector).casefold()
    matches = [
        item
        for item in candidates
        if item["id"] == str(selector) or item["name"].casefold() == selector_folded
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise HueError("No Hue room or zone matched {!r}".format(selector))
    raise HueError("More than one Hue group matched {!r}; use its numeric id".format(selector))


def base_config(host, port, fingerprint, username):
    return {
        "version": 1,
        "bridge_host": host,
        "bridge_port": int(port),
        "certificate_sha256": fingerprint,
        "username": username,
        "group_id": None,
        "group_name": None,
        "busy": DEFAULT_BUSY,
        "busy_mode": "solid",
        "breathing_min_ratio": 0.4,
        "breathing_period_seconds": 4.0,
        "busy_hold_seconds": 1.0,
        "busy_interval_seconds": 10.0,
        "complete": DEFAULT_COMPLETE,
        "completion_hold_seconds": 1.0,
        "completion_gap_seconds": 0.35,
        "request_timeout_seconds": 3.0,
        "restore_transitiontime": 2,
        "restore_request_gap_seconds": 0.12,
        "stale_after_seconds": 86400,
    }


def print_groups(groups):
    candidates = selectable_groups(groups)
    if not candidates:
        print("No Hue rooms or zones were found.")
        return
    print("Available Hue rooms and zones:")
    for item in candidates:
        print(
            "  {:>3}  {:<28} {:<10} {} light(s)".format(
                item["id"], item["name"], item["type"], len(item["lights"])
            )
        )


def choose_bridge(bridges):
    if len(bridges) == 1:
        return bridges[0]["internalipaddress"]
    print("Discovered Hue Bridges:")
    for index, bridge in enumerate(bridges, start=1):
        print(
            "  {}. {} ({})".format(
                index,
                bridge.get("internalipaddress"),
                bridge.get("id") or bridge.get("source") or "unknown",
            )
        )
    while True:
        selection = input("Choose a Bridge [1-{}]: ".format(len(bridges))).strip()
        try:
            return bridges[int(selection) - 1]["internalipaddress"]
        except (ValueError, IndexError):
            print("Enter a number from 1 to {}.".format(len(bridges)))


def choose_group(groups):
    candidates = selectable_groups(groups)
    if not candidates:
        raise HueError("No Hue rooms or zones were found")
    print_groups(groups)
    while True:
        selection = input("Choose a room or zone by number or name: ").strip()
        try:
            return select_group(groups, selection)
        except HueError as error:
            print("{}".format(error))


def setup_command(args):
    host = args.bridge
    if not host:
        bridges = discover_bridges()
        if not bridges:
            raise HueError("No Hue Bridge was discovered; pass --bridge with its local IP")
        host = choose_bridge(bridges)

    print("Using Hue Bridge at {}:{}".format(host, args.port))
    print("Press the round link button on the Hue Bridge now.")
    fingerprint, username = pair(host, args.port, args.wait)
    config = base_config(host, args.port, fingerprint, username)
    client = client_from_config(config)
    groups = client.groups()

    group = select_group(groups, args.room) if args.room else choose_group(groups)
    config["group_id"] = group["id"]
    config["group_name"] = group["name"]
    write_json_atomic(config_path(), config)
    print(
        "Configured Hue {}: {} (id {})".format(
            group["type"], group["name"], group["id"]
        )
    )
    print("Next: run 'claude-code-hue test', then 'claude-code-hue install-hooks'.")
    return 0


def discover_command():
    bridges = discover_bridges()
    if not bridges:
        raise HueError("No Hue Bridge was discovered on the local network")
    for bridge in bridges:
        print(
            "{}  {}  {}".format(
                bridge.get("id") or "unknown",
                bridge.get("internalipaddress"),
                bridge.get("source") or "unknown",
            )
        )


def set_room_command(selector):
    config = read_json(config_path(), None)
    if not isinstance(config, dict) or not config.get("username"):
        raise HueError("Run setup before selecting a room")
    client = client_from_config(config)
    group = select_group(client.groups(), selector)
    config["group_id"] = group["id"]
    config["group_name"] = group["name"]
    write_json_atomic(config_path(), config)
    print("Configured Hue {}: {} (id {})".format(group["type"], group["name"], group["id"]))


def rooms_command():
    config = read_json(config_path(), None)
    if not isinstance(config, dict) or not config.get("username"):
        raise HueError("Run setup before listing rooms")
    print_groups(client_from_config(config).groups())


def test_command():
    config = load_config()
    client = client_from_config(config)
    with StateLock():
        state = load_state()
        prepare_completion_snapshot(state, config, client)
        state["pulse"] = None
        save_state(state)
        client.group_action(str(config["group_id"]), config.get("complete", DEFAULT_COMPLETE))
        time.sleep(float(config.get("completion_hold_seconds", 1.0)))
        finish_notification(state, config, client)
    print("Pulsed {} green and restored its prior state.".format(config["group_name"]))


def reset_command():
    config = load_config()
    client = client_from_config(config)
    with StateLock():
        state = load_state()
        restore_breathing(state, config, client)
        if state.get("snapshot"):
            restore_room(
                client,
                state["snapshot"],
                transitiontime=config.get("restore_transitiontime", 2),
                request_gap_seconds=config.get("restore_request_gap_seconds", 0.12),
            )
        save_state(default_state())
    print("Cleared active turns and restored the saved room state.")


def status_command():
    config = read_json(config_path(), {})
    state = load_state()
    result = {
        "configured": bool(config.get("group_id")),
        "bridge_host": config.get("bridge_host"),
        "group_id": config.get("group_id"),
        "group_name": config.get("group_name"),
        "active_turns": len(state.get("active", {})),
        "has_saved_room_state": bool(state.get("snapshot")),
        "log": str(log_path()),
    }
    print(json.dumps(result, indent=2, sort_keys=True))


def recover_command():
    load_config()
    spawn_drain_worker()
    print("Started local recovery checks; running turns remain active.")


def is_our_hook_handler(handler):
    if not isinstance(handler, dict):
        return False
    if handler.get("args") == HOOK_ARGUMENTS:
        return True  # Exec form, as installed on Windows.
    command = str(handler.get("command", ""))
    return "claude_code_hue" in command and command.rstrip().endswith(" hook")


def remove_our_hook_handlers(document):
    hooks = document.setdefault("hooks", {})
    removed = 0
    for event_name in sorted(SUPPORTED_HOOK_EVENTS):
        if event_name not in hooks:
            continue
        groups = hooks.get(event_name, [])
        if not isinstance(groups, list):
            continue
        remaining_groups = []
        for group in groups:
            if not isinstance(group, dict):
                remaining_groups.append(group)
                continue
            handlers = group.get("hooks", [])
            if not isinstance(handlers, list):
                remaining_groups.append(group)
                continue
            kept = [handler for handler in handlers if not is_our_hook_handler(handler)]
            removed += len(handlers) - len(kept)
            if kept:
                updated = dict(group)
                updated["hooks"] = kept
                remaining_groups.append(updated)
        if remaining_groups:
            hooks[event_name] = remaining_groups
        else:
            del hooks[event_name]
    return removed


def hook_handler_command():
    if WINDOWS:
        # Exec form runs without a shell. On Windows a command string would go
        # to Git Bash or PowerShell, which quote paths with spaces differently.
        return {"command": PureWindowsPath(sys.executable).as_posix(), "args": list(HOOK_ARGUMENTS)}
    return {"command": shlex.join([sys.executable] + HOOK_ARGUMENTS)}


def hue_hook_group(status_message, matcher=None, timeout=5):
    handler = {"type": "command"}
    handler.update(hook_handler_command())
    handler.update(timeout=timeout, statusMessage=status_message)
    group = {"hooks": [handler]}
    if matcher:
        group["matcher"] = matcher
    return group


def load_settings_document(path):
    if not path.exists():
        return {"hooks": {}}
    try:
        with path.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise HueError("Could not read {}: {}".format(path, error)) from error
    if not isinstance(document, dict):
        raise HueError("{} must contain a JSON object".format(path))
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise HueError("The 'hooks' value in {} must be a JSON object".format(path))
    return document


def backup_settings(path):
    if not path.exists():
        return None
    backup = path.with_name(
        "{}.backup-{}".format(path.name, time.strftime("%Y%m%d-%H%M%S"))
    )
    shutil.copy2(path, backup)
    try:
        os.chmod(backup, 0o600)
    except OSError:
        pass
    return backup


def write_settings_atomic(path, document):
    """Keep the user's key order and file mode, and follow a symlinked file."""
    path = Path(os.path.realpath(path))
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = 0o600
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(path.parent), prefix="." + path.name + ".", text=True
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, mode)
        replace_file(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def install_hooks_command():
    path = settings_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    document = load_settings_document(path)
    remove_our_hook_handlers(document)
    hooks = document["hooks"]
    # Not "compact": it fires mid-session while a turn can still be running.
    hooks.setdefault("SessionStart", []).append(
        hue_hook_group("Reconciling interrupted Hue turns", matcher="startup|resume|clear")
    )
    hooks.setdefault("SessionEnd", []).append(
        hue_hook_group("Cleaning up Hue turn state", timeout=3)
    )
    hooks.setdefault("UserPromptSubmit", []).append(
        hue_hook_group("Setting Hue room to busy")
    )
    hooks.setdefault("Stop", []).append(
        hue_hook_group("Notifying Hue room of completion")
    )
    hooks.setdefault("StopFailure", []).append(
        hue_hook_group("Clearing failed Hue turn")
    )
    backup = backup_settings(path)
    write_settings_atomic(path, document)
    print("Installed Claude Code hooks in {}.".format(path))
    if backup:
        print("Backed up the previous file to {}.".format(backup))
    print("Running Claude Code sessions pick them up automatically; restart any that do not.")


def uninstall_hooks_command():
    path = settings_path()
    document = load_settings_document(path)
    removed = remove_our_hook_handlers(document)
    if not removed:
        print("No claude-code-hue hooks were installed in {}.".format(path))
        return
    if not document["hooks"]:
        del document["hooks"]
    backup = backup_settings(path)
    write_settings_atomic(path, document)
    print("Removed {} claude-code-hue hook(s) from {}.".format(removed, path))
    if backup:
        print("Backed up the previous file to {}.".format(backup))


def build_parser():
    parser = argparse.ArgumentParser(prog=PROJECT_NAME, description=__doc__)
    public_commands = (
        "{setup,discover,set-room,rooms,test,reset,recover,status,install-hooks,"
        "uninstall-hooks}"
    )
    subparsers = parser.add_subparsers(
        dest="command", required=True, metavar=public_commands
    )

    setup_parser = subparsers.add_parser("setup", help="discover and pair with a Hue Bridge")
    setup_parser.add_argument("--bridge", help="local bridge hostname or IP")
    setup_parser.add_argument("--port", type=int, default=443)
    setup_parser.add_argument("--room", help="Hue room/zone name or numeric id")
    setup_parser.add_argument("--wait", type=float, default=60.0)

    subparsers.add_parser("discover", help="discover Hue Bridges on the local network")
    set_room_parser = subparsers.add_parser("set-room", help="select a Hue room or zone")
    set_room_parser.add_argument("selector")
    subparsers.add_parser("rooms", help="list Hue rooms and zones")
    subparsers.add_parser("test", help="pulse the configured room and restore it")
    subparsers.add_parser("reset", help="clear active turns and restore saved lighting")
    subparsers.add_parser("recover", help="check local turn records without clearing running work")
    subparsers.add_parser("status", help="show non-secret indicator status")
    subparsers.add_parser("install-hooks", help="add the hooks to Claude Code's user settings")
    subparsers.add_parser("uninstall-hooks", help="remove the hooks from Claude Code's user settings")
    subparsers.add_parser("hook")
    subparsers.add_parser("_drain")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "setup":
            return setup_command(args)
        if args.command == "discover":
            discover_command()
        elif args.command == "set-room":
            set_room_command(args.selector)
        elif args.command == "rooms":
            rooms_command()
        elif args.command == "test":
            test_command()
        elif args.command == "reset":
            reset_command()
        elif args.command == "recover":
            recover_command()
        elif args.command == "status":
            status_command()
        elif args.command == "install-hooks":
            install_hooks_command()
        elif args.command == "uninstall-hooks":
            uninstall_hooks_command()
        elif args.command == "hook":
            return hook_command()
        elif args.command == "_drain":
            drain_events()
        return 0
    except HueError as error:
        print("Error: {}".format(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
