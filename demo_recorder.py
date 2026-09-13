"""Ultimate auto-demo recorder for Nexus-Greed.

Boots the full stack, then records exactly --seconds of live footage:

    1. Streamer     : python -m nexus_greed serve   (FastAPI/uvicorn)
                      falls back to `lite` (pure websockets) if FastAPI's
                      compiled deps can't load on this machine
    2. Dashboard    : React dev server (npm run dev) when Node exists,
                      else the built-in zero-dependency /demo page
    3. Chaos        : tests/chaos_stress_test.py --agents N
    4. Recorder     : headless Chrome/Edge driven over raw CDP
                      (Page.startScreencast) -> JPEG frames piped into
                      the imageio-ffmpeg binary -> ultimate_demo.mp4

All child processes are terminated and ports released on exit.

Usage:
    python demo_recorder.py                      # 90s, 10k agents
    python demo_recorder.py --seconds 30 --agents 500
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable

WIDTH, HEIGHT = 1600, 900


# --------------------------------------------------------------------------- #
# Process / network helpers
# --------------------------------------------------------------------------- #
def wait_http(url: str, timeout: float = 30.0) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - still booting
            time.sleep(0.3)
    return False


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        exe = shutil.which("ffmpeg")
        if exe:
            return exe
        raise RuntimeError("no ffmpeg binary (install imageio-ffmpeg or ffmpeg)")


def find_browser() -> str:
    """Locate a CDP-capable Chromium binary: Chrome, Edge, or a Playwright
    browser already downloaded under %LOCALAPPDATA%\\ms-playwright."""
    candidates = [os.environ.get("NEXUS_BROWSER", "")]
    candidates += [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    pw = os.path.expandvars(r"%LOCALAPPDATA%\ms-playwright")
    candidates += glob.glob(os.path.join(pw, "chromium_headless_shell-*",
                                         "chrome-win", "headless_shell.exe"))
    candidates += glob.glob(os.path.join(pw, "chromium-*",
                                         "chrome-win", "chrome.exe"))
    for c in candidates:
        if c and Path(c).exists():
            return c
    raise RuntimeError("no Chromium-family browser found (set NEXUS_BROWSER)")


# --------------------------------------------------------------------------- #
# Minimal CDP client (pure asyncio + websockets — no playwright needed)
# --------------------------------------------------------------------------- #
class CDP:
    def __init__(self, ws: object) -> None:
        self.ws = ws
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self.frames: asyncio.Queue = asyncio.Queue()

    async def recv_loop(self) -> None:
        async for raw in self.ws:
            try:
                m = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if "id" in m and m["id"] in self._pending:
                self._pending.pop(m["id"]).set_result(m)
            elif m.get("method") == "Page.screencastFrame":
                self.frames.put_nowait(m.get("params", {}))

    async def call(self, method: str, params: dict | None = None,
                   session: str | None = None) -> dict:
        self._id += 1
        mid = self._id
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        msg = {"id": mid, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        await self.ws.send(json.dumps(msg))
        return await asyncio.wait_for(fut, timeout=15)


async def record_dashboard(browser: str, page_url: str, seconds: float,
                           out_mp4: Path) -> int:
    """Screencast `page_url` for `seconds` and encode straight to mp4."""
    try:
        from websockets.asyncio.client import connect
    except ImportError:
        from websockets import connect  # type: ignore

    debug_port = free_port()
    prof = Path(tempfile.mkdtemp(prefix="nexus_chrome_"))
    chrome = subprocess.Popen([
        browser, "--headless=new", f"--remote-debugging-port={debug_port}",
        "--remote-allow-origins=*", f"--user-data-dir={prof}",
        "--no-first-run", "--no-default-browser-check",
        f"--window-size={WIDTH},{HEIGHT}", "--hide-scrollbars",
        "--mute-audio",
        # Keep compositor frames flowing: headless pages report
        # visibilityState=hidden, which suppresses BeginFrames and starves
        # Page.startScreencast. These flags + focus emulation defeat that.
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-features=CalculateNativeWinOcclusion",
        "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    frames = {"n": 0}
    try:
        if not wait_http(f"http://127.0.0.1:{debug_port}/json/version", 20):
            raise RuntimeError("browser CDP endpoint did not come up")
        with urllib.request.urlopen(
                f"http://127.0.0.1:{debug_port}/json/version", timeout=5) as r:
            browser_ws = json.loads(r.read())["webSocketDebuggerUrl"]

        ff = subprocess.Popen([
            ffmpeg_exe(), "-y",
            # Wallclock timestamps: frames arrive at whatever rate the
            # compositor produces (~10-15fps under load). Tagging them with
            # real arrival time makes the output duration match wall time
            # (90s recording -> ~90s video); output CFR 30 pads duplicates.
            "-use_wallclock_as_timestamps", "1",
            "-f", "image2pipe", "-i", "-",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-r", "60",
            str(out_mp4),
        ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        assert ff.stdin is not None

        async with connect(browser_ws, max_size=None) as ws:
            cdp = CDP(ws)
            pump = asyncio.create_task(cdp.recv_loop())
            try:
                r = await cdp.call("Target.createTarget",
                                   {"url": "about:blank"})
                tid = r["result"]["targetId"]
                r = await cdp.call("Target.attachToTarget",
                                   {"targetId": tid, "flatten": True})
                sid = r["result"]["sessionId"]
                await cdp.call("Page.enable", session=sid)
                # Fake focus so the page isn't treated as occluded/hidden.
                await cdp.call("Emulation.setFocusEmulationEnabled",
                               {"enabled": True}, session=sid)
                await cdp.call("Emulation.setDeviceMetricsOverride",
                               {"width": WIDTH, "height": HEIGHT,
                                "deviceScaleFactor": 1, "mobile": False},
                               session=sid)
                await cdp.call("Page.navigate", {"url": page_url}, session=sid)
                await asyncio.sleep(2)  # let the dashboard connect + paint
                await cdp.call("Page.startScreencast",
                               {"format": "jpeg", "quality": 92,
                                "everyNthFrame": 1}, session=sid)

                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    try:
                        params = await asyncio.wait_for(
                            cdp.frames.get(), timeout=deadline - time.monotonic())
                    except asyncio.TimeoutError:
                        break
                    if not params:
                        continue
                    jpeg = base64.b64decode(params["data"])
                    await asyncio.to_thread(ff.stdin.write, jpeg)
                    frames["n"] += 1
                    await cdp.call("Page.screencastFrameAck",
                                   {"sessionId": params["sessionId"]},
                                   session=sid)
                await cdp.call("Page.stopScreencast", session=sid)
            finally:
                pump.cancel()

        ff.stdin.close()
        await asyncio.to_thread(ff.wait, 30)
        return frames["n"]
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=8)
        except Exception:  # noqa: BLE001
            chrome.kill()
        shutil.rmtree(prof, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="Nexus-Greed ultimate demo recorder")
    ap.add_argument("--seconds", type=float, default=90.0)
    ap.add_argument("--agents", type=int, default=10_000)
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--tick-seconds", type=float, default=0.06)
    ap.add_argument("--out", default="ultimate_demo.mp4")
    args = ap.parse_args()

    procs: list[subprocess.Popen] = []
    out_mp4 = ROOT / args.out

    try:
        # --- 1. Streamer: FastAPI first, pure-websockets fallback -----------
        api = f"http://127.0.0.1:{args.port}/api/status"
        serve_cmd = [PY, "-m", "nexus_greed", "serve", "--seed", str(args.seed),
                     "--tick-seconds", str(args.tick_seconds),
                     "--port", str(args.port)]
        print(f"[demo] streamer: {' '.join(serve_cmd)}", flush=True)
        p = subprocess.Popen(serve_cmd, cwd=ROOT)
        procs.append(p)
        if not wait_http(api, 15):
            print("[demo] FastAPI path unavailable — falling back to `lite` "
                  "server (same wire protocol, pure websockets)", flush=True)
            p.terminate()
            procs.remove(p)
            lite_cmd = [PY, "-m", "nexus_greed", "lite", "--seed", str(args.seed),
                        "--tick-seconds", str(args.tick_seconds),
                        "--port", str(args.port)]
            print(f"[demo] lite: {' '.join(lite_cmd)}", flush=True)
            p = subprocess.Popen(lite_cmd, cwd=ROOT)
            procs.append(p)
            if not wait_http(api, 20):
                raise RuntimeError("no streamer came up on /api/status")
        print("[demo] streamer live", flush=True)

        # --- 2. Dashboard target ------------------------------------------
        npm = shutil.which("npm") or shutil.which("npm.cmd")
        dash_url = f"http://127.0.0.1:{args.port}/demo"
        if npm:
            fe = ROOT / "frontend"
            if (fe / "node_modules").exists() or subprocess.call(
                    [npm, "install"], cwd=fe) == 0:
                procs.append(subprocess.Popen([npm, "run", "dev"], cwd=fe))
                # Poll until Vite serves a real HTTP 200 — never open the
                # browser against a dead port (ERR_CONNECTION_REFUSED frame).
                if wait_http("http://127.0.0.1:5173", 30):
                    dash_url = "http://127.0.0.1:5173"
                print(f"[demo] dashboard: {dash_url}", flush=True)
        if dash_url.endswith("/demo"):
            print("[demo] npm unavailable — using built-in /demo dashboard",
                  flush=True)
        # Hard gate: the browser is not launched until the dashboard URL
        # returns HTTP 200.
        if not wait_http(dash_url, 30):
            raise RuntimeError(f"dashboard never became ready: {dash_url}")
        print(f"[demo] dashboard live at {dash_url}", flush=True)

        # --- 3. Chaos harness ----------------------------------------------
        chaos_cmd = [PY, str(ROOT / "tests" / "chaos_stress_test.py"),
                     "--host", "127.0.0.1", "--port", str(args.port),
                     "--agents", str(args.agents)]
        print(f"[demo] chaos: {' '.join(chaos_cmd)}", flush=True)
        procs.append(subprocess.Popen(chaos_cmd, cwd=ROOT))
        print("[demo] warming up market + chaos connections…", flush=True)
        time.sleep(20)

        # --- 4. Record ------------------------------------------------------
        browser = find_browser()
        print(f"[demo] browser: {browser}", flush=True)
        print(f"[demo] recording {args.seconds:.0f}s of {dash_url}", flush=True)
        n = asyncio.run(record_dashboard(browser, dash_url, args.seconds, out_mp4))
        if out_mp4.exists() and out_mp4.stat().st_size > 0:
            print(f"[demo] saved {out_mp4} "
                  f"({out_mp4.stat().st_size / 1e6:.1f} MB, {n} frames)",
                  flush=True)
        else:
            raise RuntimeError("mp4 was not produced")
        return 0

    finally:
        for proc in procs:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        for proc in procs:
            try:
                proc.wait(timeout=8)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        print("[demo] all processes terminated, ports released", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
