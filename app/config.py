# ─── Config ───
TEMPLATES_AUTO_RELOAD = True

# ─── Custom filters for Jinja2 ───
import time
from fastapi.templating import Jinja2Templates

def timestamp_to_date(ts):
    """Convert Unix timestamp to readable date."""
    try:
        return time.strftime("%b %d, %Y", time.localtime(int(ts)))
    except (ValueError, TypeError):
        return ""

def format_duration(seconds):
    """Convert seconds to mm:ss."""
    try:
        s = int(seconds)
        m = s // 60
        sec = s % 60
        return f"{m}:{sec:02d}"
    except (ValueError, TypeError):
        return "0:00"
