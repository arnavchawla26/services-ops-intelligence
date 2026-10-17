"""Start the Streamlit dashboard on a free port, screenshot each page headlessly
with Playwright, stitch them into reports/dashboard.png, stop the server.

Uses the machine's Google Chrome through Playwright's "chrome" channel when it
exists, else Playwright's own Chromium (`uv run playwright install chromium`).
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGES = ["Fleet risk", "Dispatch budget", "Parts", "Repeat dispatch"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url: str, timeout: float = 60.0) -> None:
    import urllib.request
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            urllib.request.urlopen(url, timeout=2).read()
            return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"dashboard did not come up at {url}")


def main() -> int:
    from playwright.sync_api import sync_playwright
    from PIL import Image

    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", str(ROOT / "app" / "dashboard.py"), "--server.headless", "true",
         "--server.port", str(port), "--browser.gatherUsageStats", "false"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    out_dir = ROOT / "reports"
    shots: list[Path] = []
    try:
        url = f"http://127.0.0.1:{port}"
        wait_http(url)
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(channel="chrome", headless=True)
            except Exception:
                browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1400, "height": 900}, device_scale_factor=1)
            page.goto(url, wait_until="networkidle")
            page.wait_for_timeout(2500)
            for i, name in enumerate(PAGES):
                page.get_by_text(name, exact=True).first.click()
                page.wait_for_timeout(2500)
                p = out_dir / f"_page_{i}.png"
                page.screenshot(path=str(p), full_page=False)
                shots.append(p)
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    imgs = [Image.open(p) for p in shots]
    w, h = imgs[0].size
    grid = Image.new("RGB", (w * 2, h * 2), "white")
    for i, im in enumerate(imgs):
        grid.paste(im, ((i % 2) * w, (i // 2) * h))
    grid = grid.resize((w, h), Image.LANCZOS)
    grid.save(out_dir / "dashboard.png", optimize=True)
    for p in shots:
        p.unlink()
    print(f"wrote {out_dir / 'dashboard.png'} ({(out_dir / 'dashboard.png').stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
