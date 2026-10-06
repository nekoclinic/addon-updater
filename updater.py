from __future__ import annotations

import calendar
import json
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

# リポジトリ
GITHUB_REPO = "nekoclinic/addon-updater"

ADDON_DIR = Path(__file__).absolute().parent
PKG = __package__.split(".")[0] if __package__ else ADDON_DIR.name

_ID = re.sub(r"\W", "_", PKG).lower()
OP = f"{_ID}_upd"
PROP_PRE = f"{_ID}_upd_pre"
PROP_MONTHS = f"{_ID}_upd_months"
PROP_DAYS = f"{_ID}_upd_days"
PROP_HOURS = f"{_ID}_upd_hours"
PROP_MINUTES = f"{_ID}_upd_minutes"
PROP_SECONDS = f"{_ID}_upd_seconds"
PROP_TAG = f"{_ID}_upd_tag"

API_TIMEOUT = 15  # 一覧取得の無通信上限
DOWNLOAD_TIMEOUT = 60  # DLの無通信上限
POLL_INTERVAL = 0.3  # 完了確認の間隔
Version = tuple[int, ...]


# ---------------------------------------------------------------------------
# モデル
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Release:
    tag: str
    version: Version
    url: str
    name: str = ""
    prerelease: bool = False


@dataclass
class Settings:
    include_pre: bool = True
    interval_months: int = 0
    interval_days: int = 7
    interval_hours: int = 0
    interval_minutes: int = 0
    interval_seconds: int = 0
    last_check: float = 0.0
    last_notified: float = 0.0

    @staticmethod
    def path() -> Path:
        return Path(bpy.utils.user_resource("CONFIG")) / f"{PKG}_updater.json"

    @classmethod
    def load(cls) -> Settings:
        settings = cls()
        try:
            data = json.loads(cls.path().read_text(encoding="utf-8"))
            for f in fields(cls):
                if f.name in data:
                    setattr(settings, f.name, type(getattr(settings, f.name))(data[f.name]))
        except (OSError, ValueError, TypeError):
            pass
        return settings

    def save(self) -> None:
        try:
            self.path().write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        except OSError:
            pass

    def due_at(self, since: float) -> datetime:
        # since から設定間隔(月・日・時・分・秒)を足した時刻
        base = datetime.fromtimestamp(since)
        total_months = base.year * 12 + base.month - 1 + self.interval_months
        year, month_index = divmod(total_months, 12)
        month = month_index + 1
        day = min(base.day, calendar.monthrange(year, month)[1])
        return base.replace(year=year, month=month, day=day) + timedelta(
            days=self.interval_days,
            hours=self.interval_hours,
            minutes=self.interval_minutes,
            seconds=self.interval_seconds,
        )

    def interval_elapsed(self, since: float) -> bool:
        return not since or datetime.now() >= self.due_at(since)


@dataclass
class State:
    busy: bool = False
    notify_after_fetch: bool = False
    force_notify_after_fetch: bool = False
    show_restart_prompt: bool = False
    prompt_window: Optional[bpy.types.Window] = None
    message: str = ""
    releases: list[Release] = field(default_factory=list)
    last_check_pending: bool = False

    def clear_notify(self) -> None:
        self.notify_after_fetch = False
        self.force_notify_after_fetch = False


settings = Settings()
state = State()
_enum_items: list[tuple[str, str, str]] = [("NONE", "(Not checked)", "")]


# ---------------------------------------------------------------------------
# バージョン
# ---------------------------------------------------------------------------
def current_version() -> Version:
    info = getattr(sys.modules.get(PKG), "bl_info", None) or {}
    return tuple(info.get("version", (0, 0, 0)))


def parse_version(tag: str) -> Optional[Version]:
    m = re.match(r"^[vV]?(\d+(?:\.\d+)*)", tag.strip())
    return tuple(map(int, m.group(1).split("."))) if m else None


def format_version(ver: Version) -> str:
    return ".".join(map(str, ver))


def compare_versions(a: Version, b: Version) -> int:
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return (a > b) - (a < b)


def latest_newer() -> Optional[Release]:
    current = current_version()
    newer = (r for r in state.releases if compare_versions(r.version, current) > 0)
    return max(newer, key=lambda r: r.version, default=None)


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------
def _headers() -> dict[str, str]:
    return {
        "User-Agent": f"{PKG}-updater",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def format_error(error: BaseException) -> str:
    if isinstance(error, PermissionError):
        message = str(error)
        if getattr(error, "winerror", None) == 5 or error.errno == 5:
            message += " | Access denied to the add-on folder. Run Blender as administrator or install the add-on in a writable user folder."
        if error.filename:
            message += f" [Path: {error.filename}]"
        return message
    if isinstance(error, urllib.error.HTTPError):
        detail = ""
        try:
            detail = json.loads(error.read().decode("utf-8")).get("message", "")
        except (OSError, UnicodeDecodeError, ValueError, AttributeError):
            pass
        message = f"HTTP {error.code} {error.reason}"
        if detail:
            message += f" ({detail})"
        return f"{message}: {error.url}"
    return str(error)


def _redraw() -> None:
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            area.tag_redraw()


def _rebuild_enum() -> None:
    global _enum_items
    _enum_items = [(r.tag, f"{r.tag}{' [pre]' if r.prerelease else ''}", r.name) for r in state.releases] or [("NONE", "(No releases)", "")]
    wm = bpy.context.window_manager
    if getattr(wm, PROP_TAG) not in {item[0] for item in _enum_items}:
        setattr(wm, PROP_TAG, _enum_items[0][0])


def _enum_cb(self, context):
    return _enum_items


# ---------------------------------------------------------------------------
# GitHub 通信
# ---------------------------------------------------------------------------
def _parse_release(raw: dict, include_pre: bool) -> Optional[Release]:
    if raw.get("draft") or (raw.get("prerelease") and not include_pre):
        return None
    tag = raw.get("tag_name", "")
    version = parse_version(tag)
    if version is None:
        return None
    zip_asset = next((a for a in raw.get("assets", []) if a["name"].lower().endswith(".zip")), None)
    url = (zip_asset or {}).get("browser_download_url") or (
        f"https://github.com/{GITHUB_REPO}/archive/refs/tags/{urllib.parse.quote(tag, safe='')}.zip"
    )
    return Release(
        tag=tag,
        version=version,
        url=url,
        name=raw.get("name") or "",
        prerelease=bool(raw.get("prerelease")),
    )


def _fetch_worker(include_pre: bool) -> None:
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=50",
            headers=_headers(),
        )
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as res:
            data = json.load(res)
        parsed = (_parse_release(raw, include_pre) for raw in data)
        state.releases = sorted(filter(None, parsed), key=lambda r: r.version, reverse=True)
        state.message = f"Found {len(state.releases)} releases"
        state.last_check_pending = True
    except Exception as e:
        state.message = f"Check failed: {format_error(e)}"
        state.clear_notify()
    finally:
        state.busy = False


def _download_release(release: Release, dest: Path) -> None:
    req = urllib.request.Request(release.url, headers={"User-Agent": f"{PKG}-updater"})
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as res, dest.open("wb") as f:
        shutil.copyfileobj(res, f)


def _find_addon_root(path: Path) -> Optional[Path]:
    ignored = {"__pycache__", ".git"}
    for init in sorted(path.rglob("__init__.py"), key=lambda p: len(p.parts)):
        if ignored & set(init.relative_to(path).parts):
            continue
        try:
            if "bl_info" in init.read_text(encoding="utf-8"):
                return init.parent
        except OSError:
            continue
    return None


def _replace_addon_tree(source: Path) -> str:
    # アドオンフォルダを置換する。失敗時はロールバックし、警告文字列を返す
    staging_root = Path(tempfile.mkdtemp(prefix=".gh_updater_", dir=ADDON_DIR.parent))
    staged = staging_root / ADDON_DIR.name
    backup = staging_root / "previous"
    moved_old = installed_new = preserve_backup = False
    warning = ""
    try:
        shutil.copytree(source, staged, ignore=shutil.ignore_patterns("__pycache__"))
        ADDON_DIR.replace(backup)
        moved_old = True
        try:
            staged.replace(ADDON_DIR)
            installed_new = True
        except OSError as install_error:
            try:
                backup.replace(ADDON_DIR)
                moved_old = False
            except OSError as rollback_error:
                raise RuntimeError(
                    f"Could not install the new version or restore the previous one. "
                    f"Backup: {backup} / Install: {install_error} / Restore: {rollback_error}"
                ) from rollback_error
            raise
    finally:
        if moved_old and not installed_new:
            if not ADDON_DIR.exists():
                try:
                    backup.replace(ADDON_DIR)
                except OSError as rollback_error:
                    preserve_backup = True
                    warning = f"Could not restore the previous version. Backup: {backup} / {format_error(rollback_error)}"
            else:
                preserve_backup = True
                warning = f"Previous version backup kept: {backup}"
        if not preserve_backup:
            try:
                shutil.rmtree(staging_root)
            except OSError as cleanup_error:
                if installed_new:
                    warning = f"Could not remove previous-version backup: {staging_root} / {format_error(cleanup_error)}"
                elif not warning:
                    warning = f"Could not remove temporary files: {staging_root} / {format_error(cleanup_error)}"
    return warning


def _install_worker(release: Release) -> None:
    with tempfile.TemporaryDirectory(prefix="gh_updater_", ignore_cleanup_errors=True) as tmp:
        try:
            tmp_dir = Path(tmp)
            zip_path = tmp_dir / "pkg.zip"
            _download_release(release, zip_path)

            extract_dir = tmp_dir / "extract"
            with zipfile.ZipFile(zip_path) as z:
                z.extractall(extract_dir)
            src = _find_addon_root(extract_dir)
            if src is None:
                raise RuntimeError("No add-on (__init__.py + bl_info) found in the ZIP")

            warning = _replace_addon_tree(src)
            state.show_restart_prompt = True
            state.message = f"Installed {release.tag}" + (f" ({warning})" if warning else "")
        except Exception as e:
            state.message = f"Install failed: {format_error(e)}"
        finally:
            state.busy = False


def _start_worker(target: Callable[..., None], arg: object) -> None:
    threading.Thread(target=target, args=(arg,), daemon=True).start()
    bpy.app.timers.register(_poll, first_interval=POLL_INTERVAL)


def start_fetch(*, notify_after_fetch: bool = False, force_notify: bool = False) -> bool:
    if state.busy:
        return False
    state.busy = True
    state.notify_after_fetch = notify_after_fetch
    state.force_notify_after_fetch = force_notify
    state.message = "Checking releases..."
    _start_worker(_fetch_worker, settings.include_pre)
    return True


def start_install(release: Release, window: Optional[bpy.types.Window] = None) -> bool:
    if state.busy:
        return False
    state.busy = True
    state.clear_notify()
    state.prompt_window = window or bpy.context.window
    state.message = f"Downloading {release.tag}..."
    _start_worker(_install_worker, release)
    return True


# ---------------------------------------------------------------------------
# メインスレッド側の処理
# ---------------------------------------------------------------------------
def _op(name: str) -> Callable[..., set]:
    return getattr(getattr(bpy.ops, OP), name)


def _invoke_popup(invoke: Callable[[], set], label: str, preferred: Optional[bpy.types.Window] = None) -> Optional[bool]:
    # ポップアップをカーソル位置に開く。開けたら True、待つべきなら None、失敗なら False
    wm = bpy.context.window_manager
    windows = list(wm.windows)
    candidates: list[bpy.types.Window] = []
    for win in (preferred, bpy.context.window, *windows):
        if win is not None and win in windows and win not in candidates:
            candidates.append(win)

    last_error: Optional[Exception] = None
    for win in candidates:
        for area in win.screen.areas:
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            if region is None:
                continue
            try:
                with bpy.context.temp_override(window=win, area=area, region=region):
                    result = invoke()
                if "RUNNING_MODAL" in result:
                    return True
            except Exception as e:
                last_error = e

    if last_error:
        state.message = f"{label.capitalize()} failed: {last_error}"
        return False
    if not windows:
        return None
    state.message = f"Could not open the {label} popup"
    return False


def _maybe_notify(force: bool = False) -> None:
    if not latest_newer():
        return
    if not force and not settings.interval_elapsed(settings.last_notified):
        return
    if not bpy.context.window_manager.windows:
        return
    settings.last_notified = time.time()
    settings.save()
    _invoke_popup(lambda: _op("popup")("INVOKE_DEFAULT"), "update notification")


def _open_restart_prompt() -> Optional[bool]:
    return _invoke_popup(
        lambda: _op("restart_prompt")("INVOKE_DEFAULT"),
        "quit confirmation",
        state.prompt_window,
    )


def _poll() -> Optional[float]:
    if state.last_check_pending:
        state.last_check_pending = False
        settings.last_check = time.time()
        settings.save()
    if state.busy:
        return POLL_INTERVAL

    _rebuild_enum()
    _redraw()

    if state.show_restart_prompt:
        opened = _open_restart_prompt()
        if opened is None:
            return POLL_INTERVAL
        state.show_restart_prompt = False
        state.prompt_window = None

    if state.notify_after_fetch:
        force = state.force_notify_after_fetch
        state.clear_notify()
        _maybe_notify(force=force)
    return None


def _startup_check() -> Optional[float]:
    if bpy.app.background:
        return None
    if state.busy:
        return 1.0
    if settings.interval_elapsed(settings.last_check):
        start_fetch(notify_after_fetch=True, force_notify=True)
    return None


# ---------------------------------------------------------------------------
# オペレーター
# ---------------------------------------------------------------------------
class GHUPD_OT_check(bpy.types.Operator):
    bl_idname = f"{OP}.check"
    bl_label = "Check for Updates"

    def execute(self, context):
        return {"FINISHED"} if start_fetch(notify_after_fetch=True) else {"CANCELLED"}


class GHUPD_OT_install(bpy.types.Operator):
    bl_idname = f"{OP}.install"
    bl_label = "Install Version"

    @staticmethod
    def _target(context) -> Optional[Release]:
        tag = getattr(context.window_manager, PROP_TAG)
        return next((r for r in state.releases if r.tag == tag), None)

    def invoke(self, context, event):
        rel = self._target(context)
        if rel is None:
            return {"CANCELLED"}
        return context.window_manager.invoke_confirm(
            self,
            event,
            message=f"Install {rel.tag} over v{format_version(current_version())}?",
        )

    def execute(self, context):
        rel = self._target(context)
        if rel is None:
            return {"CANCELLED"}
        return {"FINISHED"} if start_install(rel, context.window) else {"CANCELLED"}


class GHUPD_OT_install_latest(bpy.types.Operator):
    bl_idname = f"{OP}.install_latest"
    bl_label = "Install"
    bl_description = "Install the latest version."
    bl_options = {"INTERNAL"}

    def execute(self, context):
        rel = latest_newer()
        if rel is None:
            return {"CANCELLED"}
        return {"FINISHED"} if start_install(rel, context.window) else {"CANCELLED"}


class GHUPD_OT_popup(bpy.types.Operator):
    bl_idname = f"{OP}.popup"
    bl_label = "Update Available"
    bl_description = "Show the update notification."
    bl_options = {"INTERNAL"}

    def invoke(self, context, event):
        if not latest_newer():
            return {"CANCELLED"}
        return context.window_manager.invoke_popup(self, width=340)

    def draw(self, context):
        rel = latest_newer()
        if rel is None:
            return
        layout = self.layout
        layout.label(text="Update Available", icon="INFO")
        col = layout.column()
        pre = " (Pre-release)" if rel.prerelease else ""
        col.label(text=f"Available: {rel.tag}{pre}")
        col.label(text=f"Installed: v{format_version(current_version())}")
        button = layout.row()
        button.scale_y = 1.5
        button.operator(f"{OP}.install_latest", text="Install", icon="IMPORT")

    def execute(self, context):
        return {"CANCELLED"}


class GHUPD_OT_restart_prompt(bpy.types.Operator):
    bl_idname = f"{OP}.restart_prompt"
    bl_label = "Restart Blender"
    bl_options = {"INTERNAL"}

    def invoke(self, context, event):
        wm = context.window_manager
        try:
            return wm.invoke_popup(self, width=360)
        except (RuntimeError, TypeError):
            return wm.invoke_props_dialog(self, width=360, title="Update Installed")

    def draw(self, context):
        layout = self.layout
        layout.label(text="Restart Blender to apply the update.")
        layout.label(text="Save your work before quitting.")
        row = layout.row()
        row.alert = True
        row.operator("wm.quit_blender", text="Quit Blender", icon="QUIT")

    def execute(self, context):
        return {"CANCELLED"}


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
def draw(layout: bpy.types.UILayout, context: bpy.types.Context) -> None:
    wm = context.window_manager
    layout.use_property_split = False
    layout.use_property_decorate = False

    layout.label(text="Updater Settings")
    layout.prop(wm, PROP_PRE, text="Include Pre-releases")

    interval_row = layout.row(align=True)
    interval_row.alignment = "RIGHT"
    interval_row.prop(wm, PROP_MONTHS, text="Months")
    interval_row.prop(wm, PROP_DAYS, text="Days")
    interval_row.prop(wm, PROP_HOURS, text="Hours")
    interval_row.prop(wm, PROP_MINUTES, text="Minutes")
    interval_row.prop(wm, PROP_SECONDS, text="Seconds")

    controls = layout.row(align=True)
    check_slot = controls.row(align=True)
    check_slot.scale_x = 0.95
    check_slot.scale_y = 2.5
    check_slot.enabled = not state.busy
    check_text = "Check"
    if settings.last_check:
        check_text = f"Check ({datetime.fromtimestamp(settings.last_check):%Y-%m-%d %H:%M:%S})"
    check_slot.operator(f"{OP}.check", text=check_text, icon="FILE_REFRESH")

    side_controls = controls.column(align=True)
    install_controls = side_controls.column(align=True)
    install_controls.enabled = not state.busy
    if state.releases:
        install_controls.prop(wm, PROP_TAG, text="")
    else:
        install_controls.label(text="Version", icon="DOWNARROW_HLT")
    install_button = install_controls.row(align=True)
    install_button.scale_y = 1.5
    install_button.enabled = not state.busy and bool(state.releases)
    install_button.operator(f"{OP}.install", text="Install", icon="IMPORT")


classes = (
    GHUPD_OT_check,
    GHUPD_OT_install,
    GHUPD_OT_install_latest,
    GHUPD_OT_popup,
    GHUPD_OT_restart_prompt,
)

_INTERVAL_PROPS = (
    # (WindowManager 属性, Settings フィールド, 表示名, 最大値)
    (PROP_MONTHS, "interval_months", "Months", 120),
    (PROP_DAYS, "interval_days", "Days", 365),
    (PROP_HOURS, "interval_hours", "Hours", 23),
    (PROP_MINUTES, "interval_minutes", "Minutes", 59),
    (PROP_SECONDS, "interval_seconds", "Seconds", 59),
)


def _accessors(key: str, cast: Callable):
    def getter(self):
        return cast(getattr(settings, key))

    def setter(self, value):
        setattr(settings, key, cast(value))
        settings.save()

    return getter, setter


def register(repo=""):
    global settings, GITHUB_REPO
    settings = Settings.load()
    if repo:
        GITHUB_REPO = repo

    wm_type = bpy.types.WindowManager
    get, set_ = _accessors("include_pre", bool)
    setattr(wm_type, PROP_PRE, BoolProperty(name="Include Pre-releases", get=get, set=set_))

    for attr, key, label, maximum in _INTERVAL_PROPS:
        get, set_ = _accessors(key, int)
        setattr(
            wm_type,
            attr,
            IntProperty(
                name=label,
                description=f"{label} between update notifications",
                min=0,
                max=maximum,
                get=get,
                set=set_,
            ),
        )
    setattr(wm_type, PROP_TAG, EnumProperty(name="Version", items=_enum_cb))

    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.app.timers.register(_startup_check, first_interval=0.0)


def unregister():
    for fn in (_startup_check, _poll):
        if bpy.app.timers.is_registered(fn):
            bpy.app.timers.unregister(fn)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    wm_type = bpy.types.WindowManager
    delattr(wm_type, PROP_TAG)
    for attr, *_ in reversed(_INTERVAL_PROPS):
        delattr(wm_type, attr)
    delattr(wm_type, PROP_PRE)
