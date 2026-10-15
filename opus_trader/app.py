"""App shell: engine + local server on a background thread, UI in a native window (or the browser)."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import CONFIG_PATH, ROOT, ConfigManager
from .engine import Engine
from .clock import Clock
from .news import NewsCalendar, YamlEventSource
from .store import Store

log = logging.getLogger("opus_trader")
DATA = ROOT / "data"


def setup_logging(log_dir: Path, verbose: bool = False) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    fh = RotatingFileHandler(log_dir / "opus_trader.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    for noisy in ("uvicorn.access", "httpx", "httpx2", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ROOT / ".env")


class InstanceLock:
    """One Opus Trader per data folder, so two bots never manage the same account.

    An OS file lock: released automatically if the process dies, so a crash
    never leaves a stale lock behind.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self.fh = fh
        return True

    def note(self, text: str) -> None:
        """Record where this instance can be reached (read by a second start)."""
        info = self.path.with_suffix(".info")
        info.write_text(text, encoding="utf-8")

    def holder(self) -> str:
        try:
            return self.path.with_suffix(".info").read_text(encoding="utf-8").strip()
        except OSError:
            return ""


def already_running(lock: InstanceLock) -> int:
    where = lock.holder()
    print("\nOpus Trader is already running" + (f" ({where})" if where else "") + ".\n"
          "This second start was stopped so two bots never trade the same account.\n"
          "Switch to the open Opus Trader window, or close it first.\n", flush=True)
    return 2


def free_port(preferred: int) -> int:
    for port in [preferred] + list(range(preferred + 1, preferred + 20)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("No free local port for the UI server")


def build_engine(args) -> tuple[Engine, object | None]:
    """Real terminal + Opus, or the --sim preview. Returns (engine, sim_env)."""
    if args.sim:
        from .sim import SimEnv

        env = SimEnv(ROOT, fresh=not args.keep_sim)
        engine = Engine(config=env.config, gateway=env.gateway, llm=env.llm, store=env.store, news=env.news, clock=env.clock, root=env.dir, sim=True)
        return engine, env

    try:
        import MetaTrader5 as mt5  # Windows only
    except ImportError:
        sys.exit("The MetaTrader5 package isn't installed (it only exists for Windows). Use --sim to preview the app.")
    from .llm import OpusClient
    from .mt5_gateway import MT5Gateway

    store = Store(DATA / "opus_trader.sqlite")
    config = ConfigManager(CONFIG_PATH, store)
    cfg = config.cfg
    gateway = MT5Gateway(mt5, cfg.app.symbol, cfg.app.magic, os.environ.get("MT5_TERMINAL_PATH"))
    news = NewsCalendar(YamlEventSource(ROOT / cfg.news.events_file), cfg.news.currencies)
    llm = OpusClient(os.environ.get("ANTHROPIC_API_KEY") or None)
    return Engine(config=config, gateway=gateway, llm=llm, store=store, news=news, clock=Clock(), root=ROOT), None


class ServerThread(threading.Thread):
    """Runs the engine and uvicorn on one asyncio loop."""

    def __init__(self, engine: Engine, port: int, token: str, sim_env=None, seed_days: float = 0.0, on_exit=None):
        super().__init__(name="engine", daemon=True)
        self.engine = engine
        self.port = port
        self.token = token
        self.sim_env = sim_env
        self.seed_days = seed_days
        self.on_exit = on_exit
        self.loop: asyncio.AbstractEventLoop | None = None
        self.server = None
        self.ready = threading.Event()
        self.error: BaseException | None = None

    def run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main())
        except BaseException as exc:  # noqa: BLE001
            self.error = exc
            log.exception("Engine thread stopped")
            self.ready.set()

    async def _main(self) -> None:
        import uvicorn

        from .api import create_app

        app = create_app(self.engine, self.token, on_exit=self.request_exit, port=self.port)
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="off", timeout_graceful_shutdown=3)
        self.server = uvicorn.Server(config)
        if self.sim_env is not None:
            from .sim import seed_history

            self.engine.llm.latency_s = (0.0, 0.0)
            if self.seed_days > 0:
                log.info("Seeding %.1f days of simulated history...", self.seed_days)
                n = await seed_history(self.engine, self.sim_env, self.seed_days)
                log.info("Seeded %d bars", n)
            self.engine.llm.latency_s = (3.0, 9.0)
        await self.engine.start()
        serve = asyncio.create_task(self.server.serve())
        while not self.server.started and not serve.done():
            await asyncio.sleep(0.05)
        self.ready.set()
        await serve
        await self.engine.shutdown()

    def request_exit(self) -> None:
        """Called from the API (Stop and exit) on the engine loop."""
        log.info("Exit requested from the app")
        if self.server:
            self.server.should_exit = True
        if self.on_exit:
            self.on_exit()

    def stop(self) -> None:
        if self.loop and self.server:
            self.loop.call_soon_threadsafe(setattr, self.server, "should_exit", True)


def run_app(args) -> int:
    load_env()
    lock = InstanceLock((DATA / "sim" if args.sim else DATA) / "opus_trader.lock")
    if not lock.acquire():
        return already_running(lock)
    setup_logging((DATA / "sim" / "logs") if args.sim else (DATA / "logs"), args.verbose)
    engine, sim_env = build_engine(args)
    port = free_port(args.port or engine.cfg.app.server_port)
    lock.note(f"http://127.0.0.1:{port}/")
    token = secrets.token_urlsafe(24)
    window_holder: dict = {}

    def close_window() -> None:
        w = window_holder.get("window")
        if w is not None:
            window_holder["allow_close"] = True
            threading.Thread(target=w.destroy, daemon=True).start()

    thread = ServerThread(engine, port, token, sim_env, args.seed_days if args.sim else 0.0, on_exit=close_window)
    thread.start()
    thread.ready.wait()
    if thread.error:
        log.error("Could not start: %s", thread.error)
        return 1
    url = f"http://127.0.0.1:{port}/"
    log.info("Opus Trader is running at %s", url)

    if args.no_window:
        if args.open_browser:
            webbrowser.open(url)
        return wait_for_ctrl_c(thread)

    try:
        import webview
    except ImportError:
        log.warning("pywebview isn't available; opening the UI in your browser instead")
        webbrowser.open(url)
        return wait_for_ctrl_c(thread)

    window = webview.create_window("Opus Trader", url, width=1360, height=860, min_size=(1024, 700), background_color="#0E0F11", text_select=True)
    window_holder["window"] = window

    def on_closing():
        if window_holder.get("allow_close"):
            return True
        # Ask in the page (styled dialog); "Stop and exit" calls /api/exit, which closes the window.
        threading.Thread(target=lambda: window.evaluate_js("window.opusAskExit && window.opusAskExit()"), daemon=True).start()
        return False

    window.events.closing += on_closing
    try:
        webview.start(private_mode=False, storage_path=str(DATA / "webview"))
    except Exception as exc:  # noqa: BLE001 - no WebView2 runtime, etc.
        log.warning("The app window couldn't open (%s); using your browser instead", exc)
        webbrowser.open(url)
        return wait_for_ctrl_c(thread)
    thread.stop()
    thread.join(timeout=10)
    return 0


def wait_for_ctrl_c(thread: ServerThread) -> int:
    print("\nOpus Trader is running. Press Ctrl+C here to stop the bot and exit "
          "(open positions keep their stop-loss and take-profit at the broker).\n", flush=True)
    try:
        while thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        thread.stop()
        thread.join(timeout=10)
    return 0


def run_once(args) -> int:
    """Run a single decision cycle on the last closed bar and print it."""
    load_env()
    lock = InstanceLock((DATA / "sim" if args.sim else DATA) / "opus_trader.lock")
    if not lock.acquire():
        return already_running(lock)
    setup_logging((DATA / "sim" / "logs") if args.sim else (DATA / "logs"), args.verbose)
    engine, sim_env = build_engine(args)

    async def main():
        from .clock import last_closed_bar_open

        await engine.start(run_tasks=False)
        bar_open = last_closed_bar_open(engine.clock.now())
        did = await engine.safe_process_bar(bar_open, wait=False)
        await engine.shutdown()
        return did

    did = asyncio.run(main())
    from .tools import print_decision

    return print_decision(engine.store, did)
