import json
import logging
import os
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .config import (
    CATEGORY_CONFIG,
    TIMELINE_MAPPING_FILE,
    DATA_DIR,
    BASE_DOMAIN,
    START_PAGE,
)
from .http_utils import safe_get
from .parsing import (
    extract_article_metadata,
    extract_article_id_from_url,
    normalize_publish_date,
    extract_post_links_from_timeline_html,
)
from .timeline_mapping import load_timeline_mapping
from .media import download_tts_audio, download_images_from_article
from .social import (
    fetch_article_reactions,
    fetch_comments_for_post,
    get_comment_count_only,
)


def crawl_single_article(article_url: str, category_name: str):
    """
    Fetch one article page and collect:
      - metadata
      - audio (TTS)
      - images
      - reactions
      - comments
    Then save everything into data/<post_id>.json.
    """
    resp = safe_get(article_url)
    if not resp:
        return None, 0

    soup = BeautifulSoup(resp.text, "html.parser")

    meta = extract_article_metadata(soup, article_url, category_name)
    post_id = meta["postId"] or extract_article_id_from_url(article_url)

    if not post_id:
        logging.warning("Could not determine postId for article %s", article_url)
        return None, 0

    # Audio using deterministic TTS URL
    publish_date = normalize_publish_date(meta.get("date"))
    audio_local_path = None
    audio_url = None

    if publish_date:
        audio_url = None  # kept for compatibility with previous structure
        audio_local_path = download_tts_audio(post_id, publish_date)
    else:
        logging.info(
            "No publish date found for %s; skipping TTS audio generation.",
            post_id
        )

    # Images
    image_paths = download_images_from_article(soup, post_id)

    # Article-level reactions
    vote_reactions = fetch_article_reactions(post_id)

    # Comments (with replies)
    comments, total_comment_count = fetch_comments_for_post(post_id)

    post_data = {
        **meta,
        "audio_podcast": audio_local_path or audio_url,
        "images": image_paths,
        "vote_reactions": vote_reactions,
        "comments": comments,
        "total_comment_count": total_comment_count,
    }

    out_path = os.path.join(DATA_DIR, f"{post_id}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(post_data, f, ensure_ascii=False, indent=2)

    logging.info(
        "Saved post %s (%s) with %d comments to %s",
        post_id, meta["title"], total_comment_count, out_path
    )

    return post_data, total_comment_count


def crawl_category(
    category_cfg: dict,
    timeline_mapping: dict,
    found_post_with_20_global: bool,
    is_last_category: bool,
):
    """
    Global >=20-comment requirement:

    - For every category:
        * Crawl timeline pages.
        * Save posts up to target_posts for that category.
        * If any saved post has >= 20 comments, set found_post_with_20_global = True.

    - No longer require >= 20 comments per category.

    - After the normal crawl:
        * If found_post_with_20_global is already True, we stop.
        * If not, and this is the last category:
            - keep crawling additional pages in "search mode"
            - in search mode, first only get comment counts
            - if a post has >= 20 comments, fully crawl and save that one.

    Returns: (saved_posts, found_post_with_20_global).
    """
    category_url = category_cfg["url"]
    target_posts = category_cfg["target_posts"]

    parsed = urlparse(category_url)
    cat_path = parsed.path  # '/thoi-su.htm'
    cat_name = category_cfg.get("name", cat_path)

    zone_id = timeline_mapping.get(cat_path)
    if zone_id is None:
        logging.warning(
            "No timeline id found for category %s (%s), skipping.",
            cat_name, cat_path
        )
        return [], found_post_with_20_global

    logging.info(
        "=== Crawling category %s (%s) with timeline id %s ===",
        cat_name, category_url, zone_id
    )

    visited_urls = set()
    saved_posts = []

    page_index = START_PAGE
    max_category_pages = 100  # safety cap

    # 1) Normal crawl phase
    while True:
        if len(saved_posts) >= target_posts:
            break

        if page_index > max_category_pages:
            logging.warning(
                "Reached max category pages (%d) for %s (normal phase)",
                max_category_pages, category_url
            )
            break

        page_url = f"{BASE_DOMAIN}/timeline/{zone_id}/trang-{page_index}.htm"
        resp = safe_get(page_url)
        if not resp:
            logging.warning(
                "Failed to fetch timeline page %d (%s), skipping to next page",
                page_index, page_url
            )
            page_index += 1
            continue

        article_links = extract_post_links_from_timeline_html(resp.text, category_url)
        new_links = [u for u in article_links if u not in visited_urls]

        logging.info(
            "Timeline page %d (%s): %d links, %d new links (normal phase)",
            page_index, page_url, len(article_links), len(new_links)
        )

        for article_url in new_links:
            if len(saved_posts) >= target_posts:
                break

            visited_urls.add(article_url)

            post_data, comment_count = crawl_single_article(article_url, cat_name)
            if not post_data:
                continue

            saved_posts.append(post_data)

            if comment_count is not None and comment_count >= 20:
                found_post_with_20_global = True

        page_index += 1

    logging.info(
        "Finished NORMAL phase for category %s: saved %d posts, "
        "found_post_with_20_global=%s",
        cat_name, len(saved_posts), found_post_with_20_global
    )

    # 2) Extra search phase (only in last category and still no >=20-comment post)
    if not found_post_with_20_global and is_last_category:
        logging.info(
            "No >=20-comment post found in previous categories; "
            "entering SEARCH phase in last category %s",
            cat_name
        )

        while not found_post_with_20_global and page_index <= max_category_pages:
            page_url = f"{BASE_DOMAIN}/timeline/{zone_id}/trang-{page_index}.htm"
            resp = safe_get(page_url)
            if not resp:
                logging.warning(
                    "Failed to fetch timeline page %d (%s) in SEARCH phase, skipping",
                    page_index, page_url
                )
                page_index += 1
                continue

            article_links = extract_post_links_from_timeline_html(resp.text, category_url)
            new_links = [u for u in article_links if u not in visited_urls]

            logging.info(
                "Timeline page %d (%s): %d links, %d new links (SEARCH phase)",
                page_index, page_url, len(article_links), len(new_links)
            )

            for article_url in new_links:
                if found_post_with_20_global:
                    break

                visited_urls.add(article_url)

                comment_count = get_comment_count_only(article_url, cat_name)
                if comment_count is None:
                    comment_count = 0

                if comment_count >= 20:
                    post_data, final_cc = crawl_single_article(article_url, cat_name)
                    if post_data:
                        saved_posts.append(post_data)
                        found_post_with_20_global = True
                        logging.info(
                            "Found >=20-comment post in last category %s: %s with %d comments",
                            cat_name, article_url, final_cc
                        )
                        break

            page_index += 1

        logging.info(
            "Finished SEARCH phase for last category %s. "
            "found_post_with_20_global=%s",
            cat_name, found_post_with_20_global
        )

    logging.info(
        "Finished category %s: total saved posts=%d, "
        "found_post_with_20_global=%s",
        cat_name, len(saved_posts), found_post_with_20_global
    )

    return saved_posts, found_post_with_20_global


def main():
    # 1) Build or load mapping: category path -> timeline id
    timeline_mapping = load_timeline_mapping(TIMELINE_MAPPING_FILE)

    all_posts = []
    found_post_with_20_global = False

    # 2) Crawl
    for idx, cfg in enumerate(CATEGORY_CONFIG):
        is_last_category = (idx == len(CATEGORY_CONFIG) - 1)

        posts, found_post_with_20_global = crawl_category(
            cfg,
            timeline_mapping,
            found_post_with_20_global,
            is_last_category,
        )
        all_posts.extend(posts)

    logging.info(
        "=== Done. Total posts crawled across all categories: %d, "
        "found >=20-comment post somewhere: %s ===",
        len(all_posts),
        found_post_with_20_global,
    )


if __name__ == "__main__":
    main()
