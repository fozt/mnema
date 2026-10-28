"""System audio as a live source: a Core Audio process tap (macOS 14.2+, no drivers).

On a call the other side plays through the Mac's speakers: the microphone hears them only
acoustically, and not at all with headphones. CoreAudioTapSource takes the clean digital system
signal; together with MicSource this separates speakers by source: mic is "me", system is "them".

A CATap (CATapDescription + AudioHardwareCreateProcessTap + an aggregate device) needs only the
system audio recording permission, without screen recording or the sharing indicator. The tap
delivers float32 at the output device's rate and channel count; numpy converts it to the
AudioSource contract (PCM16 mono 16 kHz) by averaging channels and decimating (or linear
interpolation). ObjC and ctypes imports are lazy, so the module imports on any OS (thin client).
"""

from __future__ import annotations

import logging
import platform
import queue
import shutil
import subprocess
import threading
from typing import Any

from ..core.base import MediaError
from ..i18n import _
from .sources import EnergyChronicle

logger = logging.getLogger(__name__)
# no commands in app texts: the cause and what to install go to the log
SYSTEM_AUDIO_FAILED = "Could not record system audio. See the Mnema log for details"

_TARGET_RATE = 16000
_QUEUE_SECONDS = 30  # same queue limit as MicSource (see sources.py)
_TAP_BLOCK_SECONDS = 0.02  # IOProc gets the device buffer (~10-20 ms); estimate for the queue
_TAP_MIN_MACOS = (14, 2)  # AudioHardwareCreateProcessTap appeared in macOS 14.2


def to_pcm16_mono(
    data: bytes, *, rate: float, channels: int, planar: bool, target_rate: int = _TARGET_RATE
) -> bytes:
    """float32 frame (planar or interleaved) -> PCM16 mono at target_rate (AudioSource contract).

    Channels are averaged; with an integer resampling factor groups of samples are averaged (a
    crude anti-aliasing filter too), otherwise linear interpolation is used.
    """
    import numpy as np

    a = np.frombuffer(data, dtype=np.float32)
    if channels > 1 and a.size % channels == 0:
        a = a.reshape((channels, -1) if planar else (-1, channels)).mean(axis=0 if planar else 1)
    if a.size and rate != target_rate:
        factor = rate / target_rate
        if factor == int(factor) and a.size % int(factor) == 0:
            a = a.reshape(-1, int(factor)).mean(axis=1)
        else:
            n_out = max(1, round(a.size * target_rate / rate))
            a = np.interp(np.arange(n_out) * (a.size / n_out), np.arange(a.size), a)
    return np.clip(np.rint(a * 32768.0), -32768, 32767).astype(np.int16).tobytes()


class _BufferedSource:
    """Common base of system audio sources: a bounded PCM16 mono 16 kHz queue that drops the
    oldest frames, the energy chronicle and a batched read(); platform classes add only the
    capture (a Core Audio tap on macOS, parec on Linux)."""

    def __init__(self, *, label: str, tee: Any, block_seconds: float) -> None:
        # tee: Callable[[bytes], None]; the recording comes first, frames reach it before the queue
        self.name = label
        self._tee = tee
        self.recording_error: str | None = None
        maxblocks = max(1, int(_QUEUE_SECONDS / block_seconds))
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=maxblocks)
        self._dropped_bytes = 0
        self._chronicle = EnergyChronicle(byterate=_TARGET_RATE * 2)  # PCM16: 2 bytes per sample
        self._stopped = False

    @property
    def dropped_seconds(self) -> float:
        """Seconds of audio dropped because the queue overflowed (PCM16 mono 16 kHz)."""
        return self._dropped_bytes / (_TARGET_RATE * 2)

    def speech_energy(self, start: float, end: float) -> float | None:
        """Peak RMS of the delivered stream over [start, end] seconds (ASR stream time)."""
        return self._chronicle.energy(start, end)

    def read(self) -> bytes | None:
        """All queued frames at once: one per visit would not drain the queue while the session
        loop shares turns between sources."""
        try:
            first = self._queue.get(timeout=0.5)
        except queue.Empty:
            return None if self._stopped else b""
        if first is None:
            return None
        self._chronicle.push(first)  # the chronicle follows what is delivered, block by block
        parts = [first]
        while True:
            try:
                nxt = self._queue.get_nowait()
            except queue.Empty:
                break
            if nxt is None:  # stop sentinel: deliver what was collected, the next read returns None
                self._stopped = True
                break
            self._chronicle.push(nxt)
            parts.append(nxt)
        return b"".join(parts)

    def _emit(self, pcm: bytes) -> None:
        """Deliver a frame: first to the recording (tee), then to the dropping queue."""
        if self._tee is not None:
            try:
                self._tee(pcm)
            except Exception:  # noqa: BLE001 - the session stops capture outside the callback
                from .remarks import track_stopped

                logger.warning("track %s is not being written", self.name, exc_info=True)
                self.recording_error = self.recording_error or track_stopped(self.name)
        self._put_dropping_oldest(pcm)

    def _put_dropping_oldest(self, item: bytes | None) -> None:
        """Enqueue without blocking; on overflow drop the oldest frames."""
        while True:
            try:
                self._queue.put_nowait(item)
                return
            except queue.Full:
                try:
                    old = self._queue.get_nowait()
                    if old is not None:
                        self._dropped_bytes += len(old)
                except queue.Empty:  # raced with the reader: there is room now, put again
                    pass


class CoreAudioTapSource(_BufferedSource):
    """System audio through a Core Audio process tap (macOS 14.2+).

    CATapDescription (global stereo mix of all processes) -> AudioHardwareCreateProcessTap -> a
    private aggregate device with the tap -> AudioDeviceIOProc. Frames are converted to PCM16
    mono 16 kHz into a bounded queue that drops the oldest on overflow, reported through
    dropped_seconds (as in MicSource). It needs only the system audio recording permission
    (asked on first use); the screen is not involved.
    """

    def __init__(self, *, label: str = "system", tee: Any = None) -> None:
        super().__init__(label=label, tee=tee, block_seconds=_TAP_BLOCK_SECONDS)
        self._ca: Any = None  # CoreAudio CDLL, kept alive because our C objects live in it
        self._tap_id = 0
        self._agg_id = 0
        self._ioproc_id: Any = None
        self._ioproc_ref: Any = None  # CFUNCTYPE object; without a reference GC frees the callback
        self._fmt: tuple[float, int] | None = None  # tap (rate, channels), kAudioTapPropertyFormat

    def available(self) -> bool:
        if platform.system() != "Darwin":
            return False
        try:
            import objc  # noqa: F401 - pyobjc-core (extra live/menubar)
        except Exception:
            return False
        parts = platform.mac_ver()[0].split(".")
        try:
            ver = (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
        except ValueError:
            return False
        return ver >= _TAP_MIN_MACOS

    # ── ctypes bindings for the CoreAudio HAL ───────────────────────────────────

    @staticmethod
    def _load_coreaudio() -> Any:
        import ctypes

        ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        u32p = ctypes.POINTER(ctypes.c_uint32)
        ca.AudioHardwareCreateProcessTap.argtypes = [ctypes.c_void_p, u32p]
        ca.AudioHardwareCreateProcessTap.restype = ctypes.c_int32
        ca.AudioHardwareDestroyProcessTap.argtypes = [ctypes.c_uint32]
        ca.AudioHardwareCreateAggregateDevice.argtypes = [ctypes.c_void_p, u32p]
        ca.AudioHardwareCreateAggregateDevice.restype = ctypes.c_int32
        ca.AudioHardwareDestroyAggregateDevice.argtypes = [ctypes.c_uint32]
        return ca

    def start(self) -> None:
        import ctypes
        import uuid as _uuid

        import objc
        from Foundation import NSDictionary  # ty: ignore[unresolved-import]

        ca = self._load_coreaudio()  # loading the framework registers its ObjC classes
        self._ca = ca
        CATapDescription = objc.lookUpClass("CATapDescription")  # ty: ignore[unresolved-attribute]

        # global mix of all processes; private: other apps do not see the tap
        desc = CATapDescription.alloc().initStereoGlobalTapButExcludeProcesses_([])
        desc.setName_("mnema system audio")
        desc.setPrivate_(True)

        tap_id = ctypes.c_uint32(0)
        status = ca.AudioHardwareCreateProcessTap(
            objc.pyobjc_id(desc),  # ty: ignore[unresolved-attribute]
            ctypes.byref(tap_id),
        )
        if status != 0 or not tap_id.value:
            logger.warning("Core Audio tap not created, status %s", status)
            raise MediaError(system_audio_denied_text())
        self._tap_id = tap_id.value

        try:
            self._fmt = self._read_tap_format(ca, tap_id.value)
            # a private aggregate device with one sub-tap: CoreAudio drives the IOProc with the
            # tap's data through it; drift compensation is Apple's advice for taps
            agg_uid = str(_uuid.uuid4())
            cfg = NSDictionary.dictionaryWithDictionary_(
                {
                    "name": "mnema system tap",
                    "uid": agg_uid,
                    "private": True,
                    "taps": [{"uid": desc.UUID().UUIDString(), "drift": True}],
                }
            )
            agg_id = ctypes.c_uint32(0)
            status = ca.AudioHardwareCreateAggregateDevice(
                objc.pyobjc_id(cfg),  # ty: ignore[unresolved-attribute]
                ctypes.byref(agg_id),
            )
            if status != 0 or not agg_id.value:
                raise MediaError(
                    _(
                        "Core Audio did not create the aggregate device (status {status}).",
                        status=status,
                    )
                )
            self._agg_id = agg_id.value

            self._install_ioproc(ca, agg_id.value)
            status = ca.AudioDeviceStart(agg_id.value, self._ioproc_id)
            if status != 0:
                raise MediaError(
                    _("Core Audio did not start the capture (status {status}).", status=status)
                )
        except Exception:
            self._teardown()
            raise

    def stop(self) -> None:
        self._stopped = True
        self._teardown()
        self._put_dropping_oldest(None)  # end sentinel, without blocking on a full queue

    # ── internals ─────────────────────────────────────────────────────────────

    @staticmethod
    def _read_tap_format(ca: Any, tap_id: int) -> tuple[float, int]:
        """kAudioTapPropertyFormat ('tfmt') -> (rate, channels); 48 kHz stereo on failure."""
        import ctypes

        class _ASBD(ctypes.Structure):
            _fields_ = [
                ("mSampleRate", ctypes.c_double),
                ("mFormatID", ctypes.c_uint32),
                ("mFormatFlags", ctypes.c_uint32),
                ("mBytesPerPacket", ctypes.c_uint32),
                ("mFramesPerPacket", ctypes.c_uint32),
                ("mBytesPerFrame", ctypes.c_uint32),
                ("mChannelsPerFrame", ctypes.c_uint32),
                ("mBitsPerChannel", ctypes.c_uint32),
                ("mReserved", ctypes.c_uint32),
            ]

        class _Addr(ctypes.Structure):
            _fields_ = [
                ("mSelector", ctypes.c_uint32),
                ("mScope", ctypes.c_uint32),
                ("mElement", ctypes.c_uint32),
            ]

        addr = _Addr(int.from_bytes(b"tfmt", "big"), int.from_bytes(b"glob", "big"), 0)
        asbd = _ASBD()
        size = ctypes.c_uint32(ctypes.sizeof(asbd))
        status = ca.AudioObjectGetPropertyData(
            ctypes.c_uint32(tap_id),
            ctypes.byref(addr),
            ctypes.c_uint32(0),
            None,
            ctypes.byref(size),
            ctypes.byref(asbd),
        )
        if status != 0 or not asbd.mSampleRate:
            return 48000.0, 2
        return float(asbd.mSampleRate), max(1, int(asbd.mChannelsPerFrame))

    def _install_ioproc(self, ca: Any, device_id: int) -> None:
        import ctypes

        class _AudioBuffer(ctypes.Structure):
            _fields_ = [
                ("mNumberChannels", ctypes.c_uint32),
                ("mDataByteSize", ctypes.c_uint32),
                ("mData", ctypes.c_void_p),
            ]

        class _AudioBufferList(ctypes.Structure):
            # the array is oversized; only the first mNumberBuffers entries are read
            _fields_ = [("mNumberBuffers", ctypes.c_uint32), ("mBuffers", _AudioBuffer * 8)]

        ioproc_t = ctypes.CFUNCTYPE(
            ctypes.c_int32,
            ctypes.c_uint32,  # inDevice
            ctypes.c_void_p,  # inNow
            ctypes.POINTER(_AudioBufferList),  # inInputData: the tap's data
            ctypes.c_void_p,  # inInputTime
            ctypes.c_void_p,  # outOutputData
            ctypes.c_void_p,  # inOutputTime
            ctypes.c_void_p,  # inClientData
        )

        def ioproc(_dev, _now, in_data, _in_time, _out_data, _out_time, _client) -> int:
            try:
                if not in_data or self._stopped:
                    return 0
                abl = in_data.contents
                n = min(int(abl.mNumberBuffers), 8)
                if n == 0:
                    return 0
                chunks = []
                for i in range(n):
                    buf = abl.mBuffers[i]
                    if buf.mData and buf.mDataByteSize:
                        chunks.append(ctypes.string_at(buf.mData, buf.mDataByteSize))
                if not chunks:
                    return 0
                rate, fmt_ch = self._fmt or (48000.0, 2)
                planar = n > 1
                channels = n if planar else max(1, int(abl.mBuffers[0].mNumberChannels or fmt_ch))
                pcm = to_pcm16_mono(b"".join(chunks), rate=rate, channels=channels, planar=planar)
                if pcm:
                    self._emit(pcm)
            except Exception:  # noqa: BLE001 - CoreAudio's real-time thread must not raise
                pass
            return 0

        self._ioproc_ref = ioproc_t(ioproc)
        ca.AudioDeviceCreateIOProcID.argtypes = [
            ctypes.c_uint32,
            ioproc_t,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        proc_id = ctypes.c_void_p(0)
        status = ca.AudioDeviceCreateIOProcID(
            ctypes.c_uint32(device_id), self._ioproc_ref, None, ctypes.byref(proc_id)
        )
        if status != 0 or not proc_id.value:
            raise MediaError(
                _("Core Audio did not accept the audio callback (status {status}).", status=status)
            )
        self._ioproc_id = proc_id

    def _teardown(self) -> None:
        """Tear the chain down in reverse order; every step is best effort."""
        ca = self._ca
        if ca is None:
            return
        try:
            if self._agg_id and self._ioproc_id:
                ca.AudioDeviceStop(self._agg_id, self._ioproc_id)
                ca.AudioDeviceDestroyIOProcID(self._agg_id, self._ioproc_id)
        except Exception:  # noqa: BLE001
            pass
        self._ioproc_id = None
        try:
            if self._agg_id:
                ca.AudioHardwareDestroyAggregateDevice(self._agg_id)
        except Exception:  # noqa: BLE001
            pass
        self._agg_id = 0
        try:
            if self._tap_id:
                ca.AudioHardwareDestroyProcessTap(self._tap_id)
        except Exception:  # noqa: BLE001
            pass
        self._tap_id = 0
        self._ioproc_ref = None


class PulseMonitorSource(_BufferedSource):
    """System audio on Linux: the PulseAudio/PipeWire monitor source through `parec`.

    Every output device (sink) has a "<sink>.monitor" source with everything the system plays.
    parec writes raw s16le mono 16 kHz PCM to stdout; a reader thread feeds the recording (tee)
    and the queue. Ubuntu with PipeWire works out of the box (pipewire-pulse provides pactl and
    parec).
    """

    _BLOCK_SECONDS = 0.1  # stdout is read in 100 ms pieces

    def __init__(self, *, label: str = "system", tee: Any = None) -> None:
        super().__init__(label=label, tee=tee, block_seconds=self._BLOCK_SECONDS)
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None

    def available(self) -> bool:
        if platform.system() != "Linux":
            return False
        if not (shutil.which("parec") and shutil.which("pactl")):
            return False
        try:  # the sound server is alive (a headless server without PulseAudio is False)
            subprocess.run(["pactl", "info"], capture_output=True, timeout=5, check=True)
        except Exception:  # noqa: BLE001
            return False
        return True

    def start(self) -> None:
        try:
            sink = subprocess.run(
                ["pactl", "get-default-sink"], capture_output=True, text=True, timeout=5
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("PulseAudio/PipeWire did not answer (pactl get-default-sink): %s", e)
            raise MediaError(_(SYSTEM_AUDIO_FAILED)) from e
        name = sink.stdout.strip()
        if sink.returncode != 0 or not name:
            logger.warning(
                "cannot determine the audio output device (pactl get-default-sink): %s",
                sink.stderr.strip() or "empty reply",
            )
            raise MediaError(_(SYSTEM_AUDIO_FAILED))
        monitor = f"{name}.monitor"
        try:
            self._proc = subprocess.Popen(
                [
                    "parec",
                    f"--device={monitor}",
                    "--format=s16le",
                    f"--rate={_TARGET_RATE}",
                    "--channels=1",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError as e:
            logger.warning("parec is missing: sudo apt install pulseaudio-utils")
            raise MediaError(_(SYSTEM_AUDIO_FAILED)) from e
        self._reader = threading.Thread(target=self._pump, name="pulse-monitor", daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        chunk = int(_TARGET_RATE * 2 * self._BLOCK_SECONDS)
        stdout = self._proc.stdout
        while True:
            try:
                data = stdout.read(chunk)
            except Exception:  # noqa: BLE001 - the pipe was closed by stop
                break
            if not data:  # parec died (the sound server restarted): an end, not silence
                break
            self._emit(data)
        self._stopped = True
        self._put_dropping_oldest(None)

    def stop(self) -> None:
        self._stopped = True
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001 - best effort
                pass
        self._put_dropping_oldest(None)  # sentinel, in case the pump did not send one


def system_source(*, label: str = "system", tee: Any = None):
    """The system audio source of this platform: a Core Audio tap on macOS, the PulseAudio or
    PipeWire monitor on Linux. Elsewhere the CATap class, whose available() is False."""
    if platform.system() == "Linux":
        return PulseMonitorSource(label=label, tee=tee)
    return CoreAudioTapSource(label=label, tee=tee)


def system_unavailable_hint() -> str:
    """Why system audio does not work here, in plain words for this platform."""
    if platform.system() == "Linux":
        return _(
            "System audio is unavailable. Install the pulseaudio-utils package and restart Mnema"
        )
    return _(
        "System audio can be recorded only on macOS 14.2 or later. Update macOS or turn off "
        "“System audio”"
    )


def system_audio_denied_text() -> str:
    """No access to system audio: what to allow, and to whom macOS gives the permission."""
    if in_mnema_app():
        return _(
            "No access to system audio. Allow Mnema to record system audio in System Settings, "
            "Privacy & Security, and restart Mnema"
        )
    return _(
        "No access to system audio. Allow the terminal to record system audio in System "
        "Settings, Privacy & Security, and restart the terminal"
    )


def in_mnema_app() -> bool:
    """macOS gives audio permissions to Mnema.app, or else to the terminal Mnema runs in."""
    try:
        from Foundation import NSBundle  # ty: ignore[unresolved-import]

        from ..bar.install import BUNDLE_ID

        return NSBundle.mainBundle().bundleIdentifier() == BUNDLE_ID
    except ImportError:
        return False
