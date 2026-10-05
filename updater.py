"""
GitHub Releases ベースのアドオン更新/ダウングレードモジュール。

使い方:
    1. このファイルをアドオンのルート (__init__.py と同じ階層) に置く
    2. GITHUB_REPO を書き換える
    3. __init__.py の register()/unregister() から updater.register()/unregister() を呼ぶ
    4. AddonPreferences.draw() 内で updater.draw(self.layout, context) を呼ぶ
"""

import json
import os
import re
import shutil
import sys
import tempfile
import threading
import urllib.request
import zipfile

import bpy
from bpy.props import BoolProperty, EnumProperty

# ---------------------------------------------------------------- 設定
GITHUB_REPO = "nekoclinic/blender-updater"      # 例: "hoshinome/blender-retouch"
ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
PKG = __package__.split(".")[0] if __package__ else os.path.basename(ADDON_DIR)
ENV_FILE = os.path.join(ADDON_DIR, ".env")   # GITHUB_TOKEN=github_pat_xxx (private repo の場合のみ)


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

# ---------------------------------------------------------------- 状態
_state = {
    "busy": False,
    "message": "",
    "releases": [],      # [{tag, version, url, name, prerelease}]
    "need_restart": False,
}
_enum_items = [("NONE", "(未取得)", "")]


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


def _poll():
    if _state["busy"]:
        return 0.3
    _rebuild_enum()
    _redraw()
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
    except Exception as e:
        _state["message"] = f"取得失敗: {e}"
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


# ---------------------------------------------------------------- オペレーター
class GHUPD_OT_check(bpy.types.Operator):
    bl_idname = "ghupd.check"
    bl_label = "リリースを取得"

    def execute(self, context):
        if _state["busy"]:
            return {"CANCELLED"}
        _state["busy"] = True
        _state["message"] = "取得中..."
        threading.Thread(
            target=_fetch_worker,
            args=(context.window_manager.gh_updater_pre,),
            daemon=True,
        ).start()
        bpy.app.timers.register(_poll, first_interval=0.3)
        return {"FINISHED"}


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
        if not rel or _state["busy"]:
            return {"CANCELLED"}
        _state["busy"] = True
        _state["message"] = f'{rel["tag"]} をダウンロード中...'
        threading.Thread(target=_install_worker, args=(rel,), daemon=True).start()
        bpy.app.timers.register(_poll, first_interval=0.3)
        return {"FINISHED"}


# ---------------------------------------------------------------- UI
def draw(layout, context):
    wm = context.window_manager
    box = layout.box()
    box.label(text=f"アップデーター  (現在: v{_fmt(current_version())})", icon="URL")

    row = box.row()
    row.prop(wm, "gh_updater_pre")
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
_classes = (GHUPD_OT_check, GHUPD_OT_install)


def register():
    bpy.types.WindowManager.gh_updater_tag = EnumProperty(
        name="Version", items=_enum_cb)
    bpy.types.WindowManager.gh_updater_pre = BoolProperty(
        name="プレリリースを含める", default=False)
    for c in _classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
    del bpy.types.WindowManager.gh_updater_pre
    del bpy.types.WindowManager.gh_updater_tag