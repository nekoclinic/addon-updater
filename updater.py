import calendar
import json
import os
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
from datetime import datetime, timedelta

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty

# 設定
GITHUB_REPO = "nekoclinic/addon-updater"  # リポジトリ

ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
PKG = __package__.split(".")[0] if __package__ else os.path.basename(ADDON_DIR)

_DEFAULTS = {
    "include_pre": True,
    "interval_months": 0,
    "interval_days": 7,
    "interval_hours": 0,
    "interval_minutes": 0,
    "interval_seconds": 0,
    "last_check": 0.0,
    "last_notified": 0.0,
}
_settings = dict(_DEFAULTS)

# 状態
_state = {
    "busy": False,
    "notify_after_fetch": False,
    "force_notify_after_fetch": False,
    "show_restart_prompt": False,
    "prompt_window": None,
    "message": "",
    "releases": [],
    "last_check_pending": False,
}
_enum_items = [("NONE", "(Not checked)", "")]


# 設定の保存
def _settings_path():
    return os.path.join(bpy.utils.user_resource("CONFIG"), f"{PKG}_updater.json")


def _load_settings():
    _settings.update(_DEFAULTS)
    try:
        with open(_settings_path(), encoding="utf-8") as f:
            data = json.load(f)
        for k in _DEFAULTS:
            if k in data:
                _settings[k] = type(_DEFAULTS[k])(data[k])
    except (OSError, ValueError, TypeError):
        pass


def _save_settings():
    try:
        with open(_settings_path(), "w", encoding="utf-8") as f:
            json.dump(_settings, f, indent=2)
    except OSError:
        pass


def _accessors(key, cast):
    def getter(self):
        return cast(_settings[key])

    def setter(self, value):
        _settings[key] = cast(value)
        _save_settings()

    return getter, setter


# 共通処理
def current_version():
    mod = sys.modules.get(PKG)
    info = getattr(mod, "bl_info", {}) or {}
    return tuple(info.get("version", (0, 0, 0)))


def parse_version(tag):
    m = re.match(r"^[vV]?(\d+(?:\.\d+)*)", tag.strip())
    if not m:
        return None
    return tuple(int(x) for x in m.group(1).split("."))


def _fmt(ver):
    return ".".join(map(str, ver))


def _cmp_ver(a, b):
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return (a > b) - (a < b)


def _headers():
    return {
        "User-Agent": f"{PKG}-updater",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _format_error(error):
    if isinstance(error, PermissionError):
        path = error.filename or ""
        message = f"{error}"
        if getattr(error, "winerror", None) == 5 or error.errno == 5:
            message += " | Access denied to the add-on folder. Run Blender as administrator or install the add-on in a writable user folder."
        if path:
            message += f" [Path: {path}]"
        return message
    if isinstance(error, urllib.error.HTTPError):
        detail = ""
        try:
            payload = json.loads(error.read().decode("utf-8"))
            detail = payload.get("message", "")
        except (OSError, UnicodeDecodeError, ValueError, AttributeError):
            pass
        message = f"HTTP {error.code} {error.reason}"
        if detail:
            message += f" ({detail})"
        message += f": {error.url}"
        return message
    return str(error)


def _redraw():
    wm = bpy.context.window_manager
    for win in wm.windows:
        for area in win.screen.areas:
            area.tag_redraw()


def _rebuild_enum():
    global _enum_items
    items = []
    for r in _state["releases"]:
        pre = " [pre]" if r["prerelease"] else ""
        items.append((r["tag"], f"{r['tag']}{pre}", r["name"] or ""))
    _enum_items = items or [("NONE", "(No releases)", "")]
    wm = bpy.context.window_manager
    if wm.gh_updater_tag not in {item[0] for item in _enum_items}:
        wm.gh_updater_tag = _enum_items[0][0]


def _latest_newer():
    newer_releases = (release for release in _state["releases"] if _cmp_ver(release["version"], current_version()) > 0)
    return max(newer_releases, key=lambda release: release["version"], default=None)


def _interval_elapsed(timestamp):
    if not timestamp:
        return True
    last_time = datetime.fromtimestamp(timestamp)
    total_months = last_time.year * 12 + last_time.month - 1 + _settings["interval_months"]
    year, month_index = divmod(total_months, 12)
    month = month_index + 1
    day = min(last_time.day, calendar.monthrange(year, month)[1])
    due_at = last_time.replace(year=year, month=month, day=day)
    due_at += timedelta(
        days=_settings["interval_days"],
        hours=_settings["interval_hours"],
        minutes=_settings["interval_minutes"],
        seconds=_settings["interval_seconds"],
    )
    return datetime.now() >= due_at


def _maybe_notify(force=False):
    if not _latest_newer():
        return
    if not force and not _interval_elapsed(_settings["last_notified"]):
        return
    wm = bpy.context.window_manager
    if not wm.windows:
        return
    _settings["last_notified"] = time.time()
    _save_settings()
    try:
        with bpy.context.temp_override(window=wm.windows[0]):
            bpy.ops.ghupd.popup("INVOKE_DEFAULT")
    except Exception as e:
        _state["message"] = f"Notification popup failed: {e}"


def _poll():
    if _state["last_check_pending"]:
        _state["last_check_pending"] = False
        _settings["last_check"] = time.time()
        _save_settings()
    if _state["busy"]:
        return 0.3
    _rebuild_enum()
    _redraw()
    if _state["show_restart_prompt"]:
        wm = bpy.context.window_manager
        preferred_window = _state["prompt_window"]
        active_window = bpy.context.window
        windows = list(wm.windows)
        ordered_windows = []
        for win in (preferred_window, active_window, *windows):
            if win is not None and win in windows and win not in ordered_windows:
                ordered_windows.append(win)

        popup_opened = False
        last_error = None
        for win in ordered_windows:
            for area in win.screen.areas:
                region = next(
                    (item for item in area.regions if item.type == "WINDOW"),
                    None,
                )
                if region is None:
                    continue
                try:
                    with bpy.context.temp_override(
                        window=win,
                        area=area,
                        region=region,
                    ):
                        result = bpy.ops.ghupd.restart_prompt("INVOKE_DEFAULT")
                    if "RUNNING_MODAL" in result:
                        popup_opened = True
                        break
                except Exception as e:
                    last_error = e
            if popup_opened:
                break
        if popup_opened:
            _state["show_restart_prompt"] = False
            _state["prompt_window"] = None
        elif last_error:
            _state["message"] = f"Quit confirmation failed: {last_error}"
            _state["show_restart_prompt"] = False
        elif not wm.windows:
            return 0.3
        else:
            _state["message"] = "Could not open the quit confirmation popup"
            _state["show_restart_prompt"] = False
    if _state["notify_after_fetch"]:
        _state["notify_after_fetch"] = False
        force_notify = _state["force_notify_after_fetch"]
        _state["force_notify_after_fetch"] = False
        _maybe_notify(force=force_notify)
    return None


def _startup_check():
    if bpy.app.background:
        return None
    if _state["busy"]:
        return 1.0
    if _interval_elapsed(_settings["last_check"]):
        _start_fetch(notify_after_fetch=True, force_notify=True)
    return None


def _enum_cb(self, context):
    return _enum_items


# GitHub通信
def _fetch_worker(include_pre):
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=50"
        req = urllib.request.Request(url, headers=_headers())
        with urllib.request.urlopen(req, timeout=15) as res:
            data = json.load(res)

        releases = []
        for rel in data:
            if rel.get("draft"):
                continue
            if rel.get("prerelease") and not include_pre:
                continue
            ver = parse_version(rel.get("tag_name", ""))
            if ver is None:
                continue
            zip_asset = next(
                (asset for asset in rel.get("assets", []) if asset["name"].lower().endswith(".zip")),
                None,
            )
            browser_url = zip_asset.get("browser_download_url") if zip_asset else None
            download_url = browser_url or (f"https://github.com/{GITHUB_REPO}/archive/refs/tags/{urllib.parse.quote(rel['tag_name'], safe='')}.zip")
            releases.append(
                {
                    "tag": rel["tag_name"],
                    "version": ver,
                    "url": download_url,
                    "name": rel.get("name", ""),
                    "prerelease": bool(rel.get("prerelease")),
                }
            )
        releases.sort(key=lambda r: r["version"], reverse=True)
        _state["releases"] = releases
        _state["message"] = f"Found {len(releases)} releases"
        _state["last_check_pending"] = True
    except Exception as e:
        _state["message"] = f"Check failed: {_format_error(e)}"
        _state["notify_after_fetch"] = False
        _state["force_notify_after_fetch"] = False
    finally:
        _state["busy"] = False


def _find_addon_root(path):
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
        if "__init__.py" in files:
            try:
                with open(os.path.join(root, "__init__.py"), encoding="utf-8") as f:
                    if "bl_info" in f.read():
                        return root
            except OSError:
                pass
    return None


def _replace_addon_tree(source):
    parent = os.path.dirname(ADDON_DIR)
    staging_root = tempfile.mkdtemp(prefix=".gh_updater_", dir=parent)
    staged_addon = os.path.join(staging_root, os.path.basename(ADDON_DIR))
    backup = os.path.join(staging_root, "previous")
    moved_old_addon = False
    installed_new_addon = False
    preserve_backup = False
    cleanup_warning = ""
    try:
        shutil.copytree(
            source,
            staged_addon,
            ignore=shutil.ignore_patterns("__pycache__"),
        )

        os.replace(ADDON_DIR, backup)
        moved_old_addon = True
        try:
            os.replace(staged_addon, ADDON_DIR)
            installed_new_addon = True
        except OSError as install_error:
            try:
                os.replace(backup, ADDON_DIR)
                moved_old_addon = False
            except OSError as rollback_error:
                raise RuntimeError(
                    f"Could not install the new version or restore the previous one. "
                    f"Backup: {backup} / Install: {install_error} / "
                    f"Restore: {rollback_error}"
                ) from rollback_error
            raise
    finally:
        if moved_old_addon and not installed_new_addon:
            if not os.path.exists(ADDON_DIR):
                try:
                    os.replace(backup, ADDON_DIR)
                except OSError as rollback_error:
                    preserve_backup = True
                    cleanup_warning = f"Could not restore the previous version. Backup: {backup} / {_format_error(rollback_error)}"
            else:
                preserve_backup = True
                cleanup_warning = f"Previous version backup kept: {backup}"
        if not preserve_backup:
            try:
                shutil.rmtree(staging_root)
            except OSError as cleanup_error:
                if installed_new_addon:
                    cleanup_warning = f"Could not remove previous-version backup: {staging_root} / {_format_error(cleanup_error)}"
                elif not cleanup_warning:
                    cleanup_warning = f"Could not remove temporary files: {staging_root} / {_format_error(cleanup_error)}"
    return cleanup_warning


def _download_release(release, zip_path):
    download_url = release["url"]
    headers = {"User-Agent": f"{PKG}-updater"}
    req = urllib.request.Request(download_url, headers=headers)
    response = urllib.request.urlopen(req, timeout=60)
    with response, open(zip_path, "wb") as f:
        shutil.copyfileobj(response, f)


def _install_worker(release):
    tmp = tempfile.mkdtemp(prefix="gh_updater_")
    try:
        zip_path = os.path.join(tmp, "pkg.zip")
        _download_release(release, zip_path)

        ext = os.path.join(tmp, "extract")
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(ext)
        src = _find_addon_root(ext)
        if not src:
            raise RuntimeError("No add-on (__init__.py + bl_info) found in the ZIP")

        cleanup_warning = _replace_addon_tree(src)

        _state["show_restart_prompt"] = True
        _state["message"] = f"Installed {release['tag']}"
        if cleanup_warning:
            _state["message"] += f" ({cleanup_warning})"
    except Exception as e:
        _state["message"] = f"Install failed: {_format_error(e)}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        _state["busy"] = False


def _start_fetch(notify_after_fetch=False, force_notify=False):
    if _state["busy"]:
        return False
    _state["busy"] = True
    _state["notify_after_fetch"] = notify_after_fetch
    _state["force_notify_after_fetch"] = force_notify
    _state["message"] = "Checking releases..."
    threading.Thread(target=_fetch_worker, args=(_settings["include_pre"],), daemon=True).start()
    bpy.app.timers.register(_poll, first_interval=0.3)
    return True


def _start_install(release, window=None):
    if _state["busy"]:
        return False
    _state["busy"] = True
    _state["notify_after_fetch"] = False
    _state["force_notify_after_fetch"] = False
    _state["prompt_window"] = window or bpy.context.window
    _state["message"] = f"Downloading {release['tag']}..."
    threading.Thread(target=_install_worker, args=(release,), daemon=True).start()
    bpy.app.timers.register(_poll, first_interval=0.3)
    return True


# UIオペレーター
class GHUPD_OT_check(bpy.types.Operator):
    bl_idname = "ghupd.check"
    bl_label = "Check for Updates"

    def execute(self, context):
        return {"FINISHED"} if _start_fetch(notify_after_fetch=True) else {"CANCELLED"}


class GHUPD_OT_install(bpy.types.Operator):
    bl_idname = "ghupd.install"
    bl_label = "Install Version"

    def _target(self, context):
        tag = context.window_manager.gh_updater_tag
        return next((r for r in _state["releases"] if r["tag"] == tag), None)

    def invoke(self, context, event):
        rel = self._target(context)
        if not rel:
            return {"CANCELLED"}
        return context.window_manager.invoke_confirm(
            self,
            event,
            message=f"Install {rel['tag']} over v{_fmt(current_version())}?",
        )

    def execute(self, context):
        rel = self._target(context)
        if not rel:
            return {"CANCELLED"}
        return {"FINISHED"} if _start_install(rel, context.window) else {"CANCELLED"}


class GHUPD_OT_popup(bpy.types.Operator):
    bl_idname = "ghupd.popup"
    bl_label = "Update Available"
    bl_description = "Show the update notification."
    bl_options = {"INTERNAL"}

    def invoke(self, context, event):
        if not _latest_newer():
            return {"CANCELLED"}
        wm = context.window_manager
        try:
            return wm.invoke_props_dialog(self, width=340, title="Update Available", confirm_text="Install")
        except TypeError:  # 旧Blenderではconfirm_text非対応
            return wm.invoke_props_dialog(self, width=340)

    def draw(self, context):
        rel = _latest_newer()
        col = self.layout.column()
        pre = " (Pre-release)" if rel["prerelease"] else ""
        col.label(text=f"Available: {rel['tag']}{pre}")
        col.label(text=f"Installed: v{_fmt(current_version())}")

    def execute(self, context):
        rel = _latest_newer()
        if not rel:
            return {"CANCELLED"}
        return {"FINISHED"} if _start_install(rel, context.window) else {"CANCELLED"}


class GHUPD_OT_restart_prompt(bpy.types.Operator):
    bl_idname = "ghupd.restart_prompt"
    bl_label = "Restart Blender"
    bl_options = {"INTERNAL"}

    def invoke(self, context, event):
        try:
            return context.window_manager.invoke_popup(self, width=360)
        except (RuntimeError, TypeError):
            return context.window_manager.invoke_props_dialog(self, width=360, title="Update Installed")

    def draw(self, context):
        self.layout.label(text="Restart Blender to apply the update.")
        self.layout.label(text="Save your work before quitting.")
        quit_row = self.layout.row()
        quit_row.alert = True
        quit_row.operator("wm.quit_blender", text="Quit Blender", icon="QUIT")

    def execute(self, context):
        return {"CANCELLED"}


# UI
def draw(layout, context):
    wm = context.window_manager
    layout.use_property_split = False
    layout.use_property_decorate = False

    layout.label(text="Updater Settings")
    layout.prop(wm, "gh_updater_pre", text="Include Pre-releases")

    interval_row = layout.row(align=True)
    interval_row.alignment = "RIGHT"
    interval_row.prop(wm, "gh_updater_months", text="Months")
    interval_row.prop(wm, "gh_updater_days", text="Days")
    interval_row.prop(wm, "gh_updater_hours", text="Hours")
    interval_row.prop(wm, "gh_updater_minutes", text="Minutes")
    interval_row.prop(wm, "gh_updater_seconds", text="Seconds")

    controls = layout.row(align=True)
    check_slot = controls.row(align=True)
    check_slot.scale_x = 0.95
    check_slot.scale_y = 2.5
    check_slot.enabled = not _state["busy"]
    check_text = "Check"
    last = _settings["last_check"]
    if last:
        checked_at = datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M:%S")
        check_text = f"Check ({checked_at})"
    check_slot.operator("ghupd.check", text=check_text, icon="FILE_REFRESH")

    side_controls = controls.column(align=True)
    update_controls = side_controls.column(align=True)
    update_controls.enabled = not _state["busy"]
    if _state["releases"]:
        update_controls.prop(wm, "gh_updater_tag", text="")
    else:
        update_controls.label(text="Version", icon="DOWNARROW_HLT")
    update_button = update_controls.row(align=True)
    update_button.scale_y = 1.5
    update_button.enabled = not _state["busy"] and bool(_state["releases"])
    update_button.operator("ghupd.install", text="Update", icon="IMPORT")


# 登録と解除
classes = (
    GHUPD_OT_check,
    GHUPD_OT_install,
    GHUPD_OT_popup,
    GHUPD_OT_restart_prompt,
)


def register():
    _load_settings()

    g, s = _accessors("include_pre", bool)
    bpy.types.WindowManager.gh_updater_pre = BoolProperty(name="Include Pre-releases", get=g, set=s)
    g, s = _accessors("interval_months", int)
    bpy.types.WindowManager.gh_updater_months = IntProperty(
        name="Months", description="Months between update notifications", min=0, max=120, get=g, set=s
    )
    g, s = _accessors("interval_days", int)
    bpy.types.WindowManager.gh_updater_days = IntProperty(name="Days", description="Days between update notifications", min=0, max=365, get=g, set=s)
    g, s = _accessors("interval_hours", int)
    bpy.types.WindowManager.gh_updater_hours = IntProperty(
        name="Hours", description="Hours between update notifications", min=0, max=23, get=g, set=s
    )
    g, s = _accessors("interval_minutes", int)
    bpy.types.WindowManager.gh_updater_minutes = IntProperty(
        name="Minutes", description="Minutes between update notifications", min=0, max=59, get=g, set=s
    )
    g, s = _accessors("interval_seconds", int)
    bpy.types.WindowManager.gh_updater_seconds = IntProperty(
        name="Seconds", description="Seconds between update notifications", min=0, max=59, get=g, set=s
    )
    bpy.types.WindowManager.gh_updater_tag = EnumProperty(name="Version", items=_enum_cb)

    for c in classes:
        bpy.utils.register_class(c)
    bpy.app.timers.register(_startup_check, first_interval=0.0)


def unregister():
    if bpy.app.timers.is_registered(_startup_check):
        bpy.app.timers.unregister(_startup_check)
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
    del bpy.types.WindowManager.gh_updater_tag
    del bpy.types.WindowManager.gh_updater_seconds
    del bpy.types.WindowManager.gh_updater_minutes
    del bpy.types.WindowManager.gh_updater_hours
    del bpy.types.WindowManager.gh_updater_days
    del bpy.types.WindowManager.gh_updater_months
    del bpy.types.WindowManager.gh_updater_pre
