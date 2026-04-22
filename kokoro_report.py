#!/usr/bin/env python3
import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import wave
from pathlib import Path
from urllib import error, request


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


def pcm_to_wav(pcm_path: Path, wav_path: Path, sample_rate: int) -> None:
    pcm_bytes = pcm_path.read_bytes()
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm_bytes)


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


def enqueue_job(audio_path: Path, cleanup: bool = True) -> Path:
    ensure_dirs()
    job = {
        "id": uuid.uuid4().hex,
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

    pcm_fd, pcm_path_raw = tempfile.mkstemp(prefix="kokoro_report_", suffix=".pcm")
    os.close(pcm_fd)
    pcm_path = Path(pcm_path_raw)

    wav_path = AUDIO_DIR / f"report_{time.time_ns()}_{uuid.uuid4().hex}.wav"

    try:
        call_kokoro_api(
            api_base=args.api_base,
            text=args.text,
            voice=args.voice,
            model=args.model,
            speed=args.speed,
            lang_code=args.lang_code,
            volume_multiplier=args.volume_multiplier,
            timeout_sec=args.timeout,
            pcm_out=pcm_path,
        )

        pcm_to_wav(pcm_path=pcm_path, wav_path=wav_path, sample_rate=args.sample_rate)
        enqueue_job(audio_path=wav_path, cleanup=True)

        if args.detach:
            spawn_worker_detached(Path(__file__).resolve())
            print(f"queued: {wav_path}")
            return 0

        return run_worker(once=True, quiet=False)
    finally:
        with contextlib.suppress(Exception):
            pcm_path.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Cross-platform Kokoro report speaker")
    sub = p.add_subparsers(dest="command", required=True)

    say = sub.add_parser("say", help="Generate speech, queue it, and trigger worker")
    say.add_argument("text", help="One-sentence report text to speak")
    say.add_argument("--api-base", default="http://127.0.0.1:8880")
    say.add_argument("--voice", default="am_echo")
    say.add_argument("--model", default="kokoro")
    say.add_argument("--speed", type=float, default=1.0)
    say.add_argument("--lang-code", default="a")
    say.add_argument("--volume-multiplier", type=float, default=1.0)
    say.add_argument("--timeout", type=int, default=90)
    say.add_argument("--sample-rate", type=int, default=24000)
    say.add_argument("--detach", action=argparse.BooleanOptionalAction, default=True)
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
