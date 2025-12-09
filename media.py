import os
import logging
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .config import AUDIO_DIR, IMAGES_DIR, BASE_DOMAIN, IGNORE_IMAGE_PATTERNS
from .http_utils import safe_get
from .parsing import normalize_url


def build_tts_audio_url(post_id: str, publish_date: str) -> str:
    """
    Construct the TTS audio URL using the fixed pattern:
    https://tts.mediacdn.vn/YYYY/MM/DD/tuoitre-nu-1-POST_ID.m4a

    publish_date must be in format YYYY-MM-DD or YYYY/MM/DD.
    """
    date = publish_date.replace("-", "/")
    base = "https://tts.mediacdn.vn"
    return f"{base}/{date}/tuoitre-nu-1-{post_id}.m4a"


def download_tts_audio(post_id: str, publish_date: str) -> str | None:
    """
    Download the AI-generated audio for a post using the deterministic URL format.
    Saves the audio file as audio/<post_id>.m4a.
    """
    os.makedirs(AUDIO_DIR, exist_ok=True)

    tts_url = build_tts_audio_url(post_id, publish_date)
    local_path = os.path.join(AUDIO_DIR, f"{post_id}.m4a")

    if os.path.exists(local_path):
        logging.info("Audio already exists: %s", local_path)
        return local_path

    resp = safe_get(tts_url, stream=True)
    if not resp:
        logging.warning("Failed to fetch TTS audio from %s", tts_url)
        return None

    try:
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(8192):
                if chunk:
                    f.write(chunk)

        logging.info("Downloaded TTS audio for %s: %s", post_id, local_path)
        return local_path
    except Exception as e:
        logging.warning("Failed to save TTS audio for %s: %s", post_id, e)
        return None


def download_images_from_article(soup: BeautifulSoup, post_id: str):
    """
    Download all <img> tags inside the article content and save to ./images/<post_id>/.
    Keeps original filenames where possible and skips global assets such as logos and banners.
    """
    image_folder = os.path.join(IMAGES_DIR, post_id)
    os.makedirs(image_folder, exist_ok=True)

    image_paths = []

    for img in soup.find_all("img", src=True):
        src = img["src"].strip()
        if not src:
            continue

        # Skip base64 / inlined content
        if src.startswith("data:"):
            continue

        src_lower = src.lower()

        # Filter out site-wide assets
        if any(p in src_lower for p in IGNORE_IMAGE_PATTERNS):
            logging.info("Skipping irrelevant image: %s", src)
            continue

        img_url = normalize_url(src, base=BASE_DOMAIN)

        parsed = urlparse(img_url)
        filename = os.path.basename(parsed.path)
        if not filename:
            filename = f"{len(image_paths)+1}.jpg"

        local_path = os.path.join(image_folder, filename)

        if os.path.exists(local_path):
            image_paths.append(local_path)
            continue

        resp = safe_get(img_url, stream=True)
        if not resp:
            continue

        try:
            with open(local_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            image_paths.append(local_path)
            logging.info("Downloaded image: %s", img_url)
        except Exception as e:
            logging.warning("Failed to save image %s: %s", img_url, e)

    return image_paths
