"""
Signal handling utilities for LL3M Client.
Handles graceful shutdown and cleanup.
"""

import signal
import sys
import time
import requests
import atexit
from typing import Optional



# Global variables for signal handling
_current_session_id: Optional[str] = None


def set_current_session(session_id: str):
    """Set the current session ID for signal handling."""
    global _current_session_id
    _current_session_id = session_id




def get_current_session() -> Optional[str]:
    """Get the current session ID."""
    return _current_session_id


def _signal_handler(signum, frame, server_url: str, client_root: str):
    """Handle SIGINT (Ctrl+C) by gracefully aborting the current session."""
    global _current_session_id
    if _current_session_id:
        print(f"\n[Client] Received interrupt signal. Aborting session {_current_session_id}...")
        try:
            # Import auth headers here to avoid circular imports
            try:
                from auth.token_store import get_auth_headers
                headers = {**get_auth_headers()}
            except Exception:
                headers = {}
            
            # Send abort request with a short timeout
            response = requests.post(f"{server_url}/runs/{_current_session_id}/abort", json={}, headers=headers, timeout=3)
            if response.status_code == 200:
                print("[Client] Session aborted successfully.")
            else:
                print(f"[Client] Abort request failed with status {response.status_code}")
        except requests.exceptions.Timeout:
            print("[Client] Abort request timed out, but session should be cleaned up by server timeout.")
        except Exception as e:
            print(f"[Client] Failed to abort session: {e}")
        # Show feedback form for aborted sessions
        from .feedback import show_feedback_form
        show_feedback_form()
    else:
        print("\n[Client] Received interrupt signal. No active session to abort.")
    
    # Force exit after a brief delay to allow abort request to be sent
    time.sleep(0.1)
    sys.exit(0)


def _cleanup_on_exit(server_url: str, client_root: str):
    """Cleanup function called on normal exit."""
    global _current_session_id
    if _current_session_id:
        print(f"[Client] Cleaning up session {_current_session_id}...")
        try:
            # Import auth headers here to avoid circular imports
            try:
                from auth.token_store import get_auth_headers
                headers = {**get_auth_headers()}
            except Exception:
                headers = {}
            
            requests.post(f"{server_url}/runs/{_current_session_id}/abort", json={}, headers=headers, timeout=5)
        except Exception:
            pass  # Ignore errors during cleanup


def setup_signal_handlers(server_url: str, client_root: str):
    """Set up signal handlers for graceful shutdown."""
    def signal_handler_wrapper(signum, frame):
        _signal_handler(signum, frame, server_url, client_root)
    
    def cleanup_wrapper():
        _cleanup_on_exit(server_url, client_root)
    
    signal.signal(signal.SIGINT, signal_handler_wrapper)
    atexit.register(cleanup_wrapper)
