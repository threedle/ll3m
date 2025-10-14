"""
Configuration loader for LL3M Client.
Loads client configuration from YAML files.
"""

import yaml
import os
from pathlib import Path
from typing import Dict, Any


def load_client_config() -> Dict[str, Any]:
    """Load client configuration from config.yaml file.
    
    Returns:
        Dict containing configuration, with empty dict as fallback.
    """
    # Get the directory where this file is located
    config_dir = Path(__file__).parent
    config_path = config_dir / "config.yaml"
    
    if not config_path.exists():
        print(f"Warning: Configuration file not found: {config_path}")
        return {}
    
    try:
        with open(config_path, 'r') as file:
            config = yaml.safe_load(file)
        
        if not config:
            print("Warning: Configuration file is empty or invalid")
            return {}
        
        return config
        
    except yaml.YAMLError as e:
        print(f"Error parsing YAML configuration: {e}")
        return {}
    except Exception as e:
        print(f"Error loading configuration: {e}")
        return {}


def get_server_config() -> Dict[str, Any]:
    """Get server configuration with environment variable overrides.
    
    Environment variables override YAML config:
    - LL3M_SERVER_URL
    
    Returns:
        Dict with server settings.
    """
    config = load_client_config()
    server_cfg = config.get("server", {})
    
    # Get values from config
    url = server_cfg.get("url", "http://localhost:8080")
    
    # Environment variable overrides
    url = os.environ.get("LL3M_SERVER_URL", url)
    
    return {
        "url": url,
    }


def get_cognito_config() -> Dict[str, Any]:
    """Return client-side Cognito config for PKCE flow."""
    config = load_client_config()
    c = (config.get("cognito") or {})
    return {
        "domain": c.get("domain"),
        "client_id": c.get("client_id"),
        "redirect_uri": c.get("redirect_uri", "http://localhost:8765/callback"),
        "logout_redirect_uri": c.get("logout_redirect_uri", "http://localhost:8765/logout"),
        "scopes": c.get("scopes", ["openid", "email", "phone", "profile"]),
    }




def get_blender_config() -> Dict[str, Any]:
    """Get Blender configuration for code execution.
    
    Uses sensible defaults for internal settings.
    headless_rendering and gpu_rendering are user-configurable.
    
    Returns:
        Dict with Blender settings for execution.
    """
    config = load_client_config()
    blender_cfg = config.get("blender", {})
    
    # User-configurable settings
    headless_rendering = blender_cfg.get("headless_rendering", True)
    gpu_rendering = blender_cfg.get("gpu_rendering", False)
    
    # Internal settings with sensible defaults
    headless_timeout = 300  # 5 minutes
    fallback_to_socket = True  # Always fallback for reliability
    
    # Environment variable overrides (for development/testing)
    headless_rendering = os.environ.get("LL3M_BLENDER_HEADLESS_RENDERING", str(headless_rendering)).lower() == 'true'
    gpu_rendering = os.environ.get("LL3M_BLENDER_GPU_RENDERING", str(gpu_rendering)).lower() == 'true'
    headless_timeout = int(os.environ.get("LL3M_BLENDER_HEADLESS_TIMEOUT", str(headless_timeout)))
    fallback_to_socket = os.environ.get("LL3M_BLENDER_FALLBACK_TO_SOCKET", str(fallback_to_socket)).lower() == 'true'
    
    return {
        "headless_rendering": headless_rendering,
        "gpu_rendering": gpu_rendering,
        "headless_timeout": headless_timeout,
        "fallback_to_socket": fallback_to_socket,
    }


def get_effective_gpu_setting():
    """
    Get the effective GPU rendering setting after checking availability.
    
    Returns:
        dict: {
            'gpu_rendering': bool,  # Whether to use GPU rendering
            'gpu_available': bool,  # Whether GPU is actually available
            'gpu_info': dict        # Detailed GPU information
        }
    """
    from blender.client import BlenderClient
    
    # Get config setting
    blender_config = get_blender_config()
    config_gpu_enabled = blender_config.get("gpu_rendering", False)
    
    # Detect actual GPU availability through Blender addon
    gpu_info = {'has_gpu': False, 'gpu_type': None, 'preferred_engine': 'BLENDER_EEVEE', 'device': 'CPU'}
    gpu_available = False
    
    if config_gpu_enabled:
        try:
            # Create GPU detection code to run in Blender
            gpu_detection_code = '''
import bpy

# Check available render engines
available_engines = bpy.types.RenderSettings.bl_rna.properties['engine'].enum_items.keys()

# Check for GPU devices
gpu_available = False
gpu_type = None

# Check CUDA/OpenCL/Metal devices
if hasattr(bpy.context.preferences, 'addons') and 'cycles' in bpy.context.preferences.addons:
    cycles_prefs = bpy.context.preferences.addons['cycles'].preferences
    if hasattr(cycles_prefs, 'devices'):
        for device in cycles_prefs.devices:
            if device.type in ('CUDA', 'OPENCL', 'OPTIX', 'HIP', 'METAL'):
                gpu_available = True
                gpu_type = device.type
                break

# Determine preferred engine and device
if gpu_available and 'BLENDER_EEVEE' in available_engines:
    preferred_engine = 'BLENDER_EEVEE'
    device = 'GPU'
elif gpu_available and 'BLENDER_EEVEE_NEXT' in available_engines:
    preferred_engine = 'BLENDER_EEVEE_NEXT'
    device = 'GPU'
elif gpu_available and 'CYCLES' in available_engines:
    preferred_engine = 'CYCLES'
    device = 'GPU'
else:
    # Fallback to CPU
    if 'BLENDER_EEVEE' in available_engines:
        preferred_engine = 'BLENDER_EEVEE'
    elif 'BLENDER_EEVEE_NEXT' in available_engines:
        preferred_engine = 'BLENDER_EEVEE_NEXT'
    elif 'CYCLES' in available_engines:
        preferred_engine = 'CYCLES'
    else:
        preferred_engine = list(available_engines)[0]
    device = 'CPU'

# Return result as a string that can be parsed
result = f"GPU_DETECTION_RESULT:{{'has_gpu':{gpu_available},'gpu_type':'{gpu_type}','preferred_engine':'{preferred_engine}','device':'{device}'}}"
print(result)
'''
            
            # Execute GPU detection in Blender
            result = BlenderClient.execute_code(gpu_detection_code)
            
            if result and isinstance(result, dict) and result.get("status") == "success":
                # Parse the nested result structure
                inner_result = result.get("result", {})
                if isinstance(inner_result, dict) and inner_result.get("executed") == True:
                    output = inner_result.get("result", "")
                    if isinstance(output, str) and "GPU_DETECTION_RESULT:" in output:
                        import json
                        try:
                            # Extract the JSON part after GPU_DETECTION_RESULT:
                            json_str = output.split("GPU_DETECTION_RESULT:")[1].strip()
                            # Replace single quotes with double quotes for valid JSON
                            json_str = json_str.replace("'", '"')
                            # Replace Python boolean literals with JSON boolean literals
                            json_str = json_str.replace('True', 'true').replace('False', 'false')
                            gpu_info = json.loads(json_str)
                            gpu_available = gpu_info.get('has_gpu', False)
                            print(f"[Client] GPU detection successful: {gpu_info}")
                        except Exception as e:
                            print(f"[Client] Error parsing GPU detection result: {e}")
                            gpu_available = False
                    else:
                        print(f"[Client] GPU detection result not found in output: {output}")
                        gpu_available = False
                else:
                    print(f"[Client] GPU detection execution failed: {inner_result}")
                    gpu_available = False
            else:
                print(f"[Client] GPU detection failed: {result}")
                gpu_available = False
                
        except Exception as e:
            print(f"[Client] Error during GPU detection: {e}")
            gpu_available = False
    
    # Determine effective setting
    if config_gpu_enabled and gpu_available:
        effective_gpu_rendering = True
        print(f"[Client] GPU rendering enabled: {gpu_info['gpu_type']} GPU detected")
    elif config_gpu_enabled and not gpu_available:
        effective_gpu_rendering = False
        print(f"[Client] GPU rendering requested but no GPU detected, falling back to CPU")
    else:
        effective_gpu_rendering = False
        print(f"[Client] CPU rendering (GPU disabled in config)")
    
    return {
        'gpu_rendering': effective_gpu_rendering,
        'gpu_available': gpu_available,
        'gpu_info': gpu_info
    }


# Load configuration when module is imported
client_config = load_client_config()
