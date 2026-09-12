#!/usr/bin/env python3
"""GridCrate - modern artwork manager for non-Steam Steam shortcuts.

    gridcrate                 open the app window (WebKitGTK)
    gridcrate --browser       open it in the default browser instead
    gridcrate --server-only   run just the API/HTTP server (Ctrl+C to stop)
    gridcrate --port 8317     force a port
"""

import argparse
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

from core import config, server  # noqa: E402

DEFAULT_PORT = 8317


def find_running_instance():
    runtime = config.read_runtime()
    port = runtime.get("port")
    if not port:
        return None
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as response:
            if response.status == 200 and b"gridcrate" in response.read():
                return port
    except Exception:
        return None
    return None


def open_browser(url):
    subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def open_window(url, port):
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("WebKit2", "4.1")
    from gi.repository import GLib, Gtk, WebKit2

    GLib.set_prgname("gridcrate")  # wayland app_id, matches StartupWMClass
    Gtk.init()
    window = Gtk.Window(title="GridCrate")
    window.set_default_size(1360, 880)
    window.set_icon_from_file(str(APP_DIR / "assets" / "gridcrate.png"))

    # keep WebKit's storage out of the project directory
    webkit_dir = config.CACHE_DIR / "webkit"
    webkit_dir.mkdir(parents=True, exist_ok=True)
    data_manager = WebKit2.WebsiteDataManager(
        base_cache_directory=str(webkit_dir / "cache"),
        base_data_directory=str(webkit_dir / "data"),
    )
    web_context = WebKit2.WebContext.new_with_website_data_manager(data_manager)
    view = WebKit2.WebView.new_with_context(web_context)
    view.set_size_request(900, 600)
    settings = view.get_settings()
    settings.set_property("enable-developer-extras", True)
    settings.set_property("enable-smooth-scrolling", True)
    view.load_uri(url)
    window.add(view)
    window.connect("destroy", Gtk.main_quit)
    window.show_all()
    window.present()
    if os.environ.get("GRIDCRATE_FULLSCREEN"):
        window.fullscreen()
    Gtk.main()


def log(message):
    try:
        path = config.CACHE_DIR / "gridcrate.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def main():
    parser = argparse.ArgumentParser(description="GridCrate artwork manager")
    parser.add_argument("--browser", action="store_true", help="open in the default browser")
    parser.add_argument("--server-only", action="store_true", help="headless server")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    existing = find_running_instance()
    if existing:
        url = f"http://127.0.0.1:{existing}/"
        if args.server_only:
            print(f"GridCrate is already running on {url}")
            return 0
        print(f"GridCrate is already running - opening {url}")
        try:
            open_browser(url) if args.browser else open_window(url, existing)
        except Exception:
            import traceback

            traceback.print_exc()
            log("window failed:\n" + traceback.format_exc())
            print("Could not open the app window - falling back to the browser.")
            open_browser(url)
            while True:
                time.sleep(1)
        return 0

    port = args.port or DEFAULT_PORT
    try:
        api, httpd, actual_port = server.serve(port=port, host=args.host)
    except OSError:
        api, httpd, actual_port = server.serve(port=0, host=args.host)
    url = f"http://{args.host}:{actual_port}/"
    print(f"GridCrate listening on {url}  (account {api.ctx.account['account_id'] if api.ctx.account else 'none'})")

    if args.server_only:
        signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
        while True:
            time.sleep(1)

    if args.browser:
        open_browser(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return 0

    try:
        open_window(url, actual_port)
    except Exception:
        import traceback

        traceback.print_exc()
        log("window failed:\n" + traceback.format_exc())
        print("Could not open the app window - falling back to the browser.")
        open_browser(url)
        while True:
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
