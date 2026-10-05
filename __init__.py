# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTIBILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

bl_info = {
    "name": "Blender Updater",
    "author": "wawawa",
    "description": "",
    "blender": (2, 80, 0),
    "version": (0, 0, 3),
    "location": "",
    "warning": "",
    "category": "Generic",
}

import bpy
from . import updater

class BUPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__.split(".")[0]   # アドオンのルートパッケージ名

    def draw(self, context):
        updater.draw(self.layout, context)

class BU_PT_main(bpy.types.Panel):
    bl_idname      = "BU_PT_main"
    bl_label       = "blender updater"
    bl_category    = "blender updater"
    bl_space_type  = "VIEW_3D"
    bl_region_type = "UI"
    bl_context     = ""
    bl_order       = 0

    def draw(self, context):
        layout = self.layout
        layout.label(text="blender updater")
        layout.label(text=f"Current version: {'.'.join(map(str, updater.current_version()))}")

def register():
    updater.register()
    bpy.utils.register_class(BUPreferences)
    bpy.utils.register_class(BU_PT_main)

def unregister():
    updater.unregister()
    bpy.utils.unregister_class(BUPreferences)
    bpy.utils.unregister_class(BU_PT_main)
