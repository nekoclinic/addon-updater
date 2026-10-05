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
import urllib.error
import urllib.parse
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
    "show_restart_prompt": False,
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
    h = {
        "User-Agent": f"{PKG}-updater",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = _token()
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _format_error(error):
    if isinstance(error, PermissionError):
        path = error.filename or ""
        message = f"{error}"
        if getattr(error, "winerror", None) == 5 or error.errno == 5:
            message += (
                " | アドオンフォルダへのアクセスが拒否されました。"
                "Blenderを管理者として実行するか、書き込み可能なユーザー領域へ"
                "アドオンをインストールしてください"
            )
        if path:
            message += f" [対象: {path}]"
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
        if (
            error.code == 404
            and "api.github.com/repos/" in error.url
            and GITHUB_REPO.lower() in error.url.lower()
        ):
            if _token():
                message += (
                    " | GITHUB_TOKENは設定済みですが、GitHubがリポジトリを"
                    "参照できていません。Fine-grained tokenなら対象リポジトリを"
                    "Repository accessに追加し、Contents: Readを許可してください。"
                    "Classic tokenならrepoスコープが必要です。期限切れや"
                    "Organizationの承認待ちも確認してください"
                )
            else:
                message += (
                    " | 非公開リポジトリを読むGITHUB_TOKENがありません。"
                    "アドオン直下の.envに設定してください"
                )
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
        items.append((r["tag"], f'{r["tag"]}{pre}', r["name"] or ""))
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
    if _state["show_restart_prompt"]:
        _state["show_restart_prompt"] = False
        wm = bpy.context.window_manager
        if wm.windows:
            try:
                with bpy.context.temp_override(window=wm.windows[0]):
                    bpy.ops.ghupd.restart_prompt("INVOKE_DEFAULT")
            except Exception as e:
                _state["message"] = f"終了確認ポップアップ失敗: {e}"
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
            zip_asset = next(
                (
                    asset for asset in rel.get("assets", [])
                    if asset["name"].lower().endswith(".zip")
                ),
                None,
            )
            browser_url = (
                zip_asset.get("browser_download_url") if zip_asset else None
            )
            asset_api_url = zip_asset.get("url") if zip_asset else None
            download_url = (
                (asset_api_url if _token() else browser_url)
                or browser_url
                or asset_api_url
            ) or (
                f"https://github.com/{GITHUB_REPO}/archive/refs/tags/"
                f"{urllib.parse.quote(rel['tag_name'], safe='')}.zip"
            )
            if not download_url:
                continue
            releases.append({
                "tag": rel["tag_name"],
                "version": ver,
                "url": download_url,
                "browser_url": browser_url,
                "name": rel.get("name", ""),
                "prerelease": bool(rel.get("prerelease")),
            })
        releases.sort(key=lambda r: r["version"], reverse=True)
        _state["releases"] = releases
        _state["message"] = f"{len(releases)} 件のリリースを取得"
        _settings["last_check"] = time.time()
        _save_settings()
    except Exception as e:
        _state["message"] = f"取得失敗: {_format_error(e)}"
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
            ignore=shutil.ignore_patterns(".env", "__pycache__"),
        )
        env_path = os.path.join(ADDON_DIR, ".env")
        if os.path.isfile(env_path):
            shutil.copy2(env_path, os.path.join(staged_addon, ".env"))

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
                    f"新バージョンの配置に失敗し、旧版の復元にも失敗しました。"
                    f"旧版: {backup} / 配置エラー: {install_error} / "
                    f"復元エラー: {rollback_error}"
                ) from rollback_error
            raise
    finally:
        if moved_old_addon and not installed_new_addon:
            if not os.path.exists(ADDON_DIR):
                try:
                    os.replace(backup, ADDON_DIR)
                except OSError as rollback_error:
                    preserve_backup = True
                    cleanup_warning = (
                        f"旧版を自動復元できませんでした。バックアップ: {backup} / "
                        f"{_format_error(rollback_error)}"
                    )
            else:
                preserve_backup = True
                cleanup_warning = (
                    f"旧版バックアップを保持しました: {backup}"
                )
        if not preserve_backup:
            try:
                shutil.rmtree(staging_root)
            except OSError as cleanup_error:
                if installed_new_addon:
                    cleanup_warning = (
                        f"旧版バックアップを削除できませんでした: "
                        f"{staging_root} / {_format_error(cleanup_error)}"
                    )
                elif not cleanup_warning:
                    cleanup_warning = (
                        f"一時ファイルを削除できませんでした: "
                        f"{staging_root} / {_format_error(cleanup_error)}"
                    )
    return cleanup_warning


def _download_release(release, zip_path):
    download_url = release["url"]
    headers = {"User-Agent": f"{PKG}-updater"}
    if _token():
        headers["Authorization"] = _headers()["Authorization"]
    if "api.github.com" in download_url:
        headers["Accept"] = "application/octet-stream"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    req = urllib.request.Request(download_url, headers=headers)
    try:
        response = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        fallback_url = release.get("browser_url")
        if e.code != 415 or not fallback_url or fallback_url == download_url:
            raise
        e.close()
        response = urllib.request.urlopen(
            urllib.request.Request(
                fallback_url, headers={"User-Agent": f"{PKG}-updater"}
            ),
            timeout=60,
        )
    with response, open(zip_path, "wb") as f:
        shutil.copyfileobj(response, f)


def _install_worker(release):
    tmp = tempfile.mkdtemp(prefix="gh_updater_")
    try:
        # ダウンロード
        zip_path = os.path.join(tmp, "pkg.zip")
        _download_release(release, zip_path)

        # 展開
        ext = os.path.join(tmp, "extract")
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(ext)
        src = _find_addon_root(ext)
        if not src:
            raise RuntimeError("zip内にアドオン(__init__.py + bl_info)が見つかりません")

        cleanup_warning = _replace_addon_tree(src)

        _state["need_restart"] = True
        _state["show_restart_prompt"] = True
        _state["message"] = f'{release["tag"]} をインストールしました'
        if cleanup_warning:
            _state["message"] += f" ({cleanup_warning})"
    except Exception as e:
        _state["message"] = f"インストール失敗: {_format_error(e)}"
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
    """更新があるときに出すポップアップ"""
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
        col.label(text=f'利用可能なバージョン: {rel["tag"]}{pre}')
        col.label(text=f"現在のバージョン: v{_fmt(current_version())}")
        col.label(text="「アップデート」で今すぐインストールします")

    def execute(self, context):
        rel = _latest_newer()
        if not rel:
            return {"CANCELLED"}
        return {"FINISHED"} if _start_install(rel) else {"CANCELLED"}


class GHUPD_OT_restart_prompt(bpy.types.Operator):
    bl_idname = "ghupd.restart_prompt"
    bl_label = "アップデートを適用"
    bl_options = {"INTERNAL"}

    def invoke(self, context, event):
        try:
            return context.window_manager.invoke_props_dialog(
                self,
                width=360,
                title="インストール完了",
                confirm_text="Blenderを終了",
            )
        except TypeError:
            return context.window_manager.invoke_props_dialog(self, width=360)

    def draw(self, context):
        self.layout.label(text="アップデートを適用するにはBlenderの再起動が必要です。")
        self.layout.label(text="作業を保存してからBlenderを終了してください。")

    def execute(self, context):
        bpy.ops.wm.quit_blender()
        return {"FINISHED"}


# ---------------------------------------------------------------- UI
def draw(layout, context):
    wm = context.window_manager
    box = layout.box()
    box.label(text="Blender Updater", icon="URL")
    box.label(text=f"現在のバージョン: v{_fmt(current_version())}")

    settings_box = box.box()
    settings_box.label(text="設定", icon="PREFERENCES")
    settings_box.prop(wm, "gh_updater_pre")
    settings_box.label(text="通知間隔 (すべて0の場合は毎回通知)")
    grid = settings_box.grid_flow(
        row_major=True, columns=3, even_columns=True, even_rows=True
    )
    grid.prop(wm, "gh_updater_months")
    grid.prop(wm, "gh_updater_days")
    grid.prop(wm, "gh_updater_hours")
    grid.prop(wm, "gh_updater_minutes")
    grid.prop(wm, "gh_updater_seconds")

    last = _settings["last_check"]
    if last:
        checked_at = datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M:%S")
        box.label(text=f"最終確認: {checked_at}")

    row = box.row()
    row.enabled = not _state["busy"]
    row.operator("ghupd.check", text="更新を確認", icon="FILE_REFRESH")

    if _state["releases"]:
        version_box = box.box()
        version_box.label(text="インストールするバージョン")
        version_box.prop(wm, "gh_updater_tag", text="")
        row = box.row()
        row.enabled = not _state["busy"]
        row.operator(
            "ghupd.install",
            text="選択したバージョンをインストール",
            icon="IMPORT",
        )

    if _state["message"]:
        box.label(text=_state["message"])
    if _state["need_restart"]:
        box.label(text="変更の反映にはBlenderの再起動が必要です", icon="ERROR")


# ---------------------------------------------------------------- 登録
_classes = (
    GHUPD_OT_check,
    GHUPD_OT_install,
    GHUPD_OT_popup,
    GHUPD_OT_restart_prompt,
)


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