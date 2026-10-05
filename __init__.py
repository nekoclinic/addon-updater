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
    "name": "Addon Updater",
    "author": "wawawa",
    "description": "",
    "blender": (2, 80, 0),
    "version": (0, 0, 4),
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

def register():
    updater.register()
    bpy.utils.register_class(BUPreferences)

def unregister():
    updater.unregister()
    bpy.utils.unregister_class(BUPreferences)
