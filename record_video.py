"""Download supported videos, or open an embed player and record its stream.

Install:
    python -m pip install -U "yt-dlp[default]" imageio-ffmpeg playwright
    python -m playwright install chromium

Usage:
    python record_video.py "https://www.youtube.com/watch?v=VIDEO_ID"
    python record_video.py --embed "https://example.com/embed/VIDEO_ID"
    python record_video.py --embed --seconds 600 "https://example.com/embed/VIDEO_ID"

Embed mode opens Chromium. Click Play, wait until the intended video is playing,
then press Enter in the terminal. The script detects an HLS/DASH manifest or a
direct media request and records the underlying video and audio using ffmpeg.
It is stream recording, not screen capture. It does not support every player,
DRM, WebRTC, or bypassing access restrictions. Record only content you may save.

Files go to saved_matches/ beside this script. Embed recording first writes MKV
so interruption is recoverable, then remuxes to MP4 without re-encoding if the
codecs permit it. For live streams, recording starts near the live edge; for
on-demand streams it normally starts at the beginning, not the browser's current
playback position. --seconds limits recorded media time, not wall-clock time.
Without --seconds, recording continues until the stream ends or Ctrl+C.
Python 3.10+ is required. Node or Deno is recommended for YouTube downloads.
"""

import argparse
from concurrent.futures import Future
from datetime import datetime
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

OUT_DIR = Path(__file__).resolve().parent / "saved_matches"


def find_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg
    except ImportError:
        raise RuntimeError("ffmpeg not found. Run: python -m pip install imageio-ffmpeg")
    return imageio_ffmpeg.get_ffmpeg_exe()


def build_options() -> dict:
    return {
        "outtmpl": str(OUT_DIR / "%(upload_date)s - %(title)s [%(id)s].%(ext)s"),
        "windowsfilenames": True,
        "noplaylist": True,
        "format": "bv*[vcodec^=avc1]+ba[ext=m4a]/bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b",
        "merge_output_format": "mp4",
        "ffmpeg_location": find_ffmpeg(),
        "js_runtimes": {"deno": {}, "node": {}},
    }


def download(urls: list[str]) -> None:
    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.postprocessor import PostProcessor
        from yt_dlp.utils import DownloadError
    except ImportError:
        raise RuntimeError('Run: python -m pip install -U "yt-dlp[default]"')

    # after_move reports the actual final filename after merging/postprocessing.
    class ReportSaved(PostProcessor):
        def run(self, info):
            print(f"Saved: {info['filepath']}")
            return [], info

    try:
        with YoutubeDL(build_options()) as ydl:
            ydl.add_post_processor(ReportSaved(), when="after_move")
            result = ydl.download(urls)
            if result:
                raise RuntimeError("One or more downloads failed.")
    except DownloadError as exc:
        raise RuntimeError(str(exc)) from exc


def safe_title(title: str) -> str:
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")
    return title[:100].rstrip(" .") or "recording"


def stream_kind(url: str, content_type: str, resource_type: str) -> str | None:
    path = urlsplit(url).path.lower()
    content_type = content_type.lower().split(";", 1)[0].strip()
    if path.endswith(".m3u8") or "mpegurl" in content_type:
        return "HLS"
    if path.endswith(".mpd") or content_type == "application/dash+xml":
        return "DASH"
    # Fetch/XHR .mp4 responses may be tiny DASH/HLS fragments, not complete media.
    if resource_type == "media" and (
        path.endswith((".mp4", ".webm", ".mov"))
        or content_type in {"video/mp4", "video/webm", "video/quicktime"}
    ):
        return "file"
    return None


def terminal_input(page, prompt: str) -> str:
    """Keep Playwright events flowing while the user types in the terminal."""
    answer = Future()

    def read():
        try:
            answer.set_result(input(prompt))
        except Exception as exc:
            answer.set_exception(exc)

    threading.Thread(target=read, daemon=True).start()
    while not answer.done():
        if page.is_closed():
            raise RuntimeError("Browser closed before recording started.")
        page.wait_for_timeout(100)
    return answer.result()


def cookie_option(cookies: list[dict], stream_url: str) -> str:
    # Use ffmpeg's domain/path-aware cookie option, not a global Cookie header.
    parts = urlsplit(stream_url)
    lines = []
    for cookie in cookies:
        fields = [str(cookie[key]) for key in ("name", "value", "path", "domain")]
        if any("\r" in value or "\n" in value for value in fields):
            continue
        name, value, path, domain = fields
        domain = domain.lstrip(".")
        # ffmpeg compares cookie domains against HTTP Host, including a custom
        # port. Scope matching cookies to that endpoint when one is present.
        if parts.port and parts.port != (443 if parts.scheme == "https" else 80):
            host = parts.hostname or ""
            if host == domain or host.endswith("." + domain):
                domain = parts.netloc
        line = f"{name}={value}; path={path}; domain={domain};"
        if cookie.get("secure"):
            line += " secure;"
        lines.append(line)
    return "\n".join(lines)


def record_command(ffmpeg: str, stream: dict, cookies: list[dict],
                   output: Path, seconds: float | None) -> list[str]:
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-stats", "-n",
           "-rw_timeout", "30000000"]
    # Preserve playback headers needed by many embeds. Cookies are scoped below.
    headers = stream["headers"]
    for key, option in (("user-agent", "-user_agent"), ("referer", "-referer")):
        value = headers.get(key)
        if value and "\r" not in value and "\n" not in value:
            cmd += [option, value]
    origin = headers.get("origin")
    if origin and "\r" not in origin and "\n" not in origin:
        cmd += ["-headers", f"Origin: {origin}\r\n"]
    cookies_text = cookie_option(cookies, stream["url"])
    if cookies_text:
        cmd += ["-cookies", cookies_text]
    cmd += ["-i", stream["url"]]
    if seconds is not None:
        cmd += ["-t", str(seconds)]
    # Automatic stream selection picks video and audio, including HLS variants.
    cmd += ["-c", "copy", "-sn", "-dn", str(output)]
    return cmd


def stop_recording(process) -> None:
    try:
        process.stdin.write(b"q\n")
        process.stdin.flush()
    except (BrokenPipeError, OSError):
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def record_embed(url: str, seconds: float | None, wait: float) -> bool:
    try:
        from playwright.sync_api import sync_playwright, Error, TimeoutError
    except ImportError:
        raise RuntimeError("Run: python -m pip install playwright\n"
                           "Then: python -m playwright install chromium")

    ffmpeg = find_ffmpeg()
    streams = {}
    stopped = False
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=False)
        except Error as exc:
            raise RuntimeError("Cannot open Chromium. Run: python -m playwright install chromium\n"
                               "Embed mode requires a desktop display.") from exc
        try:
            context = browser.new_context()
            # Detect EME/DRM use and stop; no license or decryption handling.
            context.add_init_script("""
                window.__record_video_encrypted = false;
                document.addEventListener('encrypted', () => {
                    window.__record_video_encrypted = true;
                }, true);
            """)
            page = context.new_page()

            def observe(response):
                if not 200 <= response.status < 300:
                    return
                kind = stream_kind(response.url, response.headers.get("content-type", ""),
                                   response.request.resource_type)
                if not kind or urlsplit(response.url).scheme not in {"http", "https"}:
                    return
                streams[response.url] = {
                    "url": response.url, "kind": kind,
                    "headers": response.request.all_headers(),
                }

            context.on("response", observe)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except TimeoutError:
                print("Page load timed out; try playing the video in the open browser.")
            terminal_input(page, "Click Play in the browser. When the intended video is playing, "
                           "press Enter here: ")
            deadline = time.monotonic() + wait
            while not streams and time.monotonic() < deadline:
                page.wait_for_timeout(200)
            if not streams:
                raise RuntimeError("No HLS/DASH or direct media request detected. "
                                   "This player may require a supported site extractor or screen recording.")
            for frame in page.frames:
                if frame.evaluate("Boolean(window.__record_video_encrypted)"):
                    raise RuntimeError("This player uses encrypted-media playback; DRM is not supported.")

            candidates = list(streams.values())
            if len(candidates) > 1:
                print("Detected streams (some may be ads or quality variants):")
                for index, item in enumerate(candidates, 1):
                    parts = urlsplit(item["url"])
                    print(f"  {index}. {item['kind']}: {parts.netloc}{parts.path}")
                choice = terminal_input(page, "Choose the intended stream number: ")
                try:
                    selected = int(choice) - 1
                except ValueError:
                    raise RuntimeError("Enter a valid stream number.")
                if not 0 <= selected < len(candidates):
                    raise RuntimeError("Enter a valid stream number.")
                stream = candidates[selected]
            else:
                stream = candidates[0]

            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            output = OUT_DIR / f"{stamp} - {safe_title(page.title())}.mkv"
            # Keep cookies in memory and let ffmpeg match their domain/path.
            cookies = context.cookies(stream["url"])
            command = record_command(ffmpeg, stream, cookies, output, seconds)
            print("Recording video and audio. Press Ctrl+C to stop and save.")
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
            process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                       start_new_session=sys.platform != "win32",
                                       creationflags=creationflags)
            try:
                while process.poll() is None:
                    try:
                        page.wait_for_timeout(200)
                    except Error:
                        time.sleep(0.2)
            except KeyboardInterrupt:
                stopped = True
                print("\nStopping and saving...")
            finally:
                if process.poll() is None:
                    stop_recording(process)
                process.stdin.close()
            if not output.exists() or output.stat().st_size == 0:
                raise RuntimeError("No recording was saved. The stream may reject external access.")
            if process.returncode != 0:
                print(f"ffmpeg exited with code {process.returncode}; a partial recording may exist.")
                print(f"Check: {output}")
                raise RuntimeError("Recording did not finish successfully.")

            mp4 = output.with_suffix(".mp4")
            result = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-n",
                                     "-i", str(output), "-c", "copy", "-movflags", "+faststart",
                                     str(mp4)], stdin=subprocess.DEVNULL)
            if result.returncode == 0:
                output.unlink()
                output = mp4
            else:
                mp4.unlink(missing_ok=True)
                print("MP4 remux failed; keeping the original MKV recording.")
            print(f"Saved: {output}")
        except Error as exc:
            raise RuntimeError(f"Browser/player error: {exc}") from exc
        finally:
            browser.close()
    return stopped


def positive_seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("Must be a positive number of seconds.")
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("Must be a positive number of seconds.")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("urls", nargs="*", help="Video URLs, or embed page URLs with --embed")
    parser.add_argument("--embed", action="store_true", help="Open embed pages and record their streams")
    parser.add_argument("--seconds", type=positive_seconds, help="Stop after this much media time (--embed)")
    parser.add_argument("--wait", type=positive_seconds, default=30,
                        help="Stream detection timeout after pressing Enter (default: 30 seconds)")
    args = parser.parse_args()
    if args.seconds is not None and not args.embed:
        parser.error("--seconds requires --embed")
    urls = args.urls or [input("Embed link: " if args.embed else "Video link: ").strip()]
    if any(urlsplit(url).scheme not in {"http", "https"} or not urlsplit(url).netloc for url in urls):
        parser.error("Provide an http:// or https:// URL.")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if args.embed:
        for url in urls:
            if record_embed(url, args.seconds, args.wait):
                break  # Ctrl+C stops the batch too.
    else:
        download(urls)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except (RuntimeError, EOFError) as exc:
        sys.exit(str(exc))
