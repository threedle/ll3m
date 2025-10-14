"""
Timer utilities for LL3M Client.
Handles phase timing and progress tracking.
"""

import threading
import time


class PhaseTimer:
    """Timer class for tracking execution phases."""
    
    def __init__(self):
        self._lock = threading.Lock()
        self._phase = None
        self._start_ts = None
        self._stop_flag = False
        self._thread = None
        self._paused = False
        self._pause_ts = None

    def start(self, phase_name: str):
        """Start timing a new phase."""
        with self._lock:
            # If same phase is already running, ignore
            if self._phase == phase_name and self._thread and self._thread.is_alive():
                return
            # Stop existing timer
            self._stop_locked()
            self._phase = phase_name
            self._start_ts = time.time()
            self._stop_flag = False
            self._thread = threading.Thread(target=self._run_loop, daemon=True)
            self._thread.start()

    def stop(self):
        """Stop the timer."""
        with self._lock:
            self._stop_locked()

    def _stop_locked(self):
        """Internal method to stop timer (must be called with lock held)."""
        self._stop_flag = True
        t = self._thread
        # Release lock before joining to avoid deadlock if _run_loop prints
        if t:
            self._lock.release()
            try:
                t.join(timeout=0.5)
            except Exception:
                pass
            self._lock.acquire()
        self._thread = None

    def _run_loop(self):
        """Main timer loop that prints elapsed time periodically."""
        # Periodically print elapsed time
        last_print = 0
        while not self._stop_flag:
            now = time.time()
            if now - last_print >= 5.0:
                last_print = now
                phase_key = self._phase or 'unknown'
                if self._paused:
                    # Do not print while paused
                    time.sleep(0.2)
                    continue
                elapsed = int(now - (self._start_ts or now))
                hh = elapsed // 3600
                rem = elapsed % 3600
                mm = rem // 60
                ss = rem % 60
                phase_label = self._format_phase_label(phase_key)
                # Print in requested format, e.g., [Initial Creation Phase: 0:01:17]
                print(f"[{phase_label}: {hh}:{mm:02d}:{ss:02d}]")
            time.sleep(0.2)

    def summarize_and_stop(self):
        """Print final summary and stop the timer."""
        with self._lock:
            phase = self._phase or 'unknown'
            elapsed = int((time.time() - self._start_ts)) if self._start_ts else 0
            mm = elapsed // 60
            ss = elapsed % 60
            if self._phase is not None:
                print(f"[Phase {phase}] finished in {mm:02d}:{ss:02d}")
            self._stop_locked()

    def _format_phase_label(self, phase_key: str) -> str:
        """Format phase key into a readable label."""
        mapping = {
            'initial_creation': 'Initial Creation Phase',
            'auto_refinement': 'Auto Refinement Phase',
            'user_guided_refinement': 'User Guided Refinement Phase',
            'unknown': 'Unknown Phase',
        }
        return mapping.get(phase_key, phase_key.replace('_', ' ').title() + ' Phase')

    def pause(self):
        """Pause the timer."""
        with self._lock:
            if not self._paused:
                self._paused = True
                self._pause_ts = time.time()

    def resume(self):
        """Resume the timer."""
        with self._lock:
            if self._paused:
                # Shift start time forward by paused duration so elapsed excludes pause
                paused_duration = time.time() - (self._pause_ts or time.time())
                if self._start_ts is not None:
                    self._start_ts += paused_duration
                self._paused = False
                self._pause_ts = None
