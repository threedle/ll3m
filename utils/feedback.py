"""
Client-side feedback collection utilities.
Handles displaying feedback forms from client configuration.
"""

def show_feedback_form():
    """
    Show feedback form from client configuration.
    This function is safe to call from anywhere in the client code.
    It will silently fail if config loading fails, ensuring it doesn't break the main flow.
    """
    try:
        from config.loader import load_client_config
        config = load_client_config()
        feedback_cfg = config.get("feedback", {})
        
        if not feedback_cfg.get("enabled", False):
            return
            
        feedback_url = feedback_cfg.get("google_form_url")
        feedback_message = feedback_cfg.get("message", "Help us improve LL3M! Share your feedback:")
        
        if feedback_url: 
            print("")
            print(f"💬 {feedback_message} {feedback_url}")
            print("")
    except Exception:
        # Silent fail - don't break the main flow if config loading fails
        pass


def get_feedback_url():
    """
    Get the feedback URL from client configuration.
    Returns None if not configured or disabled.
    """
    try:
        from config.loader import load_client_config
        config = load_client_config()
        feedback_cfg = config.get("feedback", {})
        
        if not feedback_cfg.get("enabled", False):
            return None
            
        feedback_url = feedback_cfg.get("google_form_url")
        
        if feedback_url: 
            return feedback_url
        
        return None
    except Exception:
        return None
