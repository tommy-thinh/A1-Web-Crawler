import json
import logging
import time

from bs4 import BeautifulSoup

from .config import COMMENT_APP_KEY, COMMENT_REQUEST_DELAY_SECS
from .http_utils import safe_get
from .parsing import extract_article_metadata, extract_article_id_from_url


def fetch_article_reactions(post_id: str) -> dict:
    """
    Fetch reaction stats for an article.

    Endpoint: https://s5.tuoitre.vn/showvote-reaction.htm?newsid=<post_id>&m=viewreact

    Expected JSON:
      {"Success": true, "Data": [{...}, {...}]}

    Handles null / unexpected shapes defensively and returns {} on error.
    """
    url = "https://s5.tuoitre.vn/showvote-reaction.htm"
    params = {"newsid": post_id, "m": "viewreact"}

    resp = safe_get(url, params=params)
    if not resp:
        return {}

    try:
        data = resp.json()
    except Exception as e:
        logging.warning(
            "Failed to parse reactions JSON for post %s: %s",
            post_id, e
        )
        return {}

    if not isinstance(data, dict):
        logging.warning(
            "Unexpected reaction JSON type for post %s: %r",
            post_id, type(data)
        )
        return {}

    raw_items = data.get("Data")
    if raw_items is None:
        logging.info("Reaction JSON for post %s has Data=None", post_id)
        return {}
    if not isinstance(raw_items, list):
        logging.warning(
            "Unexpected 'Data' type in reactions for post %s: %r",
            post_id, type(raw_items)
        )
        return {}

    reactions: dict[str, dict] = {}
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        r_type = item.get("Type")
        if r_type is None:
            continue
        key = str(r_type)
        reactions[key] = {
            "TotalStar": item.get("TotalStar"),
            "TotalVotes": item.get("TotalVotes"),
            "UserTotal": item.get("UserTotal"),
            "AvgStarCount": item.get("AvgStarCount"),
        }

    return reactions


def fetch_comments_for_post(post_id: str, pagesize: int = 20, max_pages: int = 100):
    """
    Fetch comments for a post from the Tuoi Tre comment API.

    Endpoint:
      https://id.tuoitre.vn/api/getlist-comment.api?pageindex=1&pagesize=...&objId=...

    The "Data" field is a JSON-encoded list inside the outer JSON.
    We flatten, deduplicate by id, and then build a parent/replies structure.
    """
    base_url = "https://id.tuoitre.vn/api/getlist-comment.api"
    all_raw_comments = []

    for page in range(1, max_pages + 1):
        params = {
            "pageindex": page,
            "pagesize": pagesize,
            "objId": post_id,
            "objType": 1,
            "objectpopupid": "",
            "sort": 2,
            "commentid": "",
            "command": "",
            "appKey": COMMENT_APP_KEY,
        }

        logging.info("Fetching comments page %d for post %s", page, post_id)
        resp = safe_get(base_url, params=params)
        time.sleep(COMMENT_REQUEST_DELAY_SECS)

        if not resp:
            break

        try:
            outer = resp.json()
            data_str = outer.get("Data", "[]")
            comments_page = json.loads(data_str)
        except Exception as e:
            logging.warning(
                "Failed to parse comments JSON for %s page %d: %s",
                post_id, page, e
            )
            break

        if not comments_page:
            logging.info("No more comments for post %s at page %d", post_id, page)
            break

        all_raw_comments.extend(comments_page)

    # Deduplicate by comment id
    dedup: dict[str, dict] = {}
    for c in all_raw_comments:
        if not isinstance(c, dict):
            continue
        cid = c.get("id")
        if cid and cid not in dedup:
            dedup[cid] = c

    all_comments = list(dedup.values())

    comment_index: dict[str, dict] = {
        c["id"]: c for c in all_comments
        if isinstance(c, dict) and c.get("id")
    }
    children_map: dict[str, list[dict]] = {cid: [] for cid in comment_index.keys()}

    for c in all_comments:
        if not isinstance(c, dict):
            continue
        pid = c.get("parent_id")
        cid = c.get("id")
        if not cid:
            continue
        if pid and pid != "0" and pid in children_map:
            children_map[pid].append(c)

    structured_comments = []
    for c in all_comments:
        if not isinstance(c, dict):
            continue
        if c.get("parent_id") != "0":
            continue

        structured_comments.append({
            "commentId": c.get("id"),
            "author": c.get("sender_fullname"),
            "text": c.get("content"),
            "date": c.get("created_date") or c.get("published_date"),
            "vote_react_list": {
                "likes": c.get("likes"),
                "loves": c.get("loves"),
                "hahas": c.get("hahas"),
                "sads": c.get("sads"),
                "wows": c.get("wows"),
                "wraths": c.get("wraths"),
                "stars": c.get("stars"),
                "reactions_raw": c.get("reactions"),
            },
            "replies": [
                {
                    "commentId": r.get("id"),
                    "author": r.get("sender_fullname"),
                    "text": r.get("content"),
                    "date": r.get("created_date") or r.get("published_date"),
                    "vote_react_list": {
                        "likes": r.get("likes"),
                        "loves": r.get("loves"),
                        "hahas": r.get("hahas"),
                        "sads": r.get("sads"),
                        "wows": r.get("wows"),
                        "wraths": r.get("wraths"),
                        "stars": r.get("stars"),
                        "reactions_raw": r.get("reactions"),
                    },
                }
                for r in children_map.get(c.get("id"), [])
                if isinstance(r, dict)
            ],
        })

    total_comment_objects = len(all_comments)
    return structured_comments, total_comment_objects


def get_comment_count_only(article_url: str, category_name: str) -> int:
    """
    Fetch only the total number of comments for an article.

    This is used during the search phase when we only need a candidate
    with at least 20 comments, without saving the full JSON, images, or audio.
    """
    resp = safe_get(article_url)
    if not resp:
        return 0

    soup = BeautifulSoup(resp.text, "html.parser")

    meta = extract_article_metadata(soup, article_url, category_name)
    post_id = meta.get("postId") or extract_article_id_from_url(article_url)
    if not post_id:
        logging.warning(
            "Could not determine postId (comment-count only) for %s",
            article_url
        )
        return 0

    _, total_comment_count = fetch_comments_for_post(post_id)
    logging.info(
        "Comment-count-only: post %s has %d comments",
        post_id, total_comment_count
    )
    return total_comment_count
