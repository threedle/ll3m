# Blender LL3M Addon
# Minimal socket server for code/scene/object info
import bpy
import math
import socket
import threading
import json
import traceback
import io
from contextlib import redirect_stdout
from bpy.props import IntProperty, BoolProperty

bl_info = {
    "name": "LL3M Blender",
    "author": "Sining Lu",
    "version": (2, 0), # Version Update
    "blender": (4, 4, 0), # Blender 4.4.0
    "location": "View3D > Sidebar > LL3M",
    "description": "Minimal socket server for code/scene/object info",
    "category": "Interface",
}

HOST = 'localhost'
PORT = 8888

class LL3MAgentServer:
    def __init__(self, host=HOST, port=PORT):
        self.host = host
        self.port = port
        self.running = False
        self.socket = None
        self.server_thread = None

    def start(self):
        if self.running:
            print("Server already running")
            return
        self.running = True
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((self.host, self.port))
            self.socket.listen(1)
            self.server_thread = threading.Thread(target=self._server_loop)
            self.server_thread.daemon = True
            self.server_thread.start()
            print(f"LL3MAgentServer started on {self.host}:{self.port}")
        except Exception as e:
            print(f"Failed to start server: {e}")
            self.stop()

    def stop(self):
        self.running = False
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
            self.socket = None
        if self.server_thread:
            try:
                if self.server_thread.is_alive():
                    self.server_thread.join(timeout=1.0)
            except:
                pass
            self.server_thread = None
        print("LL3MAgentServer stopped")

    def _server_loop(self):
        print("Server thread started")
        self.socket.settimeout(1.0)
        while self.running:
            try:
                try:
                    client, address = self.socket.accept()
                    print(f"Connected to client: {address}")
                    client_thread = threading.Thread(target=self._handle_client, args=(client,))
                    client_thread.daemon = True
                    client_thread.start()
                except socket.timeout:
                    continue
                except Exception as e:
                    print(f"Error accepting connection: {e}")
            except Exception as e:
                print(f"Error in server loop: {e}")
                if not self.running:
                    break
        print("Server thread stopped")

    def _handle_client(self, client):
        print("Client handler started")
        buffer = b''
        try:
            while self.running:
                try:
                    data = client.recv(8192)
                    if not data:
                        print("Client disconnected")
                        break
                    buffer += data
                    try:
                        command = json.loads(buffer.decode('utf-8'))
                        buffer = b''
                        def execute_wrapper():
                            try:
                                response = self.execute_command(command)
                                response_json = json.dumps(response)
                                try:
                                    client.sendall(response_json.encode('utf-8'))
                                except:
                                    print("Failed to send response - client disconnected")
                            except Exception as e:
                                print(f"Error executing command: {e}")
                                traceback.print_exc()
                                try:
                                    error_response = {"status": "error", "message": str(e)}
                                    client.sendall(json.dumps(error_response).encode('utf-8'))
                                except:
                                    pass
                            return None
                        bpy.app.timers.register(execute_wrapper, first_interval=0.0)
                    except json.JSONDecodeError:
                        pass
                except Exception as e:
                    print(f"Error receiving data: {e}")
                    break
        except Exception as e:
            print(f"Error in client handler: {e}")
        finally:
            try:
                client.close()
            except:
                pass
            print("Client handler stopped")

    def execute_command(self, command):
        try:
            cmd_type = command.get("type")
            params = command.get("params", {})
            if cmd_type == "get_scene_info":
                return {"status": "success", "result": self.get_scene_info()}
            elif cmd_type == "get_object_info":
                return {"status": "success", "result": self.get_object_info(params.get("name"))}
            elif cmd_type == "execute_code":
                return {"status": "success", "result": self.execute_code(params.get("code"))}
            elif cmd_type == "save_scene_copy":
                return {"status": "success", "result": self.save_scene_copy(params)}
            else:
                return {"status": "error", "message": f"Unknown command type: {cmd_type}"}
        except Exception as e:
            traceback.print_exc()
            return {"status": "error", "message": str(e)}

    def get_scene_info(self):
        scene = bpy.context.scene
        return {
            "name": scene.name,
            "object_count": len(scene.objects),
            "objects": [obj.name for obj in scene.objects]
        }

    def get_object_info(self, name):
        obj = bpy.data.objects.get(name)
        if not obj:
            return {"error": f"Object not found: {name}"}
        return {
            "name": obj.name,
            "type": obj.type,
            "location": list(obj.location),
            "rotation": list(obj.rotation_euler),
            "scale": list(obj.scale)
        }

    def execute_code(self, code):
        namespace = {"bpy": bpy}
        capture_buffer = io.StringIO()
        with redirect_stdout(capture_buffer):
            exec(code, namespace)
        return {"executed": True, "result": capture_buffer.getvalue()}

    def save_scene_copy(self, params):
        """
        Save a copy of the current scene to a .blend file without changing the user's current file binding.
        Optionally pack all external assets into the .blend file for portability.
        Expected params:
          - filepath: target path for the .blend file
          - pack: bool (default True) whether to pack external assets
        """
        filepath = params.get("filepath")
        pack = bool(params.get("pack", True))
        if not filepath:
            return {"saved": False, "message": "Missing 'filepath' parameter"}
        # Ensure directory exists
        import os
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        # Pack assets if requested
        if pack:
            try:
                bpy.ops.file.pack_all()
            except Exception as e:
                print(f"[LL3M Addon] pack_all failed: {e}")
        # Save as copy so current file path is not changed
        try:
            bpy.ops.wm.save_as_mainfile(filepath=filepath, copy=True)
            return {"saved": True, "filepath": filepath}
        except Exception as e:
            return {"saved": False, "message": str(e)}

# --- Blender UI Panel and Operators ---``
class BLENDERCUSTOMAGENT_OT_StartServer(bpy.types.Operator):
    bl_idname = "blendercustomagent.start_server"
    bl_label = "Start LL3M Server"
    bl_description = "Start the Blender LL3M socket server"

    def execute(self, context):
        scene = context.scene
        if not hasattr(bpy.types, "blendercustomagent_server") or not bpy.types.blendercustomagent_server:
            bpy.types.blendercustomagent_server = LL3MAgentServer(port=scene.blendercustomagent_port)
        bpy.types.blendercustomagent_server.start()
        scene.blendercustomagent_server_running = True
        self.report({'INFO'}, "LL3M Server started!")
        return {'FINISHED'}

class BLENDERCUSTOMAGENT_OT_StopServer(bpy.types.Operator):
    bl_idname = "blendercustomagent.stop_server"
    bl_label = "Stop LL3M Server"
    bl_description = "Stop the Blender LL3M socket server"

    def execute(self, context):
        scene = context.scene
        if hasattr(bpy.types, "blendercustomagent_server") and bpy.types.blendercustomagent_server:
            bpy.types.blendercustomagent_server.stop()
            del bpy.types.blendercustomagent_server
        scene.blendercustomagent_server_running = False
        self.report({'INFO'}, "LL3M Server stopped!")
        return {'FINISHED'}

class BLENDERCUSTOMAGENT_PT_Panel(bpy.types.Panel):
    bl_label = "Blender LL3M"
    bl_idname = "BLENDERLL3MAGENT_PT_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'LL3M'

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        layout.prop(scene, "blenderLL3Magent_port")
        if not scene.blendercustomagent_server_running:
            layout.operator("blendercustomagent.start_server", text="Start LL3M Server")
        else:
            layout.operator("blendercustomagent.stop_server", text="Stop LL3M Server")
            layout.label(text=f"Running on port {scene.blendercustomagent_port}")

# --- Registration ---
classes = [
    BLENDERCUSTOMAGENT_OT_StartServer,
    BLENDERCUSTOMAGENT_OT_StopServer,
    BLENDERCUSTOMAGENT_PT_Panel,
]

def register():
    bpy.types.Scene.blendercustomagent_port = IntProperty(
        name="Port",
        description="Port for the LL3M server",
        default=8888,
        min=1024,
        max=65535
    )
    bpy.types.Scene.blendercustomagent_server_running = BoolProperty(
        name="Server Running",
        default=False
    )
    for cls in classes:
        bpy.utils.register_class(cls)
    print("Blender LL3M addon registered")

def unregister():
    if hasattr(bpy.types, "blendercustomagent_server") and bpy.types.blendercustomagent_server:
        bpy.types.blendercustomagent_server.stop()
        del bpy.types.blendercustomagent_server
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.blendercustomagent_port
    del bpy.types.Scene.blendercustomagent_server_running
    print("Blender LL3M addon unregistered")

if __name__ == "__main__":
    register() 