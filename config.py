import os
import logging

# -----------------------------
# Global configuration
# -----------------------------

CATEGORY_CONFIG = [
    {"name": "Thế giới", "url": "https://tuoitre.vn/the-gioi.htm", "target_posts": 2},
    {"name": "Công nghệ", "url": "https://tuoitre.vn/cong-nghe.htm", "target_posts": 2},
    {"name": "Thời sự", "url": "https://tuoitre.vn/thoi-su.htm", "target_posts": 2},
]

TIMELINE_MAPPING_FILE = "category_timeline_mapping.json"

# Politeness settings
REQUEST_DELAY_SECS = 1.0       # delay between HTTP requests
COMMENT_REQUEST_DELAY_SECS = 0.5

# Comment API appKey (client-side key from the site)
COMMENT_APP_KEY = (
    "lHLShlUMAshjvNkHmBzNqERFZammKUXB1DjEuXKfWAwkunzW6fFbfrhP/IG0Xwp7a"
    "PwhwIuucLW1TVC9lzmUoA=="
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

BASE_DOMAIN = "https://tuoitre.vn"
ROBOTS_URL = f"{BASE_DOMAIN}/robots.txt"

DATA_DIR = "data"
AUDIO_DIR = "audio"
IMAGES_DIR = "images"

# Start from page 1 (you changed your mind about starting at 2)
START_PAGE = 1

# Patterns or exact names to skip when downloading images
IGNORE_IMAGE_PATTERNS = [
    "author_default",
    "avatar",
    "banner",
    "logo",
    "newslette",     # newsletter.png
    "userdeffault",  # typo from site: "userdeffault.jpg"
]

# Ensure output folders exist
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(IMAGES_DIR, exist_ok=True)

# Basic logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
