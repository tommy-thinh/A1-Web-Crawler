import json
import logging
from urllib.parse import urlparse

from .config import (
    CATEGORY_CONFIG,
    TIMELINE_MAPPING_FILE,
)
from .http_utils import safe_get
from .parsing import (
    article_has_category,
    extract_post_links_from_category_html,
    extract_article_id_from_url,
)


def get_zone_id_for_article(article_url: str, target_cat_path: str) -> int | None:
    """
    Given an article URL and a target category path (e.g. '/the-gioi.htm'):

      1. Fetch the article HTML.
      2. Check if the article actually belongs to that category (article_has_category).
      3. If yes, call the comment API with pagesize=1 to get at least one comment.
      4. If the response has a comment with zone_id, return that zone_id.
    """
    from .config import COMMENT_APP_KEY  # avoid circular import
    from .http_utils import safe_get as _safe_get

    article_resp = _safe_get(article_url)
    if not article_resp:
        return None

    html = article_resp.text

    if not article_has_category(html, target_cat_path):
        logging.info(
            "Article %s does not appear to belong to category %s, skipping.",
            article_url, target_cat_path
        )
        return None

    post_id = extract_article_id_from_url(article_url)
    if not post_id:
        logging.warning(
            "Cannot extract postId from URL when getting zone_id: %s",
            article_url
        )
        return None

    base_url = "https://id.tuoitre.vn/api/getlist-comment.api"
    params = {
        "pageindex": 1,
        "pagesize": 1,
        "objId": post_id,
        "objType": 1,
        "objectpopupid": "",
        "sort": 2,
        "commentid": "",
        "command": "",
        "appKey": COMMENT_APP_KEY,
    }

    resp = _safe_get(base_url, params=params)
    if not resp:
        return None

    try:
        outer = resp.json()
        data_str = outer.get("Data", "[]")
        comments = json.loads(data_str)
    except Exception as e:
        logging.warning(
            "Failed to parse comments when getting zone_id for %s: %s",
            article_url, e
        )
        return None

    if not comments:
        logging.info(
            "No comments found for %s when trying to get zone_id for category %s.",
            article_url, target_cat_path
        )
        return None

    first = comments[0]
    zone_id = first.get("zone_id")
    if zone_id is None:
        logging.info("No zone_id in first comment object for %s.", article_url)
        return None

    logging.info(
        "zone_id for %s (category %s) is %s",
        article_url, target_cat_path, zone_id
    )
    return int(zone_id)


def discover_zone_id_for_category(category_url: str, max_articles_to_try: int = 20) -> int | None:
    """
    Heuristic for discovering timeline id for a category URL, e.g. /the-gioi.htm:

      - Load that category's main page (may contain mixed-category content).
      - Extract candidate article links.
      - For each candidate (up to max_articles_to_try):
          * Check if it belongs to this category.
          * If yes and it has comments, read zone_id from comment API.
      - Return the first valid zone_id we find.
    """
    resp = safe_get(category_url)
    if not resp:
        logging.warning(
            "Cannot fetch category page to discover zone_id: %s",
            category_url
        )
        return None

    candidate_links = extract_post_links_from_category_html(resp.text, category_url)
    if not candidate_links:
        logging.warning(
            "No candidate article links found on category page %s",
            category_url
        )
        return None

    parsed = urlparse(category_url)
    target_cat_path = parsed.path  # e.g. '/the-gioi.htm'

    logging.info(
        "Discovering zone_id for category %s using up to %d articles",
        target_cat_path,
        max_articles_to_try,
    )

    tried = 0
    for article_url in candidate_links:
        if tried >= max_articles_to_try:
            break
        tried += 1

        zone_id = get_zone_id_for_article(article_url, target_cat_path)
        if zone_id is not None:
            return zone_id

    logging.warning(
        "Failed to discover zone_id for %s after trying %d articles",
        category_url,
        tried,
    )
    return None


def build_timeline_mapping(category_configs, mapping_file: str = TIMELINE_MAPPING_FILE) -> dict:
    """
    Build a mapping from category path (e.g. '/thoi-su.htm') to timeline id (e.g. 3),
    and save it as JSON. If the mapping file already exists, just load it.
    """
    import os

    if os.path.exists(mapping_file):
        logging.info("Loading existing timeline mapping from %s", mapping_file)
        with open(mapping_file, "r", encoding="utf-8") as f:
            return json.load(f)

    mapping = {}
    for cfg in category_configs:
        url = cfg["url"]
        parsed = urlparse(url)
        cat_path = parsed.path  # e.g. '/thoi-su.htm'

        logging.info(
            "Discovering timeline id for category %s (%s)",
            cfg.get("name", cat_path),
            url,
        )
        zone_id = discover_zone_id_for_category(url)
        if zone_id is not None:
            mapping[cat_path] = zone_id
        else:
            logging.warning("Could not determine timeline id for %s", url)

    with open(mapping_file, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)

    logging.info("Saved timeline mapping to %s: %s", mapping_file, mapping)
    return mapping


def load_timeline_mapping(mapping_file: str = TIMELINE_MAPPING_FILE) -> dict:
    """
    Load timeline mapping from disk or build a new one if it does not exist.
    """
    import os

    if not os.path.exists(mapping_file):
        logging.warning(
            "Timeline mapping file %s not found, building a new one.",
            mapping_file
        )
        return build_timeline_mapping(CATEGORY_CONFIG, mapping_file)
    with open(mapping_file, "r", encoding="utf-8") as f:
        return json.load(f)
