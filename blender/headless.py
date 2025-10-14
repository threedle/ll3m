"""
Headless Blender execution module for LL3M Client.

This module provides functionality to execute Blender code in headless mode
using subprocess, which is useful for rendering operations on less powerful computers.
"""

import subprocess
import tempfile
import os
import sys
import time
import shutil
from pathlib import Path
from typing import Dict, Any, Optional, Tuple


def find_blender_executable() -> Optional[str]:
    """
    Find the Blender executable on the system.
    
    Returns:
        Path to Blender executable, or None if not found.
    """
    # Common Blender installation paths
    possible_paths = []
    
    # Generate version paths from 3.0 to 4.9
    versions = []
    # Major versions 3.x
    for minor in range(0, 10):  # 3.0 to 3.9
        versions.append(f"3.{minor}")
    # Major versions 4.x
    for minor in range(0, 10):  # 4.0 to 4.9
        versions.append(f"4.{minor}")
    
    # Windows paths
    if sys.platform == "win32":
        # Add generic Blender path first
        possible_paths.append(r"C:\Program Files\Blender Foundation\Blender\blender.exe")
        # Add versioned paths
        for version in versions:
            possible_paths.append(fr"C:\Program Files\Blender Foundation\Blender {version}\blender.exe")
    # macOS paths
    elif sys.platform == "darwin":
        # Add generic Blender path first
        possible_paths.append("/Applications/Blender.app/Contents/MacOS/Blender")
        # Add versioned paths
        for version in versions:
            possible_paths.append(f"/Applications/Blender {version}/Blender.app/Contents/MacOS/Blender")
    # Linux paths
    else:
        # Add common Linux paths first
        possible_paths.extend([
            "/usr/bin/blender",
            "/usr/local/bin/blender",
            "/snap/bin/blender",
            "/opt/blender/blender",
        ])
        # Add versioned paths
        for version in versions:
            possible_paths.append(f"/opt/blender-{version}/blender")
    
    # Check if blender is in PATH
    blender_path = shutil.which("blender")
    if blender_path:
        possible_paths.insert(0, blender_path)
    
    # Check each possible path
    for path in possible_paths:
        if os.path.exists(path) and os.access(path, os.X_OK):
            return path
    
    return None


def is_rendering_code(code: str) -> bool:
    """
    Detect if the provided code is likely to be rendering code.
    
    Args:
        code: Python code string to analyze
        
    Returns:
        True if the code appears to be rendering code, False otherwise
    """
    if not code:
        return False
    
    code_lower = code.lower()
    
    # Check for common rendering patterns
    rendering_patterns = [
        "render_scene(",
        "bpy.ops.render.render",
        "bpy.context.scene.render.filepath",
        "bpy.context.scene.render.filepath =",
        "bpy.ops.render.render(",
        "render.render(",
        "bpy.context.scene.render.engine",
        "bpy.context.scene.render.resolution",
    ]
    
    return any(pattern in code_lower for pattern in rendering_patterns)


def execute_headless_blender(code: str, timeout: int = 300, blend_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Execute Blender code in headless mode using subprocess.
    
    Args:
        code: Python code to execute in Blender
        timeout: Timeout in seconds for the execution
        
    Returns:
        Dictionary with execution result, similar to socket-based execution
    """
    blender_path = find_blender_executable()
    if not blender_path:
        return {
            "status": "error",
            "message": "Blender executable not found. Please ensure Blender is installed and accessible.",
            "result": None
        }
    
    # Create a temporary Python script file
    temp_script = None
    try:
        # Create temporary script file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as f:
            f.write(code)
            temp_script = f.name
        
        # Prepare Blender command; load .blend if provided to reproduce current scene
        cmd = [blender_path, "--background"]
        if blend_path:
            cmd.extend(["-b", blend_path])
        cmd.extend(["--python", temp_script, "--"])
        
        print(f"[Headless] Executing Blender code in headless mode...")
        print(f"[Headless] Command: {' '.join(cmd[:3])} <script>")
        
        # Execute the command
        start_time = time.time()
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=os.getcwd()
            )
            execution_time = time.time() - start_time
            
            # Parse the result
            if result.returncode == 0:
                # Success
                output = result.stdout.strip()
                return {
                    "status": "success",
                    "message": f"Headless execution completed in {execution_time:.2f}s",
                    "result": {
                        "executed": True,
                        "result": output,
                        "execution_time": execution_time,
                        "method": "headless"
                    }
                }
            else:
                # Error
                error_output = result.stderr.strip() if result.stderr else "Unknown error"
                return {
                    "status": "error",
                    "message": f"Headless execution failed (exit code {result.returncode}): {error_output}",
                    "result": {
                        "executed": False,
                        "result": error_output,
                        "execution_time": execution_time,
                        "method": "headless"
                    }
                }
                
        except subprocess.TimeoutExpired:
            execution_time = time.time() - start_time
            return {
                "status": "error",
                "message": f"Headless execution timed out after {timeout}s",
                "result": {
                    "executed": False,
                    "result": f"Timeout after {timeout}s",
                    "execution_time": execution_time,
                    "method": "headless"
                }
            }
        except Exception as e:
            execution_time = time.time() - start_time
            return {
                "status": "error",
                "message": f"Headless execution failed: {str(e)}",
                "result": {
                    "executed": False,
                    "result": str(e),
                    "execution_time": execution_time,
                    "method": "headless"
                }
            }
    
    finally:
        # Clean up temporary script file
        if temp_script and os.path.exists(temp_script):
            try:
                os.unlink(temp_script)
            except Exception as e:
                print(f"[Headless] Warning: Could not clean up temporary script: {e}")


def should_use_headless(code: str, expects_render: bool, headless_enabled: bool) -> bool:
    """
    Determine if headless execution should be used for the given code.
    
    Args:
        code: Python code to execute
        expects_render: Flag indicating if the server expects rendering
        headless_enabled: Configuration flag for headless rendering
        
    Returns:
        True if headless execution should be used, False otherwise
    """
    if not headless_enabled:
        return False
    
    # Use headless for rendering operations
    if expects_render or is_rendering_code(code):
        return True
    
    return False
