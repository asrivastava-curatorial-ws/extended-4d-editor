"""4D-VR-THEATRE/1 control protocol: canonical message definitions.

SHARED MODULE. Identical copies of this file live in:
  * 4D-VR-Theatre-Control-Server   (fourdvr_server/protocol.py)
  * 4D-VR-Theatre-Motion-Controller (fourdvr_motion/protocol.py)
The VR Player (GDScript) implements the same schema in scripts/net/protocol.gd.
The human-readable specification is PROTOCOL.md; the machine-readable schema is
protocol/4d-vr-theatre-protocol-1.0.0.schema.json (generated from this module).

Every WebSocket text frame is one JSON object (the *envelope*):

    {"protocol": "4D-VR-THEATRE/1", "type": "PLAY", "id": "<hex>",
     "ts": <sender clock seconds>, "request_id": "<hex>|null", "payload": {...}}
"""

from __future__ import annotations

import json
import math
import time
import uuid
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

PROTOCOL_ID = "4D-VR-THEATRE/1"
PROTOCOL_VERSION = "1.0.0"
PROTOCOL_MAJOR = 1

DEVICE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"


class ProtocolError(ValueError):
    """Raised for malformed, unknown or schema-violating messages."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class MsgType(StrEnum):
    REGISTER = "REGISTER"
    AUTHENTICATE = "AUTHENTICATE"
    HEARTBEAT = "HEARTBEAT"
    TIME_SYNC = "TIME_SYNC"
    MANIFEST = "MANIFEST"
    PREPARE = "PREPARE"
    READY = "READY"
    PLAY = "PLAY"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    STOP = "STOP"
    RESTART = "RESTART"
    SYNC = "SYNC"
    BLACK_SCREEN = "BLACK_SCREEN"
    STATUS = "STATUS"
    ERROR = "ERROR"


class DeviceType(StrEnum):
    VR_PLAYER = "vr_player"
    MOTION_CONTROLLER = "motion_controller"


class TimelineState(StrEnum):
    IDLE = "IDLE"
    PREPARING = "PREPARING"
    PLAYING = "PLAYING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


class Projection(StrEnum):
    FLAT = "flat"
    DEG180 = "180"
    DEG360 = "360"


class StereoMode(StrEnum):
    MONO = "mono"
    SBS = "sbs"
    OU = "ou"


class ErrorCode(StrEnum):
    BAD_MESSAGE = "BAD_MESSAGE"
    UNSUPPORTED_PROTOCOL = "UNSUPPORTED_PROTOCOL"
    AUTH_FAILED = "AUTH_FAILED"
    NOT_AUTHENTICATED = "NOT_AUTHENTICATED"
    DEVICE_DISABLED = "DEVICE_DISABLED"
    DUPLICATE_SESSION = "DUPLICATE_SESSION"
    CONTENT_ERROR = "CONTENT_ERROR"
    CHECKSUM_MISMATCH = "CHECKSUM_MISMATCH"
    STORAGE_FULL = "STORAGE_FULL"
    PLAYBACK_ERROR = "PLAYBACK_ERROR"
    MOTION_FAULT = "MOTION_FAULT"
    LINK_FAULT = "LINK_FAULT"
    INTERNAL = "INTERNAL"


class _Model(BaseModel):
    """Base for payloads: unknown fields are ignored (forward compatible minor versions)."""

    model_config = ConfigDict(extra="ignore", frozen=False)


# ---------------------------------------------------------------- envelope
class Envelope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    protocol: str
    type: MsgType
    id: str = Field(min_length=1, max_length=64)
    ts: float
    request_id: str | None = Field(default=None, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)


# ------------------------------------------------------- client -> server
class RegisterPayload(_Model):
    device_id: str = Field(pattern=DEVICE_ID_PATTERN)
    device_type: DeviceType
    app_version: str = Field(max_length=32)
    protocol_version: str = Field(max_length=16)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    hardware: dict[str, Any] = Field(default_factory=dict)


class AuthResponse(_Model):
    stage: Literal["response"] = "response"
    device_id: str = Field(pattern=DEVICE_ID_PATTERN)
    nonce: str = Field(min_length=16, max_length=128)
    mac: str = Field(pattern=r"^[0-9a-f]{64}$")


class HeartbeatClient(_Model):
    state: str = Field(default="UNKNOWN", max_length=32)
    programme_id: int | None = None
    uptime_s: float = 0.0


class TimeSyncRequest(_Model):
    t0: float


class ReadyPayload(_Model):
    programme_id: int
    timeline_id: int
    ok: bool
    detail: str = Field(default="", max_length=500)
    video_sha256: str | None = None
    motion_sha256: str | None = None


class SyncRequest(_Model):
    reason: str = Field(default="", max_length=64)


class StatusPayload(BaseModel):
    """Device status report. Common fields are typed; product-specific fields are kept."""

    model_config = ConfigDict(extra="allow")

    state: str = Field(max_length=32)
    programme_id: int | None = None
    timeline_id: int | None = None
    position_s: float | None = None
    expected_position_s: float | None = None
    drift_s: float | None = None
    clock_offset_s: float | None = None
    rtt_s: float | None = None
    last_request_id: str | None = None
    app_version: str | None = None
    errors: list[str] = Field(default_factory=list)


class ErrorPayload(_Model):
    code: str = Field(max_length=64)
    message: str = Field(default="", max_length=1000)
    fatal: bool = False


# ------------------------------------------------------- server -> client
class AuthChallenge(_Model):
    stage: Literal["challenge"] = "challenge"
    nonce: str
    algorithm: Literal["HMAC-SHA256"] = "HMAC-SHA256"
    server_version: str
    protocol_version: str = PROTOCOL_VERSION


class AuthResult(_Model):
    stage: Literal["result"] = "result"
    result: Literal["ok"] = "ok"
    session_id: str
    content_token: str
    heartbeat_interval_s: float
    offline_timeout_s: float
    server_version: str
    protocol_version: str = PROTOCOL_VERSION
    server_time: float


class HeartbeatServer(_Model):
    server_time: float


class TimeSyncReply(_Model):
    t0: float
    t1: float
    t2: float


class AssetRef(_Model):
    url: str
    sha256: str = Field(pattern=SHA256_PATTERN)
    size: int = Field(ge=0)
    filename: str = ""


class VideoAsset(AssetRef):
    width: int = 0
    height: int = 0
    container: str = ""
    frame_rate: float = 0.0


class MotionAsset(AssetRef):
    sample_rate_hz: float = 0.0
    duration_s: float = 0.0


class ProgrammeAsset(_Model):
    programme_id: int
    name: str
    revision: int
    duration_s: float = Field(gt=0)
    projection: Projection
    stereo: StereoMode
    loop: bool = True
    video: VideoAsset
    motion: MotionAsset | None = None


class ManifestPayload(_Model):
    programmes: list[ProgrammeAsset]


class PreparePayload(_Model):
    programme: ProgrammeAsset
    timeline_id: int
    deadline_s: float


class Timeline(_Model):
    timeline_id: int = 0
    state: TimelineState = TimelineState.IDLE
    programme_id: int | None = None
    epoch: float | None = None  # master time at which programme position 0 occurs (PLAYING)
    position_s: float = 0.0  # frozen position (PAUSED/STOPPED/PREPARING)
    duration_s: float = 0.0
    loop: bool = True
    start_at: float | None = None
    server_time: float = 0.0

    def position_at(self, master_time: float) -> float:
        return timeline_position(self.state, self.epoch, self.position_s, self.duration_s, self.loop, master_time)


class PlayPayload(_Model):
    programme_id: int
    timeline_id: int
    start_at: float
    position_s: float = 0.0
    duration_s: float
    loop: bool = True


class PausePayload(_Model):
    programme_id: int
    timeline_id: int
    pause_at: float
    position_s: float


class ResumePayload(_Model):
    programme_id: int
    timeline_id: int
    resume_at: float
    position_s: float


class StopPayload(_Model):
    programme_id: int | None
    timeline_id: int
    stop_at: float


class RestartPayload(_Model):
    programme_id: int
    timeline_id: int
    start_at: float
    duration_s: float
    loop: bool = True


class SyncState(_Model):
    timeline: Timeline
    force: bool = False
    black_screen: bool = False


class BlackScreenPayload(_Model):
    enabled: bool


AuthServer = Annotated[AuthChallenge | AuthResult, Field(discriminator="stage")]

CLIENT_TO_SERVER: dict[MsgType, Any] = {
    MsgType.REGISTER: RegisterPayload,
    MsgType.AUTHENTICATE: AuthResponse,
    MsgType.HEARTBEAT: HeartbeatClient,
    MsgType.TIME_SYNC: TimeSyncRequest,
    MsgType.READY: ReadyPayload,
    MsgType.SYNC: SyncRequest,
    MsgType.STATUS: StatusPayload,
    MsgType.ERROR: ErrorPayload,
}

SERVER_TO_CLIENT: dict[MsgType, Any] = {
    MsgType.AUTHENTICATE: AuthServer,
    MsgType.HEARTBEAT: HeartbeatServer,
    MsgType.TIME_SYNC: TimeSyncReply,
    MsgType.MANIFEST: ManifestPayload,
    MsgType.PREPARE: PreparePayload,
    MsgType.PLAY: PlayPayload,
    MsgType.PAUSE: PausePayload,
    MsgType.RESUME: ResumePayload,
    MsgType.STOP: StopPayload,
    MsgType.RESTART: RestartPayload,
    MsgType.SYNC: SyncState,
    MsgType.BLACK_SCREEN: BlackScreenPayload,
    MsgType.ERROR: ErrorPayload,
}

_ADAPTERS: dict[tuple[str, MsgType], TypeAdapter[Any]] = {}


def _adapter(direction: str, msg_type: MsgType) -> TypeAdapter[Any]:
    key = (direction, msg_type)
    if key not in _ADAPTERS:
        table = CLIENT_TO_SERVER if direction == "c2s" else SERVER_TO_CLIENT
        if msg_type not in table:
            raise ProtocolError(ErrorCode.BAD_MESSAGE, f"{msg_type} is not valid in direction {direction}")
        _ADAPTERS[key] = TypeAdapter(table[msg_type])
    return _ADAPTERS[key]


# ----------------------------------------------------------------- helpers
def new_id() -> str:
    return uuid.uuid4().hex


def make_message(
    msg_type: MsgType | str,
    payload: BaseModel | dict[str, Any] | None = None,
    *,
    ts: float | None = None,
    request_id: str | None = None,
    msg_id: str | None = None,
) -> dict[str, Any]:
    if isinstance(payload, BaseModel):
        body = payload.model_dump(mode="json")
    else:
        body = dict(payload or {})
    return {
        "protocol": PROTOCOL_ID,
        "type": str(MsgType(msg_type)),
        "id": msg_id or new_id(),
        "ts": time.time() if ts is None else ts,
        "request_id": request_id,
        "payload": body,
    }


def encode(message: dict[str, Any]) -> str:
    return json.dumps(message, separators=(",", ":"), allow_nan=False)


def decode(text: str | bytes) -> Envelope:
    """Parse and validate an envelope (not the payload)."""
    try:
        raw = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ProtocolError(ErrorCode.BAD_MESSAGE, f"invalid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProtocolError(ErrorCode.BAD_MESSAGE, "message must be a JSON object")
    proto = raw.get("protocol")
    if not isinstance(proto, str) or not proto.startswith("4D-VR-THEATRE/"):
        raise ProtocolError(ErrorCode.UNSUPPORTED_PROTOCOL, f"unknown protocol {proto!r}")
    if proto != PROTOCOL_ID:
        raise ProtocolError(ErrorCode.UNSUPPORTED_PROTOCOL, f"protocol {proto} not supported (need {PROTOCOL_ID})")
    try:
        return Envelope.model_validate(raw)
    except ValidationError as exc:
        raise ProtocolError(ErrorCode.BAD_MESSAGE, _short_error(exc)) from exc


def parse_payload(envelope: Envelope, direction: Literal["c2s", "s2c"]) -> Any:
    """Validate the payload of an envelope for the given direction and return the model."""
    try:
        return _adapter(direction, envelope.type).validate_python(envelope.payload)
    except ValidationError as exc:
        raise ProtocolError(ErrorCode.BAD_MESSAGE, f"{envelope.type}: {_short_error(exc)}") from exc


def check_version_compatible(version: str) -> bool:
    """Same major version => compatible (semantic versioning)."""
    try:
        return int(version.split(".")[0]) == PROTOCOL_MAJOR
    except (ValueError, IndexError):
        return False


def timeline_position(
    state: TimelineState | str,
    epoch: float | None,
    position_s: float,
    duration_s: float,
    loop: bool,
    master_time: float,
) -> float:
    """Programme position at master_time. Negative while a PLAY is scheduled but not started."""
    if TimelineState(state) == TimelineState.PLAYING and epoch is not None:
        p = master_time - epoch
        if p < 0:
            return p
        if duration_s > 0:
            if loop:
                return math.fmod(p, duration_s)
            return min(p, duration_s)
        return p
    return position_s


def loop_count(epoch: float | None, duration_s: float, master_time: float) -> int:
    if epoch is None or duration_s <= 0 or master_time < epoch:
        return 0
    return int((master_time - epoch) // duration_s)


def protocol_json_schema() -> dict[str, Any]:
    def schemas(table: dict[MsgType, Any]) -> dict[str, Any]:
        return {str(k): TypeAdapter(v).json_schema() for k, v in table.items()}

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"{PROTOCOL_ID} control protocol {PROTOCOL_VERSION}",
        "protocol": PROTOCOL_ID,
        "version": PROTOCOL_VERSION,
        "envelope": Envelope.model_json_schema(),
        "client_to_server": schemas(CLIENT_TO_SERVER),
        "server_to_client": schemas(SERVER_TO_CLIENT),
    }


def _short_error(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:5]:
        loc = ".".join(str(x) for x in err.get("loc", ()))
        parts.append(f"{loc}: {err.get('msg')}")
    return "; ".join(parts)


if __name__ == "__main__":  # pragma: no cover - schema export helper
    print(json.dumps(protocol_json_schema(), indent=2))
