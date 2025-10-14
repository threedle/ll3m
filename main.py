"""
LL3M Client - Standalone client for LL3M Cloud service.

This is the main entry point for the LL3M client application.
It connects to the LL3M cloud server and handles Blender code execution locally.
"""

import json
import datetime
import re
import time
import os
import sys
import requests
import argparse
import mimetypes

# Add current directory to Python path for local imports
CLIENT_ROOT = os.path.abspath(os.path.dirname(__file__))
if CLIENT_ROOT not in sys.path:
    sys.path.insert(0, CLIENT_ROOT)

from blender.client import BlenderClient
from utils.timer import PhaseTimer
from utils.signals import setup_signal_handlers, set_current_session
from config.loader import get_server_config, get_blender_config, load_client_config
try:
    from auth.token_store import get_auth_headers
    from auth.login import login_via_pkce, logout_local
except Exception:
    def get_auth_headers():
        return {}
    def login_via_pkce():
        raise SystemExit("Login module unavailable")
    def logout_local():
        pass

RENDER_PREFIX_KEYS = {"render", "render_verify"}


def normalize_url(base_url: str, path: str) -> str:
    """Normalize URL to prevent double slashes."""
    base = base_url.rstrip('/')
    path = path.lstrip('/')
    return f"{base}/{path}"


def check_terms_status(server_url: str) -> dict | None:
    """Check current terms and conditions status for the authenticated user."""
    try:
        headers = {**get_auth_headers()}
        if not headers.get("Authorization"):
            return None
        
        r = requests.get(normalize_url(server_url, "terms/status"), headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.HTTPError as e:
        resp = getattr(e, "response", None)
        if resp is None:
            return None
        if resp.status_code == 401:
            # Not logged in yet
            return None
        if resp.status_code == 403:
            # Terms not accepted
            try:
                detail = resp.json()
                return {"accepted": False, "terms_url": detail.get("terms_url", "")}
            except Exception:
                return {"accepted": False}
        # Other errors: return None, caller will continue
        return None
    except Exception:
        return None


def check_rate_limit_status(server_url: str) -> dict | None:
    """Check current rate limit status for the authenticated user."""
    try:
        headers = {**get_auth_headers()}
        if not headers.get("Authorization"):
            return None
        
        r = requests.get(f"{server_url}/rate-limit/status", headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.HTTPError as e:
        resp = getattr(e, "response", None)
        if resp is None:
            return None
        if resp.status_code == 401:
            # Not logged in yet
            return None
        if resp.status_code == 429:
            # Over the limit — normalize payload to the same shape the caller expects
            try:
                detail = resp.json()
                payload = detail.get("detail") if isinstance(detail, dict) and "detail" in detail else detail
                info = (
                    payload.get("rate_limit")
                    if isinstance(payload, dict)
                    else None
                ) or {
                    "remaining": (payload or {}).get("remaining", 0),
                    "limit": (payload or {}).get("limit", 0),
                    "reset_time": (payload or {}).get("reset_time"),
                    "is_admin": (payload or {}).get("is_admin", False),
                }
                return {"rate_limit": info, "over_limit": True}
            except Exception:
                return {"rate_limit": {"remaining": 0, "limit": 0, "reset_time": "Unknown", "is_admin": False}, "over_limit": True}
        # Other errors: return None, caller will continue
        return None
    except Exception:
        return None


def check_blender_addon_connection() -> bool:
    """Check if Blender addon is running and accessible."""
    try:
        from blender.client import BlenderClient
        # Try a simple connection test
        result = BlenderClient.execute_code("import bpy; print('Blender addon is running')", expects_render=False)
        return result is not None and not isinstance(result, dict) or result.get("status") != "error"
    except Exception as e:
        print(f"[Client] Blender addon check failed: {e}")
        return False


def start_run(server_url: str, text: str | None = None, image_path: str | None = None, session_id: str | None = None, refinement_prompt: str | None = None) -> str:
    """Start a new run on the server and return the session ID."""
    def _auth_hint_from_http_error(err: Exception) -> None:
        try:
            import requests
            if isinstance(err, requests.exceptions.HTTPError):
                resp = err.response
                if resp is not None and resp.status_code in (401, 403):
                    print("[Client] Authentication required. Please login first:")
                    print("         python main.py --login")
                elif resp is not None and resp.status_code == 429:
                    # Rate limit exceeded
                    try:
                        detail = resp.json()
                        # Support both server detail shapes
                        info = detail.get("rate_limit") or detail
                        remaining = info.get("remaining", 0)
                        limit = info.get("limit", 0)
                        reset_time = info.get("reset_time") or detail.get("reset_time")
                        if not reset_time:
                            # Fallback: compute next midnight UTC client-side
                            now = datetime.datetime.now(datetime.timezone.utc)
                            reset_time = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
                        print("[Client] ----------------------------------------------")
                        print("[Client] [WARN] Daily rate limit reached")
                        if isinstance(limit, int) and isinstance(remaining, int) and limit >= 0:
                            used = limit - remaining
                            print(f"[Client] Usage today: {used}/{limit}")
                        if reset_time:
                            print(f"[Client] Resets at: {reset_time}")
                        print("[Client] Please try again tomorrow or contact an admin.")
                        print("[Client] ----------------------------------------------")
                    except Exception:
                        print("[Client] [WARN] Daily rate limit reached. Please try again later.")
        except Exception:
            pass
    # Load client render settings (num_images + gpu_rendering)
    try:
        cfg = load_client_config()
        render_cfg = (cfg.get("render") or {})
        
        # Validate num_images: must be integer between 1-10, default to 5 if invalid
        try:
            num_images_raw = render_cfg.get("num_images", 5)
            if isinstance(num_images_raw, float):
                num_images = 5  # Default to 5 if float is provided
            else:
                num_images = int(num_images_raw)
                if not (1 <= num_images <= 10):
                    num_images = 5  # Default to 5 if outside range
        except (ValueError, TypeError):
            num_images = 5  # Default to 5 if conversion fails
        
        # Get effective GPU setting (with availability check)
        from config.loader import get_effective_gpu_setting
        gpu_settings = get_effective_gpu_setting()
        effective_gpu_rendering = gpu_settings['gpu_rendering']
        
    except Exception as e:
        print(f"[Client] Error loading config: {e}")
        num_images = 5
        effective_gpu_rendering = False

    if session_id and refinement_prompt:
        # Session refinement mode
        print(f"[Client] Starting session refinement: {session_id}")
        try:
            # Get resolution_scale from config
            try:
                cfg = load_client_config()
                render_cfg = (cfg.get("render") or {})
                resolution_scale = render_cfg.get("resolution_scale", 1.0)
            except Exception:
                resolution_scale = 1.0
            
            data = {
                'session_id': session_id,
                'refinement_prompt': refinement_prompt,
                'render': json.dumps({
                    'num_images': num_images,
                    'gpu_rendering': effective_gpu_rendering,
                    'resolution_scale': resolution_scale
                })
            }
            headers = {**get_auth_headers()}
            r = requests.post(f"{server_url}/runs", data=data, headers=headers, timeout=30)
            r.raise_for_status()
            return r.json()["session_id"]
        except requests.exceptions.HTTPError as e:
            # Handle specific HTTP errors
            try:
                error_detail = e.response.json()
                if isinstance(error_detail, dict):
                    error_message = error_detail.get('detail', str(error_detail))
                    print(f"[Client] Session refinement failed: {error_message}")
                else:
                    print(f"[Client] Session refinement failed: {error_detail}")
            except:
                print(f"[Client] Session refinement failed: {e}")
            _auth_hint_from_http_error(e)
            raise
        except Exception as e:
            print(f"[Client] Failed to start session refinement: {e}")
            _auth_hint_from_http_error(e)
            raise
    elif image_path:
        if not os.path.exists(image_path):
            raise FileNotFoundError(f"Image file not found: {image_path}")
        if not os.access(image_path, os.R_OK):
            raise PermissionError(f"Cannot read image file: {image_path}")
        
        # Upload image via multipart form data
        print(f"[Client] Uploading image: {image_path}")
        print(f"[Client] Note: Server will perform comprehensive security validation")
        try:
            with open(image_path, 'rb') as f:
                guessed_type, _ = mimetypes.guess_type(image_path)
                content_type = guessed_type if guessed_type and guessed_type.startswith('image/') else 'application/octet-stream'
                files = {'image': (os.path.basename(image_path), f, content_type)}
                # Get resolution_scale from config (server will validate)
                try:
                    cfg = load_client_config()
                    render_cfg = (cfg.get("render") or {})
                    resolution_scale = render_cfg.get("resolution_scale", 1.0)
                except Exception:
                    resolution_scale = 1.0
                
                data = {
                    'text': text, 
                    'render': json.dumps({
                        'num_images': num_images,
                        'gpu_rendering': effective_gpu_rendering,
                        'resolution_scale': resolution_scale
                    })
                }
                headers = {**get_auth_headers()}
                r = requests.post(f"{server_url}/runs", files=files, data=data, headers=headers, timeout=30)
            try:
                r.raise_for_status()
            except requests.exceptions.HTTPError as e:
                # Attempt to surface structured error from server
                try:
                    detail = r.json()
                    # Support both plain string and structured error payloads
                    if isinstance(detail, dict):
                        payload = detail.get('detail', detail)
                        if isinstance(payload, dict):
                            code = payload.get('error') or payload.get('code')
                            message = payload.get('message') or str(payload)
                            print(f"[Client] Upload rejected: {message}" + (f" (code={code})" if code else ""))
                            
                            # Handle security validation errors
                            extra = payload.get('details')
                            if isinstance(extra, dict):
                                threats = extra.get('threats', [])
                                if threats:
                                    print(f"[Client] Security threats detected: {', '.join(threats)}")
                                
                                accepted = extra.get('accepted_formats')
                                if accepted:
                                    print(f"[Client] Accepted formats: {', '.join(accepted)}")
                                
                                validation_errors = extra.get('validation_errors', [])
                                if validation_errors:
                                    print(f"[Client] Validation errors: {', '.join(validation_errors)}")
                        else:
                            print(f"[Client] Upload rejected: {payload}")
                    else:
                        print(f"[Client] Upload rejected: {detail}")
                except Exception:
                    pass
                raise
            print(f"[Client] Image uploaded successfully")
        except Exception as e:
            print(f"[Client] Failed to upload image: {e}")
            _auth_hint_from_http_error(e)
            raise
    else:
        # Use existing JSON payload for text-only requests
        # Get resolution_scale from config for server validation
        try:
            cfg = load_client_config()
            render_cfg = (cfg.get("render") or {})
            resolution_scale = render_cfg.get("resolution_scale", 1.0)
            # Validate client-side as well
            if not isinstance(resolution_scale, (int, float)) or not (0 <= resolution_scale <= 1):
                resolution_scale = 1.0
        except Exception:
            resolution_scale = 1.0
            
        payload = {
            "text": text, 
            "image_path": None, 
            "render": {
                "num_images": num_images,
                "gpu_rendering": effective_gpu_rendering,
                "resolution_scale": resolution_scale
            }
        }
        headers = {**get_auth_headers()}
        try:
            r = requests.post(f"{server_url}/runs", json=payload, headers=headers, timeout=10)
            r.raise_for_status()
        except Exception as e:
            print(f"[Client] Failed to start session: {e}")
            _auth_hint_from_http_error(e)
            raise
    
    return r.json()["session_id"]


def poll_events(session_id: str, server_url: str):
    """Main event polling loop."""
    set_current_session(session_id)
    
    # Set up signal handlers for graceful shutdown
    setup_signal_handlers(server_url, CLIENT_ROOT)
    
    current_phase = None
    phase_counters: dict[str, int] = {}
    # Determine per-session log folder: ./log/run_<session_id>
    session_log_folder = os.path.join(CLIENT_ROOT, "log", f"run_{session_id}")
    os.makedirs(session_log_folder, exist_ok=True)
    phase_timer = PhaseTimer()
    last_seq = 0
    last_event_time = time.time()
    warn_after_s = 60  # warn if no events for 60s
    check_status_after_s = 180  # check server status after 180s of silence
    
    while True:
        try:
            headers = {**get_auth_headers()}
            r = requests.get(f"{server_url}/runs/{session_id}/events", params={"after": last_seq}, headers=headers, timeout=30)
            r.raise_for_status()
            events = r.json()
        except Exception as e:
            error_msg = str(e)
            if "502 Server Error: Bad Gateway" in error_msg:
                print(f"Poll error: {e}")
                print("💡 Note: This is a temporary network issue and won't affect your run. The client will automatically retry...")
            elif any(tok in error_msg for tok in ("503", "529", "Service Unavailable", "overload", "Overloaded")):
                print(f"Poll error: {e}")
                print("💡 Server appears overloaded/unavailable. The client will retry automatically...")
            else:
                print(f"Poll error: {e}")
            time.sleep(2)
            continue

        # Silence awareness and stall detector
        now = time.time()
        if not events:
            # Emit a soft warning after warn_after_s of silence
            if now - last_event_time > warn_after_s and int(now - last_event_time) % 10 == 0:
                elapsed = int(now - last_event_time)
                # Silent waiting - no user message
            # After prolonged silence, query status endpoint once per 30s window
            if now - last_event_time > check_status_after_s and int(now) % 30 == 0:
                try:
                    headers = {**get_auth_headers()}
                    sr = requests.get(f"{server_url}/runs/{session_id}/status", headers=headers, timeout=5)
                    if sr.status_code == 200:
                        s = sr.json()
                        if (s.get("state") == "FAILED"):
                            le = s.get("last_error") or {}
                            msg = le.get("message") or "Run failed"
                            et = le.get("error_type")
                            ec = le.get("error_code")
                            ra = le.get("retry_after_seconds")
                            phase = le.get("phase")
                            print("[Client] [ERROR] Server reported failure during silence.")
                            _print_structured_failure(msg, et, ec, ra, phase)
                            return
                    else:
                        pass
                except Exception:
                    pass
            time.sleep(0.5)
            continue

        for ev in events:
            last_seq = max(last_seq, ev["sequence_id"])
            last_event_time = time.time()
            et = ev["type"]
            
            if et == "PHASE_STARTED":
                current_phase = ev['payload'].get('phase') or 'unknown'
                print(f"[Phase] {current_phase}")
                phase_counters.setdefault(current_phase, 0)
                # Start or switch timer
                phase_timer.start(current_phase)
                
            elif et == "INSTRUCTION_TERMINATE_CLIENT":
                instr = ev["payload"]
                instruction_id = instr["instruction_id"]
                reason = instr.get("reason", "Server requested termination.")
                print(f"[Client] Termination requested by server: {reason}")
                try:
                    headers = {**get_auth_headers()}
                    requests.post(f"{server_url}/runs/{session_id}/results", json={
                        "instruction_id": instruction_id,
                        "status": "ok",
                        "result": {"terminated": True},
                        "message": reason,
                    }, headers=headers, timeout=5)
                except Exception as e:
                    print(f"[Client] Termination ack failed: {e}")
                # Stop timers and exit cleanly
                phase_timer.summarize_and_stop()
                set_current_session(None)
                return
                
            elif et == "INSTRUCTION_EXECUTE_BLENDER":
                _handle_blender_execution(ev, session_id, session_log_folder, phase_timer, server_url)
                
            elif et == "INSTRUCTION_PREPARE_SCENE":
                _handle_prepare_scene(ev, session_id, session_log_folder, phase_timer, server_url)
                
            elif et == "S3_LOGS_READY":
                # Server has uploaded logs to S3 (for admin purposes)
                print("[Client] Server reports logs have been archived.")
                    
            elif et == "RUN_COMPLETED":
                _handle_run_completion(ev, session_id, last_seq, phase_timer, server_url)
                set_current_session(None)
                return
                
            elif et == "RUN_FAILED":
                _handle_run_failure(ev, session_id, last_seq, phase_timer, server_url)
                set_current_session(None)
                return
                
            elif et == "INSTRUCTION_REQUEST_USER_INPUT":
                _handle_user_input(ev, session_id, phase_timer, server_url)
            
            elif et == "PHASE_HEARTBEAT":
                hb = ev.get("payload", {})
                hb_phase = hb.get("phase") or "unknown"
                elapsed_ms = hb.get("elapsed_ms")
                step = hb.get("step")
                note = hb.get("note")
                msg = f"[Heartbeat] phase={hb_phase}"
                if elapsed_ms is not None:
                    msg += f", t={int(elapsed_ms/1000)}s"
                if step:
                    msg += f", step={step}"
                if note:
                    msg += f" ({note})"
                print(msg)


def _handle_blender_execution(ev, session_id: str, session_log_folder: str, phase_timer: PhaseTimer, server_url: str):
    """Handle Blender code execution instruction."""
    instr = ev["payload"]
    instruction_id = instr["instruction_id"]
    code = instr.get("code", "")
    expects_render = bool(instr.get("expects_render", False))
    image_prefix = instr.get("image_prefix", "render")
    if image_prefix not in RENDER_PREFIX_KEYS:
        image_prefix = "render"
    count = int(instr.get("count", 5))
    
    # Load Blender configuration
    blender_config = get_blender_config()
    headless_enabled = blender_config.get("headless_rendering", True)
    headless_timeout = blender_config.get("headless_timeout", 300)
    fallback_to_socket = blender_config.get("fallback_to_socket", True)
    
    # Print a user-facing notice with a short snippet
    snippet = code.strip().splitlines()
    snippet = "\n".join(snippet[:5]) + ("\n..." if len(code.strip().splitlines()) > 10 else "")
    print("\n===== Executing Blender code from server =====")
    print(snippet)
    print("===== End snippet =====\n")
    
    # Ensure the render output directory points to the local per-session images directory
    render_images_dir = os.path.join(session_log_folder, "result", "images")
    try:
        os.makedirs(render_images_dir, exist_ok=True)
    except Exception:
        pass
    
    def _rewrite_output_path(code_text: str, out_dir: str) -> str:
        out_dir_norm = out_dir.replace('\\', '/')
        # Replace placeholder if present
        if "__LL3M_OUTPUT_DIR__" in code_text:
            return code_text.replace("__LL3M_OUTPUT_DIR__", out_dir_norm)
        # Otherwise, rewrite any render_scene(...) call to ensure output_path uses local dir
        def _repl(match: re.Match) -> str:
            args = match.group(1)
            # Remove existing output_path arguments
            args_no_out = re.sub(r"\boutput_path\s*=\s*(['\"][^'\"]*['\"])\s*,?", "", args)
            # Collapse duplicate commas and whitespace
            args_no_out = re.sub(r",\s*,", ", ", args_no_out)
            args_no_out = args_no_out.strip().rstrip(',').strip()
            if args_no_out:
                new_args = f"{args_no_out}, output_path='{out_dir_norm}'"
            else:
                new_args = f"output_path='{out_dir_norm}'"
            return f"render_scene({new_args})"
        try:
            # Only match function calls, not function definitions
            return re.sub(r"(?<!def\s)render_scene\((.*?)\)", _repl, code_text, flags=re.DOTALL)
        except Exception:
            # Fallback: append a safe call at end
            return code_text + f"\n# ll3m: appended render call with local output path\nrender_scene(output_path='{out_dir_norm}')\n"
    # Prepend resolution scaling preamble; prefer client config, fallback to server payload
    try:
        # Client config
        cfg = load_client_config()
        render_cfg = (cfg.get("render") or {})
        scale_cfg = render_cfg.get("resolution_scale")
        scale = scale_cfg if isinstance(scale_cfg, (int, float)) else instr.get("resolution_scale")
        preamble = ""
        if isinstance(scale, (int, float)):
            # Validate resolution_scale: must be between 0-1, default to 1.0 if invalid
            if not (0 <= scale <= 1):
                scale = 1.0  # Default to 1.0 if outside range
            percent = int(round(float(scale) * 100))
            preamble = (
                "import bpy\n"
                "scene = bpy.context.scene\n"
                f"scene.render.resolution_percentage = {percent}\n"
            )
        code_effective = (preamble + code) if preamble else code
    except Exception:
        code_effective = code

    code_to_run = _rewrite_output_path(code_effective, render_images_dir)
    
    # Execute code with headless support
    blend_snapshot_path = None
    if expects_render and headless_enabled:
        # Save a copy of the current scene from the interactive addon process
        try:
            snapshot_dir = os.path.join(session_log_folder, "temp")
            os.makedirs(snapshot_dir, exist_ok=True)
        except Exception:
            snapshot_dir = session_log_folder
        blend_snapshot_path = os.path.join(snapshot_dir, "scene_snapshot.blend")
        print("[Client] Creating scene snapshot for headless rendering...")
        save_res = BlenderClient.save_scene_copy(blend_snapshot_path, pack=True)
        if isinstance(save_res, dict) and (save_res.get("status") == "error" or save_res.get("saved") is False):
            msg = save_res.get("message") or "Unknown save error"
            print(f"[Client] Scene snapshot failed: {msg}. Proceeding without snapshot.")
            blend_snapshot_path = None

    result = BlenderClient.execute_code(
        code_to_run,
        expects_render=expects_render,
        headless_enabled=headless_enabled,
        headless_timeout=headless_timeout,
        fallback_to_socket=fallback_to_socket
    )

    # If we executed in headless mode and have a snapshot, re-run using the snapshot.
    # Always render sequentially to reduce CPU usage and system workload.
    if expects_render and headless_enabled and blend_snapshot_path:
        from blender.headless import execute_headless_blender
        print(f"[Client] Running headless renders sequentially for {count} angles against scene snapshot...")
        result = execute_headless_blender(code_to_run, timeout=headless_timeout, blend_path=blend_snapshot_path)

    # Evaluate execution status more robustly; treat textual error cues as failures
    def _infer_success(res) -> tuple[bool, str | None]:
        try:
            # Explicit error flag
            if isinstance(res, dict):
                status_val = (res.get("status") or "").lower()
                if status_val == "error":
                    return False, res.get("message")
            # Aggregate potential error-bearing text
            text_blobs: list[str] = []
            if isinstance(res, dict):
                for key in ("message", "result"):
                    val = res.get(key)
                    if isinstance(val, str):
                        text_blobs.append(val)
            elif isinstance(res, str):
                text_blobs.append(res)

            joined = "\n".join(text_blobs).lower()
            # Common Blender/operator error signatures
            error_tokens = (
                "error:",
                " error ",
                "failed",
                "traceback",
                "exception",
                "context is incorrect",
                "bpy.ops",
                "unrecognized",
            )
            if any(tok in joined for tok in error_tokens):
                return False, ("Execution error detected: " + (text_blobs[0] if text_blobs else "")) or None
            return True, None
        except Exception:
            # If unsure, keep previous behavior but be conservative
            return isinstance(res, dict) and (res.get("status", "ok") != "error"), None

    status_ok, inferred_msg = _infer_success(result)

    # Detect connection refused patterns from BlenderClient even if not marked as error
    if isinstance(result, dict):
        msg = ((result.get("message") or "") + "\n" + str(result.get("result") or "")).lower()
        if any(tok in msg for tok in ("actively refused", "connection refused", "10061")):
            print("[Client] Blender connection refused. Aborting the session...")
            print("[Client] Please ensure ll3m blender addon is open")
            try:
                headers = {**get_auth_headers()}
                requests.post(f"{server_url}/runs/{session_id}/abort", json={}, headers=headers, timeout=5)
            except Exception as e:
                print(f"[Client] Abort notify failed: {e}")
            return
    
    
    # If this instruction renders images, attempt to find and upload them
    if expects_render and status_ok:
        # Print resolution and rendering info on client side
        try:
            cfg = load_client_config()
            render_cfg = (cfg.get("render") or {})
            scale_cfg = render_cfg.get("resolution_scale")
            gpu_rendering = render_cfg.get("gpu_rendering", False)
            
            if isinstance(scale_cfg, (int, float)) and 0 <= scale_cfg <= 1:
                # Calculate effective resolution (assuming 1920x1080 base)
                base_w, base_h = 1920, 1080
                effective_w = int(base_w * scale_cfg)
                effective_h = int(base_h * scale_cfg)
                print(f"[Client] Render resolution: {effective_w} x {effective_h} (base {base_w}x{base_h} @ {int(scale_cfg * 100)}%)")
            
            # Print GPU/CPU rendering info
            if gpu_rendering:
                print(f"[Client] Rendering method: GPU acceleration enabled (config setting)")
            else:
                print(f"[Client] Rendering method: CPU rendering (config setting)")
        except Exception:
            pass
        
        _upload_render_images(session_log_folder, image_prefix, count, instruction_id, session_id, server_url)
    
    # Send response back to server
    response_payload = {
        "instruction_id": instruction_id,
        # Force failure status when heuristics detected an error
        "status": ("ok" if status_ok else "error"),
        "result": (result.get("result") if isinstance(result, dict) else result),
        "message": (inferred_msg or (result.get("message") if isinstance(result, dict) else None)),
    }
    
    try:
        headers = {**get_auth_headers()}
        requests.post(f"{server_url}/runs/{session_id}/results", json=response_payload, headers=headers, timeout=10)
        
        # Print user-friendly feedback based on execution result
        if status_ok:
            print("[Client] [OK] Blender code executed successfully! Waiting for next action from server...")
            # Suppress repeated rate limit prints; it's already shown at startup
        else:
            print("[Client] [RETRY] Blender code execution failed. Waiting for server to provide corrected version...")
            # Prefer inferred message, fall back to server-provided message
            err_msg_display = inferred_msg or (result.get("message") if isinstance(result, dict) else None)
            if err_msg_display:
                print(f"   Error details: {err_msg_display}")
                
    except Exception as e:
        print(f"[Client] [ERROR] Failed to send response to server: {e}")


def _upload_render_images(session_log_folder: str, image_prefix: str, count: int, instruction_id: str, session_id: str, server_url: str):
    """Upload rendered images to the server."""
    try:
        # Use the per-session path to align with server's expected save location
        images_dir = os.path.join(session_log_folder, "result", "images")

        # Collect expected files by convention: prefix_1..count.png
        files_to_upload = []
        for i in range(1, count + 1):
            fp = os.path.join(images_dir, f"{image_prefix}_{i}.png")
            if os.path.exists(fp):
                files_to_upload.append(fp)

        if not files_to_upload:
            print(f"[Client] No render images found to upload in {images_dir} (prefix={image_prefix}).")
        else:
            print(f"[Client] Uploading {len(files_to_upload)} rendered images (prefix={image_prefix}) to server...")
            multipart = [("files", (os.path.basename(p), open(p, "rb"), "image/png")) for p in files_to_upload]
            data = {"instruction_id": instruction_id, "image_prefix": image_prefix}
            try:
                headers = {**get_auth_headers()}
                ur = requests.post(f"{server_url}/runs/{session_id}/images", files=multipart, data=data, headers=headers, timeout=60)
                ur.raise_for_status()
                print(f"[Client] Upload complete: {len(files_to_upload)} images uploaded.")
            finally:
                for _, (name, fh, _ct) in multipart:
                    try:
                        fh.close()
                    except Exception:
                        pass
    except Exception as e:
        print(f"[Client] Upload error: {e}")


def _handle_run_completion(ev, session_id: str, last_seq: int, phase_timer: PhaseTimer, server_url: str):
    """Handle run completion event."""
    phase_timer.summarize_and_stop()
    print("Run completed.")
    
    # Show feedback form from client config
    from utils.feedback import show_feedback_form
    print("🎉 Thanks for using LL3M!")
    show_feedback_form()


def _handle_run_failure(ev, session_id: str, last_seq: int, phase_timer: PhaseTimer, server_url: str):
    """Handle run failure event."""
    phase_timer.summarize_and_stop()
    pld = ev.get('payload', {})
    msg = pld.get('message') or 'Run failed'
    error_type = pld.get('error_type')
    error_code = pld.get('error_code')
    retry_after_seconds = pld.get('retry_after_seconds')
    phase = pld.get('phase')
    _print_structured_failure(msg, error_type, error_code, retry_after_seconds, phase)
    
    # Show feedback form from client config
    from utils.feedback import show_feedback_form
    show_feedback_form()


def _handle_prepare_scene(ev, session_id: str, session_log_folder: str, phase_timer: PhaseTimer, server_url: str):
    """Handle scene preparation instruction for server-side rendering."""
    instr = ev["payload"]
    instruction_id = instr["instruction_id"]
    filename = instr.get("filename", "render")
    num_angles = int(instr.get("num_angles", 5))
    
    print(f"[Client] Preparing scene for server-side rendering: {filename} ({num_angles} angles)")
    
    # Load Blender configuration
    blender_config = get_blender_config()
    headless_enabled = blender_config.get("headless_rendering", True)
    
    # Create scene snapshot
    blend_snapshot_path = None
    if headless_enabled:
        try:
            snapshot_dir = os.path.join(session_log_folder, "temp")
            os.makedirs(snapshot_dir, exist_ok=True)
        except Exception:
            snapshot_dir = session_log_folder
        blend_snapshot_path = os.path.join(snapshot_dir, f"scene_{instruction_id}.blend")
        print("[Client] Creating scene snapshot for server-side rendering...")
        save_res = BlenderClient.save_scene_copy(blend_snapshot_path, pack=True)
        if isinstance(save_res, dict) and (save_res.get("status") == "error" or save_res.get("saved") is False):
            msg = save_res.get("message") or "Unknown save error"
            print(f"[Client] Scene snapshot failed: {msg}")
            # Send error response
            try:
                headers = {**get_auth_headers()}
                requests.post(f"{server_url}/runs/{session_id}/results", json={
                    "instruction_id": instruction_id,
                    "status": "error",
                    "result": None,
                    "message": f"Failed to create scene snapshot: {msg}",
                }, headers=headers, timeout=10)
            except Exception as e:
                print(f"[Client] Failed to send error response: {e}")
            return
    else:
        print("[Client] Headless rendering disabled, cannot create scene snapshot")
        try:
            headers = {**get_auth_headers()}
            requests.post(f"{server_url}/runs/{session_id}/results", json={
                "instruction_id": instruction_id,
                "status": "error",
                "result": None,
                "message": "Headless rendering disabled, cannot create scene snapshot",
            }, headers=headers, timeout=10)
        except Exception as e:
            print(f"[Client] Failed to send error response: {e}")
        return
    
    # Upload .blend file to server
    try:
        print(f"[Client] Uploading .blend file to server: {blend_snapshot_path}")
        with open(blend_snapshot_path, 'rb') as f:
            files = {'file': (os.path.basename(blend_snapshot_path), f, 'application/octet-stream')}
            data = {'instruction_id': instruction_id}
            headers = {**get_auth_headers()}
            r = requests.post(f"{server_url}/runs/{session_id}/blend_file", files=files, data=data, headers=headers, timeout=60)
        r.raise_for_status()
        print("[Client] .blend file uploaded successfully")
        
        # The server will automatically submit the result, so we don't need to do anything else
        # The instruction result will be handled by the server's upload endpoint
        
    except Exception as e:
        print(f"[Client] Failed to upload .blend file: {e}")
        try:
            headers = {**get_auth_headers()}
            requests.post(f"{server_url}/runs/{session_id}/results", json={
                "instruction_id": instruction_id,
                "status": "error",
                "result": None,
                "message": f"Failed to upload .blend file: {e}",
            }, headers=headers, timeout=10)
        except Exception as e2:
            print(f"[Client] Failed to send error response: {e2}")


def _handle_user_input(ev, session_id: str, phase_timer: PhaseTimer, server_url: str):
    """Handle user input request."""
    instr = ev["payload"]
    instruction_id = instr["instruction_id"]
    prompt = instr.get("prompt", "Enter input: ")
    # Pause phase timer while waiting for user input
    phase_timer.pause()
    try:
        # Ensure clean prompt display
        # Inform the user how to exit and about timeout
        user_text = input(f"{prompt}\n(Type TERMINATE to exit)\n[WARNING: Session will timeout after 3 minutes of inactivity]\n")
    except EOFError:
        user_text = ""
    except Exception as e:
        user_text = ""
        print(f"[Client] Input error: {e}")
    try:
        headers = {**get_auth_headers()}
        requests.post(f"{server_url}/runs/{session_id}/results", json={
            "instruction_id": instruction_id,
            "status": "ok",
            "result": {"user_input": user_text},
            "message": None,
        }, headers=headers, timeout=10)
    except Exception as e:
        print(f"Post result error: {e}")
    finally:
        # Resume timer after input is handled
        phase_timer.resume()


def _print_structured_failure(message: str, error_type: str | None, error_code: int | None, retry_after_seconds: int | None, phase: str | None):
    if phase:
        print(f"Run failed during phase: {phase}")
    print(f"Run failed: {message}")
    if error_type or error_code is not None:
        extra = []
        if error_type:
            extra.append(f"type={error_type}")
        if error_code is not None:
            extra.append(f"code={error_code}")
        print("Details: " + ", ".join(extra))
    if retry_after_seconds:
        print(f"Suggestion: retry after ~{retry_after_seconds}s")


def parse_arguments():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="LL3M Client - Standalone client for LL3M Cloud service",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                           # Use default prompt "Generate a chair"
  python main.py --text "Create a table"   # Use custom prompt
  python main.py --text ""                 # Use empty prompt (let server decide)
  python main.py --image chair.png         # Use image as input
  python main.py --list-sessions           # List all your sessions
  python main.py --session-id "abc123" --prompt "add armrests"  # Refine existing session
        """
    )
    
    # Create mutually exclusive group for text and image inputs
    input_group = parser.add_mutually_exclusive_group(required=False)
    
    input_group.add_argument(
        "--text",
        type=str,
        default="Generate a chair",
        help="Text prompt for the LL3M session (default: 'Generate a chair')"
    )
    
    input_group.add_argument(
        "--image",
        type=str,
        help="Path to input image file for the LL3M session"
    )

    parser.add_argument("--session-id", type=str, help="Previous session ID to refine (requires --prompt)")
    parser.add_argument("--prompt", type=str, help="Refinement prompt for the session (requires --session-id)")
    parser.add_argument("--list-sessions", action="store_true", help="List all sessions owned by user")
    parser.add_argument("--login", action="store_true", help="Run browser login and save tokens")
    parser.add_argument("--logout", action="store_true", help="Clear local tokens")
    parser.add_argument("--accept-terms", action="store_true", help="Accept terms and conditions")
    
    return parser.parse_args()


def main():
    """Main entry point for the LL3M client."""
    # Parse command-line arguments
    args = parse_arguments()
    
    # Load server configuration
    server_config = get_server_config()
    server_url = server_config["url"]
    
    # Handle auth-only commands
    if args.login:
        login_via_pkce()
        return
    if args.logout:
        logout_local()
        return
    if args.accept_terms:
        accept_terms(server_url)
        return
    
    print(f"[Client] Connecting to server: {server_url}")
    
    # First, check if Blender addon is running
    print("[Client] Checking Blender addon connection...")
    if not check_blender_addon_connection():
        print("[Client] [ERROR] Blender addon is not running or not accessible.")
        print("[Client] Please:")
        print("   1. Open Blender")
        print("   2. Install and enable the LL3M addon")
        print("   3. Make sure the addon is running")
        print("   4. Try again")
        sys.exit(1)
    print("[Client] [OK] Blender addon is running")
    
    # Check GPU availability after Blender addon is confirmed running
    print("[Client] Checking GPU availability...")
    try:
        from config.loader import get_effective_gpu_setting
        gpu_settings = get_effective_gpu_setting()
        
        if gpu_settings['gpu_available']:
            gpu_info = gpu_settings['gpu_info']
            print(f"[Client] [OK] GPU detected: {gpu_info['gpu_type']} - {gpu_info['preferred_engine']} rendering available")
        else:
            print(f"[Client] [INFO] No GPU detected - will use CPU rendering")
            
        if gpu_settings['gpu_rendering']:
            print(f"[Client] [GPU] GPU rendering enabled")
        else:
            print(f"[Client] [CPU] CPU rendering enabled")
            
    except Exception as e:
        print(f"[Client] [WARN] GPU detection failed: {e}")
        print(f"[Client] [CPU] Will use CPU rendering as fallback")
    
    # Check terms and conditions acceptance
    print("[Client] Checking terms and conditions acceptance...")
    terms_status = check_terms_status(server_url)
    if terms_status and not terms_status.get("accepted", False):
        print("[Client] [TERMS] Terms and conditions must be accepted to use this service.")
        print(f"[Client] Please review and accept the terms at: {terms_status.get('terms_url', 'N/A')}")
        print("[Client] To accept terms, run: python main.py --accept-terms")
        print("[Client] After accepting terms, please run the client again.")
        sys.exit(1)
    elif terms_status:
        print("[Client] [OK] Terms and conditions accepted")
    
    # Check rate limit status (only if authenticated)
    print("[Client] Checking rate limit status...")
    rate_status = check_rate_limit_status(server_url)
    if rate_status:
        rate_info = rate_status.get("rate_limit", {})
        remaining = rate_info.get("remaining", -1)
        limit = rate_info.get("limit", -1)
        is_admin = rate_info.get("is_admin", False)
        
        if is_admin:
            print(f"[Client] [OK] Admin user - Unlimited access")
        elif remaining >= 0:
            if remaining > 0:
                print(f"[Client] [OK] Rate limit: {remaining}/{limit} requests remaining today")
            else:
                print(f"[Client] [WARN] Rate limit: {limit - remaining}/{limit} requests used today (limit reached)")
                reset_time = rate_info.get("reset_time")
                if reset_time:
                    try:
                        from datetime import datetime
                        rt = datetime.fromisoformat(reset_time.replace("Z", "+00:00"))
                        print(f"[Client] Resets at: {rt:%Y-%m-%d %H:%M %Z} (UTC)")
                    except Exception:
                        print(f"[Client] Resets at: {reset_time}")
                else:
                    print("[Client] Resets at: Unknown")
                print("[Client] Please try again tomorrow or contact an admin.")
                sys.exit(1)
        else:
            print(f"[Client] [WARN] Rate limit status unknown (fallback mode)")
    else:
        print("[Client] [WARN] Could not check rate limit status (not authenticated)")
        print("[Client] Rate limiting will be checked when starting the run...")
    
    # Validate session refinement mode
    if args.session_id and not args.prompt:
        print("[Client] Error: --prompt is required when using --session-id")
        sys.exit(1)
    if args.prompt and not args.session_id:
        print("[Client] Error: --session-id is required when using --prompt")
        sys.exit(1)
    
    # Handle list sessions mode
    if args.list_sessions:
        list_user_sessions(server_url)
        return
    
    # Handle session refinement mode
    if args.session_id and args.prompt:
        print(f"[Client] Using session refinement: {args.session_id}")
        print(f"[Client] Refinement prompt: {args.prompt}")
        try:
            sid = start_run(server_url, session_id=args.session_id, refinement_prompt=args.prompt)
            poll_events(sid, server_url)
        except Exception as e:
            print(f"[Client] Failed to start session refinement: {e}")
            sys.exit(1)
        return
    
    # Determine input type and display appropriate message
    if args.image:
        print(f"[Client] Using image input: '{args.image}'")
        input_text = None  # No text when using image
    else:
        print(f"[Client] Using text prompt: '{args.text}'")
        input_text = args.text
    
    # Start the session
    try:
        sid = start_run(server_url, text=input_text, image_path=args.image)
        poll_events(sid, server_url)
    except (FileNotFoundError, PermissionError) as e:
        print(f"[Client] Error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"[Client] Failed to start session: {e}")
        sys.exit(1)


def accept_terms(server_url: str):
    """Accept terms and conditions."""
    try:
        headers = {**get_auth_headers()}
        if not headers.get("Authorization"):
            print("[Client] [ERROR] Not authenticated. Please login first:")
            print("         python main.py --login")
            return
        
        # Display the terms and conditions
        print("\n" + "="*80)
        print("TERMS AND CONDITIONS")
        print("="*80)
        
        # Read and display the markdown terms file
        md_file_path = os.path.join(CLIENT_ROOT, "LL3M_Academic_and_Evaluation_License_Agreement.md")
        try:
            with open(md_file_path, 'r', encoding='utf-8') as f:
                terms_content = f.read()
            
            # Display the markdown content with proper formatting for terminal
            lines = terms_content.split('\n')
            for line in lines:
                if line.strip():
                    # Handle markdown headers
                    if line.startswith('## '):
                        print(f"\n{line[3:]}")  # Remove ## and display
                    elif line.startswith('### '):
                        print(f"\n  {line[4:]}")  # Remove ### and indent
                    elif line.startswith('**') and line.endswith('**'):
                        # Handle bold text
                        clean_text = line.replace('**', '').strip()
                        print(f"\n{clean_text}")
                    else:
                        print(line)
                else:
                    print()  # Add blank line for empty lines
                    
        except FileNotFoundError:
            print("[Client] [ERROR] Terms and conditions file not found.")
            print("         Please ensure the markdown file exists in the client directory.")
            return
        except Exception as e:
            print(f"[Client] [ERROR] Failed to read markdown terms file: {e}")
            return
        
        print("\n" + "="*80)
        print("END OF TERMS AND CONDITIONS")
        print("="*80)
        
        # Ask for explicit confirmation
        while True:
            response = input("\nDo you agree to these terms and conditions? (yes/no): ").strip().lower()
            if response == 'yes':
                break
            elif response == 'no':
                print("[Client] Terms not accepted. You cannot use the service without accepting the terms.")
                return
            else:
                print("Please type 'yes' to agree or 'no' to decline.")
        
        print("\n[Client] Accepting terms and conditions...")
        r = requests.post(normalize_url(server_url, "terms/accept"), headers=headers, timeout=10)
        r.raise_for_status()
        
        result = r.json()
        print(f"[Client] [OK] {result.get('message', 'Terms accepted successfully')}")
        print(f"[Client] Version: {result.get('version', 'N/A')}")
        print(f"[Client] Accepted at: {result.get('accepted_at', 'N/A')}")
        
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 401:
            print("[Client] [ERROR] Not authenticated. Please login first:")
            print("         python main.py --login")
        else:
            print(f"[Client] [ERROR] Failed to accept terms: {e}")
    except Exception as e:
        print(f"[Client] [ERROR] Failed to accept terms: {e}")


def list_user_sessions(server_url: str):
    """List all sessions owned by the current user."""
    try:
        headers = {**get_auth_headers()}
        r = requests.get(f"{server_url}/sessions", headers=headers, timeout=30)
        r.raise_for_status()
        
        sessions = r.json()
        print(f"[Client] Found {len(sessions)} sessions:")
        for session_id in sessions:
            print(f"  {session_id}")
            
    except Exception as e:
        print(f"[Client] Failed to list sessions: {e}")


if __name__ == "__main__":
    main()