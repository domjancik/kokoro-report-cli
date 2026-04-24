#!/usr/bin/env python3
import argparse
import contextlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import wave
from array import array
from pathlib import Path
from typing import Any
from urllib import error, request


CONFIG_FILENAME = "kokoro_report.local.json"

DEFAULTS = {
    "api_base": "http://127.0.0.1:8880",
    "voice": "am_echo",
    "model": "kokoro",
    "speed": 1.0,
    "lang_code": "a",
    "volume_multiplier": 1.0,
    "timeout": 90,
    "sample_rate": 24000,
    "detach": True,
}

DEFAULT_AUDIO = {
    "preamp_db": 0.0,
    "bass_db": 0.0,
    "treble_db": 0.0,
    "highpass_hz": 0.0,
    "lowpass_hz": 0.0,
    # Peak limiter style behavior: only attenuate if above target.
    # 0 or negative means disabled.
    "normalize_peak": 0.0,
}


def state_root() -> Path:
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        return Path(base) / "kokoro-report"

    base = os.environ.get("XDG_STATE_HOME")
    if base:
        return Path(base) / "kokoro-report"
    return Path.home() / ".local" / "state" / "kokoro-report"


ROOT = state_root()
QUEUE_DIR = ROOT / "queue"
AUDIO_DIR = ROOT / "audio"
LOCK_PATH = QUEUE_DIR / "worker.lock"


def ensure_dirs() -> None:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)


@contextlib.contextmanager
def try_worker_lock():
    ensure_dirs()
    fh = open(LOCK_PATH, "a+b")
    locked = False
    try:
        if sys.platform.startswith("win"):
            import msvcrt

            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
            except OSError:
                locked = False
        else:
            import fcntl

            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                locked = False

        if not locked:
            yield None
            return

        yield fh
    finally:
        if locked:
            try:
                if sys.platform.startswith("win"):
                    import msvcrt

                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


def load_local_config(config_path: Path, *, required: bool) -> dict[str, Any]:
    if not config_path.exists():
        if required:
            raise RuntimeError(f"Config file not found: {config_path}")
        return {}

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(f"Failed reading config file {config_path}: {e}") from e

    if not isinstance(data, dict):
        raise RuntimeError(f"Config file must contain a JSON object: {config_path}")

    return data


def pick(cli_value: Any, cfg_value: Any, default: Any) -> Any:
    if cli_value is not None:
        return cli_value
    if cfg_value is not None:
        return cfg_value
    return default


def script_default_config_path() -> Path:
    return Path(__file__).resolve().parent / CONFIG_FILENAME


def resolve_say_settings(args: argparse.Namespace) -> tuple[dict[str, Any], Path, bool]:
    if args.config:
        cfg_path = Path(args.config).expanduser().resolve()
        required = True
    else:
        cfg_path = script_default_config_path()
        required = False

    cfg = load_local_config(cfg_path, required=required)
    has_config = bool(cfg)

    audio_cfg = cfg.get("audio", {})
    if audio_cfg is None:
        audio_cfg = {}
    if not isinstance(audio_cfg, dict):
        raise RuntimeError("Config field 'audio' must be an object")

    s: dict[str, Any] = {}
    s["api_base"] = str(pick(args.api_base, cfg.get("api_base"), DEFAULTS["api_base"]))
    s["voice"] = str(pick(args.voice, cfg.get("voice"), DEFAULTS["voice"]))
    s["model"] = str(pick(args.model, cfg.get("model"), DEFAULTS["model"]))
    s["lang_code"] = str(pick(args.lang_code, cfg.get("lang_code"), DEFAULTS["lang_code"]))

    s["speed"] = float(pick(args.speed, cfg.get("speed"), DEFAULTS["speed"]))
    s["volume_multiplier"] = float(
        pick(args.volume_multiplier, cfg.get("volume_multiplier"), DEFAULTS["volume_multiplier"])
    )
    s["timeout"] = int(pick(args.timeout, cfg.get("timeout"), DEFAULTS["timeout"]))
    s["sample_rate"] = int(pick(args.sample_rate, cfg.get("sample_rate"), DEFAULTS["sample_rate"]))
    s["detach"] = bool(pick(args.detach, cfg.get("detach"), DEFAULTS["detach"]))

    s["preamp_db"] = float(pick(args.preamp_db, audio_cfg.get("preamp_db"), DEFAULT_AUDIO["preamp_db"]))
    s["bass_db"] = float(pick(args.bass_db, audio_cfg.get("bass_db"), DEFAULT_AUDIO["bass_db"]))
    s["treble_db"] = float(pick(args.treble_db, audio_cfg.get("treble_db"), DEFAULT_AUDIO["treble_db"]))
    s["highpass_hz"] = float(
        pick(args.highpass_hz, audio_cfg.get("highpass_hz"), DEFAULT_AUDIO["highpass_hz"])
    )
    s["lowpass_hz"] = float(
        pick(args.lowpass_hz, audio_cfg.get("lowpass_hz"), DEFAULT_AUDIO["lowpass_hz"])
    )
    s["normalize_peak"] = float(
        pick(args.normalize_peak, audio_cfg.get("normalize_peak"), DEFAULT_AUDIO["normalize_peak"])
    )

    if s["sample_rate"] <= 0:
        raise RuntimeError("sample_rate must be > 0")
    if s["timeout"] <= 0:
        raise RuntimeError("timeout must be > 0")

    return s, cfg_path, has_config


def call_kokoro_api(
    *,
    api_base: str,
    text: str,
    voice: str,
    model: str,
    speed: float,
    lang_code: str,
    volume_multiplier: float,
    timeout_sec: int,
    pcm_out: Path,
) -> None:
    endpoint = api_base.rstrip("/") + "/v1/audio/speech"

    payload = {
        "model": model,
        "input": text,
        "voice": voice,
        "response_format": "pcm",
        "download_format": "pcm",
        "speed": speed,
        "stream": False,
        "return_download_link": False,
        "lang_code": lang_code,
        "volume_multiplier": volume_multiplier,
        "normalization_options": {
            "normalize": True,
            "unit_normalization": False,
            "url_normalization": True,
            "email_normalization": True,
            "optional_pluralization_normalization": True,
            "phone_normalization": True,
            "replace_remaining_symbols": True,
        },
    }

    req = request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=timeout_sec) as resp:
            body = resp.read()
    except error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {detail}") from e
    except error.URLError as e:
        raise RuntimeError(f"Failed connecting to API: {e}") from e

    if not body:
        raise RuntimeError("API returned empty audio body")

    pcm_out.write_bytes(body)


def pcm_bytes_to_float_samples(pcm_bytes: bytes) -> list[float]:
    a = array("h")
    a.frombytes(pcm_bytes)
    if sys.byteorder != "little":
        a.byteswap()
    return [max(-1.0, min(1.0, s / 32768.0)) for s in a]


def float_samples_to_pcm_bytes(samples: list[float]) -> bytes:
    a = array("h")
    for s in samples:
        s = max(-1.0, min(1.0, s))
        a.append(int(round(s * 32767.0)))
    if sys.byteorder != "little":
        a.byteswap()
    return a.tobytes()


def normalize_biquad(
    b0: float, b1: float, b2: float, a0: float, a1: float, a2: float
) -> tuple[float, float, float, float, float]:
    if abs(a0) < 1e-12:
        raise RuntimeError("Invalid biquad coefficients (a0=0)")
    return (b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)


def biquad_lowpass(sample_rate: int, cutoff_hz: float, q: float = 0.707) -> tuple[float, float, float, float, float]:
    w0 = 2.0 * math.pi * cutoff_hz / sample_rate
    cos_w0 = math.cos(w0)
    sin_w0 = math.sin(w0)
    alpha = sin_w0 / (2.0 * q)

    b0 = (1.0 - cos_w0) / 2.0
    b1 = 1.0 - cos_w0
    b2 = (1.0 - cos_w0) / 2.0
    a0 = 1.0 + alpha
    a1 = -2.0 * cos_w0
    a2 = 1.0 - alpha
    return normalize_biquad(b0, b1, b2, a0, a1, a2)


def biquad_highpass(sample_rate: int, cutoff_hz: float, q: float = 0.707) -> tuple[float, float, float, float, float]:
    w0 = 2.0 * math.pi * cutoff_hz / sample_rate
    cos_w0 = math.cos(w0)
    sin_w0 = math.sin(w0)
    alpha = sin_w0 / (2.0 * q)

    b0 = (1.0 + cos_w0) / 2.0
    b1 = -(1.0 + cos_w0)
    b2 = (1.0 + cos_w0) / 2.0
    a0 = 1.0 + alpha
    a1 = -2.0 * cos_w0
    a2 = 1.0 - alpha
    return normalize_biquad(b0, b1, b2, a0, a1, a2)


def biquad_low_shelf(sample_rate: int, freq_hz: float, gain_db: float, slope: float = 1.0) -> tuple[float, float, float, float, float]:
    a = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * freq_hz / sample_rate
    cos_w0 = math.cos(w0)
    sin_w0 = math.sin(w0)
    alpha = (sin_w0 / 2.0) * math.sqrt((a + 1.0 / a) * (1.0 / slope - 1.0) + 2.0)
    beta = 2.0 * math.sqrt(a) * alpha

    b0 = a * ((a + 1.0) - (a - 1.0) * cos_w0 + beta)
    b1 = 2.0 * a * ((a - 1.0) - (a + 1.0) * cos_w0)
    b2 = a * ((a + 1.0) - (a - 1.0) * cos_w0 - beta)
    a0 = (a + 1.0) + (a - 1.0) * cos_w0 + beta
    a1 = -2.0 * ((a - 1.0) + (a + 1.0) * cos_w0)
    a2 = (a + 1.0) + (a - 1.0) * cos_w0 - beta
    return normalize_biquad(b0, b1, b2, a0, a1, a2)


def biquad_high_shelf(sample_rate: int, freq_hz: float, gain_db: float, slope: float = 1.0) -> tuple[float, float, float, float, float]:
    a = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * freq_hz / sample_rate
    cos_w0 = math.cos(w0)
    sin_w0 = math.sin(w0)
    alpha = (sin_w0 / 2.0) * math.sqrt((a + 1.0 / a) * (1.0 / slope - 1.0) + 2.0)
    beta = 2.0 * math.sqrt(a) * alpha

    b0 = a * ((a + 1.0) + (a - 1.0) * cos_w0 + beta)
    b1 = -2.0 * a * ((a - 1.0) + (a + 1.0) * cos_w0)
    b2 = a * ((a + 1.0) + (a - 1.0) * cos_w0 - beta)
    a0 = (a + 1.0) - (a - 1.0) * cos_w0 + beta
    a1 = 2.0 * ((a - 1.0) - (a + 1.0) * cos_w0)
    a2 = (a + 1.0) - (a - 1.0) * cos_w0 - beta
    return normalize_biquad(b0, b1, b2, a0, a1, a2)


def apply_biquad(samples: list[float], coeffs: tuple[float, float, float, float, float]) -> list[float]:
    b0, b1, b2, a1, a2 = coeffs
    x1 = x2 = 0.0
    y1 = y2 = 0.0
    out: list[float] = []
    append = out.append

    for x0 in samples:
        y0 = (b0 * x0) + (b1 * x1) + (b2 * x2) - (a1 * y1) - (a2 * y2)
        append(y0)
        x2 = x1
        x1 = x0
        y2 = y1
        y1 = y0

    return out


def sanitize_cutoff(freq_hz: float, sample_rate: int) -> float | None:
    if freq_hz <= 0:
        return None
    nyquist = (sample_rate / 2.0) - 1.0
    if nyquist <= 20.0:
        return None
    return max(10.0, min(float(freq_hz), nyquist))


def apply_audio_processing(
    pcm_bytes: bytes,
    *,
    sample_rate: int,
    preamp_db: float,
    bass_db: float,
    treble_db: float,
    highpass_hz: float,
    lowpass_hz: float,
    normalize_peak: float,
) -> bytes:
    do_processing = any(
        [
            abs(preamp_db) > 1e-6,
            abs(bass_db) > 1e-6,
            abs(treble_db) > 1e-6,
            highpass_hz > 0,
            lowpass_hz > 0,
            normalize_peak > 0,
        ]
    )
    if not do_processing:
        return pcm_bytes

    samples = pcm_bytes_to_float_samples(pcm_bytes)

    if abs(preamp_db) > 1e-6:
        gain = 10.0 ** (preamp_db / 20.0)
        samples = [s * gain for s in samples]

    if abs(bass_db) > 1e-6:
        # Shelf is smoother than a hard cutoff for "reduce bass" use-cases.
        samples = apply_biquad(samples, biquad_low_shelf(sample_rate, freq_hz=200.0, gain_db=bass_db, slope=1.0))

    if abs(treble_db) > 1e-6:
        samples = apply_biquad(samples, biquad_high_shelf(sample_rate, freq_hz=4000.0, gain_db=treble_db, slope=1.0))

    hp = sanitize_cutoff(highpass_hz, sample_rate)
    if hp is not None:
        samples = apply_biquad(samples, biquad_highpass(sample_rate, cutoff_hz=hp, q=0.707))

    lp = sanitize_cutoff(lowpass_hz, sample_rate)
    if lp is not None:
        samples = apply_biquad(samples, biquad_lowpass(sample_rate, cutoff_hz=lp, q=0.707))

    if normalize_peak > 0:
        target = max(0.05, min(float(normalize_peak), 1.0))
        peak = max((abs(s) for s in samples), default=0.0)
        if peak > target and peak > 0.0:
            scale = target / peak
            samples = [s * scale for s in samples]

    return float_samples_to_pcm_bytes(samples)


def pcm_to_wav(pcm_path: Path, wav_path: Path, sample_rate: int) -> None:
    pcm_bytes = pcm_path.read_bytes()
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm_bytes)


def render_report_wav(text: str, settings: dict[str, Any]) -> Path:
    ensure_dirs()
    pcm_fd, pcm_path_raw = tempfile.mkstemp(prefix="kokoro_report_", suffix=".pcm")
    os.close(pcm_fd)
    pcm_path = Path(pcm_path_raw)
    wav_path = AUDIO_DIR / f"report_{time.time_ns()}_{uuid.uuid4().hex}.wav"

    try:
        call_kokoro_api(
            api_base=str(settings["api_base"]),
            text=text,
            voice=str(settings["voice"]),
            model=str(settings["model"]),
            speed=float(settings["speed"]),
            lang_code=str(settings["lang_code"]),
            volume_multiplier=float(settings["volume_multiplier"]),
            timeout_sec=int(settings["timeout"]),
            pcm_out=pcm_path,
        )

        processed_pcm = apply_audio_processing(
            pcm_bytes=pcm_path.read_bytes(),
            sample_rate=int(settings["sample_rate"]),
            preamp_db=float(settings["preamp_db"]),
            bass_db=float(settings["bass_db"]),
            treble_db=float(settings["treble_db"]),
            highpass_hz=float(settings["highpass_hz"]),
            lowpass_hz=float(settings["lowpass_hz"]),
            normalize_peak=float(settings["normalize_peak"]),
        )
        pcm_path.write_bytes(processed_pcm)
        pcm_to_wav(pcm_path=pcm_path, wav_path=wav_path, sample_rate=int(settings["sample_rate"]))
        return wav_path
    finally:
        with contextlib.suppress(Exception):
            pcm_path.unlink(missing_ok=True)


def enqueue_job(audio_path: Path, cleanup: bool = True) -> Path:
    ensure_dirs()
    job = {
        "id": uuid.uuid4().hex,
        "type": "audio",
        "audio_path": str(audio_path),
        "cleanup": cleanup,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    job_name = f"{time.time_ns():020d}_{uuid.uuid4().hex}.json"
    job_path = QUEUE_DIR / job_name
    tmp_path = QUEUE_DIR / f".{job_name}.tmp"
    tmp_path.write_text(json.dumps(job, separators=(",", ":")), encoding="utf-8")
    tmp_path.replace(job_path)
    return job_path


def enqueue_synth_job(text: str, settings: dict[str, Any]) -> Path:
    ensure_dirs()
    job = {
        "id": uuid.uuid4().hex,
        "type": "synth",
        "text": text,
        "settings": settings,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    job_name = f"{time.time_ns():020d}_{uuid.uuid4().hex}.json"
    job_path = QUEUE_DIR / job_name
    tmp_path = QUEUE_DIR / f".{job_name}.tmp"
    tmp_path.write_text(json.dumps(job, separators=(",", ":")), encoding="utf-8")
    tmp_path.replace(job_path)
    return job_path


def pop_next_job() -> dict | None:
    ensure_dirs()
    files = sorted(QUEUE_DIR.glob("*.json"), key=lambda p: p.name)
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            path.unlink(missing_ok=True)
            continue

        path.unlink(missing_ok=True)
        return data
    return None


def play_audio_blocking(audio_path: Path) -> None:
    if sys.platform.startswith("win"):
        import winsound

        winsound.PlaySound(str(audio_path), winsound.SND_FILENAME)
        return

    if sys.platform == "darwin":
        subprocess.run(
            ["afplay", str(audio_path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return

    candidates = [
        ["paplay", str(audio_path)],
        ["aplay", str(audio_path)],
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(audio_path)],
    ]

    for cmd in candidates:
        if shutil.which(cmd[0]):
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return

    raise RuntimeError("No supported audio player found (paplay/aplay/ffplay)")


def run_worker(*, once: bool = True, quiet: bool = False) -> int:
    with try_worker_lock() as lock:
        if lock is None:
            if not quiet:
                print("worker already running; exiting")
            return 0

        if not quiet:
            print("worker started")

        while True:
            job = pop_next_job()
            if job is None:
                if once:
                    break
                time.sleep(0.25)
                continue

            job_type = str(job.get("type", "audio"))

            if job_type == "synth":
                text = str(job.get("text", ""))
                settings = job.get("settings", {})
                if not isinstance(settings, dict):
                    settings = {}

                wav_path: Path | None = None
                try:
                    if not text.strip():
                        continue
                    wav_path = render_report_wav(text=text, settings=settings)
                    play_audio_blocking(wav_path)
                except Exception as e:
                    if not quiet:
                        print(f"synth/playback failed: {e}")
                finally:
                    if wav_path is not None:
                        with contextlib.suppress(Exception):
                            wav_path.unlink(missing_ok=True)
                continue

            audio_path = Path(str(job.get("audio_path", "")))
            cleanup = bool(job.get("cleanup", True))

            try:
                if audio_path.exists():
                    play_audio_blocking(audio_path)
                else:
                    if not quiet:
                        print(f"missing audio file: {audio_path}")
            except Exception as e:
                if not quiet:
                    print(f"playback failed for {audio_path}: {e}")
            finally:
                if cleanup:
                    with contextlib.suppress(Exception):
                        audio_path.unlink(missing_ok=True)

        if not quiet:
            print("worker finished")

    return 0


def spawn_worker_detached(script_path: Path) -> None:
    args = [sys.executable, str(script_path), "worker", "--once", "--quiet"]

    if sys.platform.startswith("win"):
        # Use Task Scheduler to fully detach from parent process/job object.
        task_name = "KokoroReportCliWorker"
        start_time = time.strftime("%H:%M", time.localtime(time.time() + 60))
        task_cmd = f'"{sys.executable}" "{script_path}" worker --once --quiet'
        try:
            subprocess.run(
                [
                    "schtasks",
                    "/Create",
                    "/TN",
                    task_name,
                    "/SC",
                    "ONCE",
                    "/ST",
                    start_time,
                    "/TR",
                    task_cmd,
                    "/F",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["schtasks", "/Run", "/TN", task_name],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except Exception:
            # Fallback: best-effort detached child process.
            creationflags = 0
            creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=creationflags,
            )
            return

    subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )


def cmd_say(args: argparse.Namespace) -> int:
    ensure_dirs()

    settings, cfg_path, has_config = resolve_say_settings(args)
    if has_config and not args.quiet:
        print(f"using config defaults: {cfg_path}")
    enqueue_synth_job(text=args.text, settings=settings)
    if settings["detach"]:
        spawn_worker_detached(Path(__file__).resolve())
        print("queued synthesis job")
        return 0
    return run_worker(once=True, quiet=False)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Cross-platform Kokoro report speaker")
    sub = p.add_subparsers(dest="command", required=True)

    say = sub.add_parser("say", help="Generate speech, queue it, and trigger worker")
    say.add_argument("text", help="One-sentence report text to speak")

    # Runtime controls (CLI > local config > built-in defaults)
    say.add_argument("--config", default=None, help=f"Path to config file (default auto: ./{CONFIG_FILENAME})")
    say.add_argument("--api-base", default=None)
    say.add_argument("--voice", default=None)
    say.add_argument("--model", default=None)
    say.add_argument("--speed", type=float, default=None)
    say.add_argument("--lang-code", default=None)
    say.add_argument("--volume-multiplier", type=float, default=None)
    say.add_argument("--timeout", type=int, default=None)
    say.add_argument("--sample-rate", type=int, default=None)
    say.add_argument("--detach", action=argparse.BooleanOptionalAction, default=None)

    # Basic audio shaping
    say.add_argument("--preamp-db", type=float, default=None, help="Pre-gain in dB")
    say.add_argument("--bass-db", type=float, default=None, help="Low-shelf gain in dB (~200Hz)")
    say.add_argument("--treble-db", type=float, default=None, help="High-shelf gain in dB (~4kHz)")
    say.add_argument("--highpass-hz", type=float, default=None, help="High-pass cutoff in Hz")
    say.add_argument("--lowpass-hz", type=float, default=None, help="Low-pass cutoff in Hz")
    say.add_argument(
        "--normalize-peak",
        type=float,
        default=None,
        help="Peak target 0..1 limiter (attenuates only; <=0 disables)",
    )

    say.add_argument("--quiet", action=argparse.BooleanOptionalAction, default=False)
    say.set_defaults(func=cmd_say)

    worker = sub.add_parser("worker", help="Drain queued report jobs")
    worker.add_argument("--once", action=argparse.BooleanOptionalAction, default=True)
    worker.add_argument("--quiet", action=argparse.BooleanOptionalAction, default=False)
    worker.set_defaults(func=lambda a: run_worker(once=a.once, quiet=a.quiet))

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
