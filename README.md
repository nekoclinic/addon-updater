# updater.py

GitHub Releases から Blender アドオンを更新・ダウングレードするモジュールです。アドオンのパッケージ内に置いて使います。

## 機能

- 任意のバージョンを選んでインストール(ダウングレードも可)
- 設定した間隔(月・日・時・分・秒)で更新を確認し、新しいバージョンがあればカーソル位置にポップアップを表示
- プレリリースを含めるかを切り替え(初期値: 含める)
- インストール失敗時は元のバージョンに自動で戻す
- インストール後に再起動ポップアップを表示

## 動作条件

- Blender 5.2 (Python 3.10 以上)
- アドオンの `__init__.py` に `bl_info` があること
- Extensions 形式 (`blender_manifest.toml`) のアドオンは非対応

## 組み込み方

1. `updater.py` をアドオンのパッケージ内に置きます。
2. `__init__.py` から登録し、Preferences の `draw` で呼び出します。

```python
import bpy
from . import updater

bl_info = {
    "name": "My Addon",
    "version": (1, 0, 0),
    "blender": (5, 2, 0),
}


class MyPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    def draw(self, context):
        updater.draw(self.layout, context)


def register():
    updater.register(repo="owner/name")
    bpy.utils.register_class(MyPreferences)


def unregister():
    bpy.utils.unregister_class(MyPreferences)
    updater.unregister()
```

`repo` を省略すると、`updater.py` 先頭の `GITHUB_REPO` を使います。

複数のアドオンに入れても、`bl_idname` とプロパティ名はパッケージ名から作られるので衝突しません。ただしパッケージ名が数字で始まると登録できません。

## リリースの作り方

- タグは `v1.1.0` または `1.1.0` の形式にします。バージョンとして読めないタグは無視されます。
- ZIP をアセットとして添付します。添付がなければ、タグのソースアーカイブを使います。
- ZIP の中に、`bl_info` を含む `__init__.py` があるフォルダが必要です。
- 更新の判定は `bl_info["version"]` とタグの比較です。リリースのたびに `bl_info` のバージョンも上げてください。上げ忘れると、更新通知が出続けます。
- ドラフトのリリースは無視されます。

## 使い方

Preferences の画面に次の項目が出ます。

| 項目 | 内容 |
| --- | --- |
| Include Pre-releases | プレリリースを一覧に含める |
| Months / Days / Hours / Minutes / Seconds | 更新を確認する間隔 |
| Check | 今すぐリリース一覧を取得する |
| バージョン選択 + Install | 選んだバージョンをインストールする |

起動時に、前回の確認から設定した間隔が経っていれば自動で確認します。新しいバージョンがあればポップアップが出るので、`Install` を押します。インストールが終わると再起動を促すポップアップが出ます。

## 設定ファイル

設定と最終確認時刻は、Blender のユーザー設定フォルダ (`CONFIG`) に `{パッケージ名}_updater.json` として保存されます。

## 注意

- ポップアップは開いたウィンドウの外には出られません。
- バックグラウンドモード (`bpy.app.background`) では、起動時の自動確認をしません。
- 失敗したときのメッセージは内部に保持しますが、画面には出しません。原因を調べるときは、System Console のエラー出力を確認してください。