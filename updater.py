"""
GitHub Releases ベースのアドオン更新/ダウングレードモジュール。

使い方:
    1. このファイルをアドオンのルート (__init__.py と同じ階層) に置く
    2. GITHUB_REPO を書き換える
    3. __init__.py の register()/unregister() から updater.register()/unregister() を呼ぶ
    4. AddonPreferences.draw() 内で updater.draw(self.layout, context) を呼ぶ

機能:
    - 手動確認でリリース一覧を取得 (プレリリース含む: 初期ON)
    - 最新版が現在より新しく、設定した通知間隔が経っていればポップアップで通知
    - Preferences から任意のバージョンへ更新/ダウングレード
    - 設定は Blender の config フォルダの JSON に保存 (アドオン入れ替えで消えない)
"""

import calendar
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from datetime import datetime, timedelta

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty

# ---------------------------------------------------------------- 設定
GITHUB_REPO = "nekoclinic/blender-updater"      # 例: "hoshinome/blender-retouch"

ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
PKG = __package__.split(".")[0] if __package__ else os.path.basename(ADDON_DIR)
ENV_FILE = os.path.join(ADDON_DIR, ".env")   # GITHUB_TOKEN=github_pat_xxx (private repo の場合のみ)

_DEFAULTS = {
    "include_pre": True,        # プレリリースを含める (初期ON)
    "interval_months": 0,
    "interval_days": 7,
    "interval_hours": 0,
    "interval_minutes": 0,
    "interval_seconds": 0,
    "last_check": 0.0,          # 最後にリリース取得に成功した時刻
    "last_notified": 0.0,       # 最後にポップアップを出した時刻
}
_settings = dict(_DEFAULTS)

# ---------------------------------------------------------------- 状態
_state = {
    "busy": False,
    "notify_after_fetch": False,
    "message": "",
    "releases": [],      # [{tag, version, url, name, prerelease}]
    "need_restart": False,
}
_enum_items = [("NONE", "(未取得)", "")]


# ---------------------------------------------------------------- 設定の保存/読み込み
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


# ---------------------------------------------------------------- トークン
def _token():
    """.env (アドオン直下) → 環境変数 の順で GITHUB_TOKEN を探す。毎回読むので再起動不要。"""
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == "GITHUB_TOKEN":
                    return v.strip().strip("'\"")
    except OSError:
        pass
    return os.environ.get("GITHUB_TOKEN", "")


# ---------------------------------------------------------------- ユーティリティ
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
    h = {"User-Agent": f"{PKG}-updater", "Accept": "application/vnd.github+json"}
    token = _token()
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _redraw():
    wm = bpy.context.window_manager
    for win in wm.windows:
        for area in win.screen.areas:
            area.tag_redraw()


def _rebuild_enum():
    global _enum_items
    cur = current_version()
    items = []
    for r in _state["releases"]:
        c = _cmp_ver(r["version"], cur)
        mark = "現在" if c == 0 else ("新しい" if c > 0 else "旧")
        pre = " [pre]" if r["prerelease"] else ""
        items.append((r["tag"], f'{r["tag"]}{pre}  ({mark})', r["name"] or ""))
    _enum_items = items or [("NONE", "(リリースなし)", "")]
    wm = bpy.context.window_manager
    if wm.gh_updater_tag not in [i[0] for i in _enum_items]:
        wm.gh_updater_tag = _enum_items[0][0]


def _latest_newer():
    """現在より新しい最新リリースを返す。なければ None。"""
    rels = _state["releases"]
    if rels and _cmp_ver(rels[0]["version"], current_version()) > 0:
        return rels[0]
    return None


def _maybe_notify():
    """手動確認で更新が見つかり、設定した通知間隔が経過していれば通知する。"""
    if not _latest_newer():
        return
    if _settings["last_notified"]:
        last_notified = datetime.fromtimestamp(_settings["last_notified"])
        total_months = (
            last_notified.year * 12 + last_notified.month - 1
            + _settings["interval_months"]
        )
        year, month_index = divmod(total_months, 12)
        month = month_index + 1
        day = min(last_notified.day, calendar.monthrange(year, month)[1])
        due_at = last_notified.replace(year=year, month=month, day=day)
        due_at += timedelta(
            days=_settings["interval_days"],
            hours=_settings["interval_hours"],
            minutes=_settings["interval_minutes"],
            seconds=_settings["interval_seconds"],
        )
        if datetime.now() < due_at:
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
        _state["message"] = f"通知ポップアップ失敗: {e}"


def _poll():
    if _state["busy"]:
        return 0.3
    _rebuild_enum()
    _redraw()
    if _state["notify_after_fetch"]:
        _state["notify_after_fetch"] = False
        _maybe_notify()
    return None


def _enum_cb(self, context):
    return _enum_items


# ---------------------------------------------------------------- ワーカー
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
            dl = None
            for a in rel.get("assets", []):
                if a["name"].lower().endswith(".zip"):
                    dl = a["url"] if _token() else a["browser_download_url"]
                    break
            if not dl:
                dl = rel.get("zipball_url")
            if not dl:
                continue
            releases.append({
                "tag": rel["tag_name"],
                "version": ver,
                "url": dl,
                "name": rel.get("name", ""),
                "prerelease": bool(rel.get("prerelease")),
            })
        releases.sort(key=lambda r: r["version"], reverse=True)
        _state["releases"] = releases
        _state["message"] = f"{len(releases)} 件のリリースを取得"
        _settings["last_check"] = time.time()
        _save_settings()
    except Exception as e:
        _state["message"] = f"取得失敗: {e}"
        _state["notify_after_fetch"] = False
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


def _install_worker(release):
    tmp = tempfile.mkdtemp(prefix="gh_updater_")
    backup = os.path.join(tmp, "backup")
    replaced = False
    try:
        # ダウンロード
        zip_path = os.path.join(tmp, "pkg.zip")
        headers = _headers()
        if _token() and "api.github.com" in release["url"]:
            headers["Accept"] = "application/octet-stream"
        req = urllib.request.Request(release["url"], headers=headers)
        with urllib.request.urlopen(req, timeout=60) as res, open(zip_path, "wb") as f:
            shutil.copyfileobj(res, f)

        # 展開
        ext = os.path.join(tmp, "extract")
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(ext)
        src = _find_addon_root(ext)
        if not src:
            raise RuntimeError("zip内にアドオン(__init__.py + bl_info)が見つかりません")

        # バックアップ → 入れ替え (失敗したらロールバック)
        shutil.copytree(ADDON_DIR, backup)
        replaced = True
        for name in os.listdir(ADDON_DIR):
            p = os.path.join(ADDON_DIR, name)
            if name in ("__pycache__", ".env"):
                continue
            if os.path.isdir(p):
                shutil.rmtree(p)
            else:
                os.remove(p)
        shutil.copytree(src, ADDON_DIR, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(".env"))

        _state["need_restart"] = True
        _state["message"] = f'{release["tag"]} をインストールしました'
    except Exception as e:
        if replaced and os.path.isdir(backup):
            try:
                shutil.copytree(backup, ADDON_DIR, dirs_exist_ok=True)
            except Exception:
                pass
        _state["message"] = f"インストール失敗(ロールバック済): {e}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        _state["busy"] = False


def _start_fetch(notify_after_fetch=False):
    if _state["busy"]:
        return False
    _state["busy"] = True
    _state["notify_after_fetch"] = notify_after_fetch
    _state["message"] = "取得中..."
    threading.Thread(
        target=_fetch_worker, args=(_settings["include_pre"],), daemon=True
    ).start()
    bpy.app.timers.register(_poll, first_interval=0.3)
    return True


def _start_install(release):
    if _state["busy"]:
        return False
    _state["busy"] = True
    _state["notify_after_fetch"] = False
    _state["message"] = f'{release["tag"]} をダウンロード中...'
    threading.Thread(target=_install_worker, args=(release,), daemon=True).start()
    bpy.app.timers.register(_poll, first_interval=0.3)
    return True


# ---------------------------------------------------------------- オペレーター
class GHUPD_OT_check(bpy.types.Operator):
    bl_idname = "ghupd.check"
    bl_label = "リリースを取得"

    def execute(self, context):
        return {"FINISHED"} if _start_fetch(notify_after_fetch=True) else {"CANCELLED"}


class GHUPD_OT_install(bpy.types.Operator):
    bl_idname = "ghupd.install"
    bl_label = "選択バージョンをインストール"

    def _target(self, context):
        tag = context.window_manager.gh_updater_tag
        return next((r for r in _state["releases"] if r["tag"] == tag), None)

    def invoke(self, context, event):
        rel = self._target(context)
        if not rel:
            return {"CANCELLED"}
        return context.window_manager.invoke_confirm(
            self, event,
            message=f'v{_fmt(current_version())} → {rel["tag"]} に入れ替えます。よろしいですか?',
        )

    def execute(self, context):
        rel = self._target(context)
        if not rel:
            return {"CANCELLED"}
        return {"FINISHED"} if _start_install(rel) else {"CANCELLED"}


class GHUPD_OT_popup(bpy.types.Operator):
    """新しいバージョンがあるときに出すポップアップ"""
    bl_idname = "ghupd.popup"
    bl_label = "アップデートがあります"
    bl_options = {"INTERNAL"}

    def invoke(self, context, event):
        wm = context.window_manager
        try:
            return wm.invoke_props_dialog(
                self, width=340, title="アップデートがあります", confirm_text="アップデート")
        except TypeError:   # 古いBlender用フォールバック
            return wm.invoke_props_dialog(self, width=340)

    def draw(self, context):
        rel = _latest_newer()
        col = self.layout.column()
        if not rel:
            col.label(text="最新です")
            return
        pre = " (プレリリース)" if rel["prerelease"] else ""
        col.label(text=f'新しいバージョン: {rel["tag"]}{pre}')
        col.label(text=f"現在のバージョン: v{_fmt(current_version())}")
        col.label(text="「アップデート」で今すぐインストールします")

    def execute(self, context):
        rel = _latest_newer()
        if not rel:
            return {"CANCELLED"}
        return {"FINISHED"} if _start_install(rel) else {"CANCELLED"}


# ---------------------------------------------------------------- UI
def draw(layout, context):
    wm = context.window_manager
    box = layout.box()
    box.label(text=f"アップデーター  (現在: v{_fmt(current_version())})", icon="URL")

    col = box.column(align=True)
    col.label(text="通知間隔 (すべて0なら確認のたびに通知)")
    row = col.row(align=True)
    row.prop(wm, "gh_updater_months")
    row.prop(wm, "gh_updater_days")
    row.prop(wm, "gh_updater_hours")
    row.prop(wm, "gh_updater_minutes")
    row.prop(wm, "gh_updater_seconds")
    col.prop(wm, "gh_updater_pre")

    last = _settings["last_check"]
    if last:
        box.label(text="最終確認: " + datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M"))

    row = box.row()
    row.enabled = not _state["busy"]
    row.operator("ghupd.check", icon="FILE_REFRESH")

    if _state["releases"]:
        box.prop(wm, "gh_updater_tag", text="バージョン")
        row = box.row()
        row.enabled = not _state["busy"]
        row.operator("ghupd.install", icon="IMPORT")

    if _state["message"]:
        box.label(text=_state["message"])
    if _state["need_restart"]:
        box.label(text="反映するにはBlenderを再起動してください", icon="ERROR")


# ---------------------------------------------------------------- 登録
_classes = (GHUPD_OT_check, GHUPD_OT_install, GHUPD_OT_popup)


def register():
    _load_settings()

    g, s = _accessors("include_pre", bool)
    bpy.types.WindowManager.gh_updater_pre = BoolProperty(
        name="プレリリースを含める", get=g, set=s)
    g, s = _accessors("interval_months", int)
    bpy.types.WindowManager.gh_updater_months = IntProperty(
        name="月", description="更新通知の間隔(月)", min=0, max=120, get=g, set=s)
    g, s = _accessors("interval_days", int)
    bpy.types.WindowManager.gh_updater_days = IntProperty(
        name="日", description="更新通知の間隔(日)", min=0, max=365, get=g, set=s)
    g, s = _accessors("interval_hours", int)
    bpy.types.WindowManager.gh_updater_hours = IntProperty(
        name="時", description="更新通知の間隔(時)", min=0, max=23, get=g, set=s)
    g, s = _accessors("interval_minutes", int)
    bpy.types.WindowManager.gh_updater_minutes = IntProperty(
        name="分", description="更新通知の間隔(分)", min=0, max=59, get=g, set=s)
    g, s = _accessors("interval_seconds", int)
    bpy.types.WindowManager.gh_updater_seconds = IntProperty(
        name="秒", description="更新通知の間隔(秒)", min=0, max=59, get=g, set=s)
    bpy.types.WindowManager.gh_updater_tag = EnumProperty(
        name="Version", items=_enum_cb)

    for c in _classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
    del bpy.types.WindowManager.gh_updater_tag
    del bpy.types.WindowManager.gh_updater_seconds
    del bpy.types.WindowManager.gh_updater_minutes
    del bpy.types.WindowManager.gh_updater_hours
    del bpy.types.WindowManager.gh_updater_days
    del bpy.types.WindowManager.gh_updater_months
    del bpy.types.WindowManager.gh_updater_pre