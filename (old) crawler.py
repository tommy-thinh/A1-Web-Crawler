import os
import re
import json
import time
import logging
from urllib.parse import urljoin, urlparse, unquote
from urllib import robotparser

import requests
from bs4 import BeautifulSoup

# -----------------------------
# CONFIG SECTION – EDIT HERE
# -----------------------------

CATEGORY_CONFIG = [
    {"name": "Thế giới", "url": "https://tuoitre.vn/the-gioi.htm", "target_posts": 2},
    {"name": "Công nghệ", "url": "https://tuoitre.vn/cong-nghe.htm", "target_posts": 2},
    {"name": "Thời sự", "url": "https://tuoitre.vn/thoi-su.htm", "target_posts": 2}
]

TIMELINE_MAPPING_FILE = "category_timeline_mapping.json"

# Politeness settings
REQUEST_DELAY_SECS = 1.0       # delay between HTTP requests
COMMENT_REQUEST_DELAY_SECS = 0.5

# Comment API appKey (decoded from observed network requests)
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

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(IMAGES_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})

# First timeline page to start from (older pages usually have more comments)
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


# -----------------------------
# BUILD MAPPING
# -----------------------------
def article_has_category(html: str, target_cat_path: str) -> bool:
    """
    Return True if this article really belongs to the target category
    (e.g. target_cat_path = '/khoa-hoc.htm').

    Priority:
      1. Use JS variable _ADM_Channel (e.g. '%2Fkhoa-hoc%2Fdetail%2F')
      2. Fallback to an article-level breadcrumb/category block.
    """

    # Parse HTML into BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    # Normalize target: '/khoa-hoc.htm' -> 'khoa-hoc'
    target_slug = target_cat_path.strip("/").replace(".htm", "")

    # 1) Try to get category from _ADM_Channel inside <script> tags
    for script in soup.find_all("script"):
        txt = script.string or script.get_text(" ", strip=False)
        if not txt:
            continue

        m = re.search(r"_ADM_Channel\s*=\s*'([^']+)'", txt)
        if m:
            # Example value: '%2Fkhoa-hoc%2Fdetail%2F'
            decoded = unquote(m.group(1))                    # '/khoa-hoc/detail/'
            first_seg = decoded.strip("/").split("/", 1)[0]  # 'khoa-hoc'
            return first_seg == target_slug

    # 2) Fallback: article-level category/breadcrumb (not the global header nav)
    cat_link = soup.select_one(
        ".detail-cate a, .detail__category a, .breadcrumb a.active"
    )
    if cat_link and cat_link.has_attr("href"):
        path = urlparse(cat_link["href"]).path
        return path == target_cat_path

    # If we can't tell, assume it doesn't match
    return False


def get_zone_id_for_article(article_url: str, target_cat_path: str) -> int | None:
    """
    Given an article URL and a target category path (e.g. '/the-gioi.htm'):

      1. Fetch the article HTML.
      2. Check if the article really belongs to that category.
      3. If it does, call the comment API with pagesize=1 to grab one comment.
      4. If the comment contains zone_id, return it.

    Returns None if anything fails and lets the caller try another article.
    """
    # Step 1: fetch article page
    article_resp = safe_get(article_url)
    if not article_resp:
        return None

    html = article_resp.text

    # Step 2: verify that this article is tagged with the desired category
    if not article_has_category(html, target_cat_path):
        logging.info(
            "Article %s does not appear to belong to category %s (no matching category logic), skipping.",
            article_url, target_cat_path
        )
        return None

    # Step 3: extract post ID and call comment API
    post_id = extract_article_id_from_url(article_url)
    if not post_id:
        logging.warning("Cannot extract postId from URL when getting zone_id: %s", article_url)
        return None

    base_url = "https://id.tuoitre.vn/api/getlist-comment.api"
    params = {
        "pageindex": 1,
        "pagesize": 1,          # only need one comment to read zone_id
        "objId": post_id,
        "objType": 1,
        "objectpopupid": "",
        "sort": 2,
        "commentid": "",
        "command": "",
        "appKey": COMMENT_APP_KEY,
    }

    resp = safe_get(base_url, params=params)
    if not resp:
        return None

    try:
        outer = resp.json()
        data_str = outer.get("Data", "[]")
        comments = json.loads(data_str)
    except Exception as e:
        logging.warning("Failed to parse comments when getting zone_id for %s: %s", article_url, e)
        return None

    # Require at least one comment
    if not comments:
        logging.info(
            "No comments found for %s when trying to get zone_id for category %s (skipping).",
            article_url, target_cat_path
        )
        return None

    first = comments[0]
    zone_id = first.get("zone_id")
    if zone_id is None:
        logging.info("No zone_id in first comment object for %s (skipping).", article_url)
        return None

    logging.info("zone_id for %s (category %s) is %s", article_url, target_cat_path, zone_id)
    return int(zone_id)


def discover_zone_id_for_category(category_url: str, max_articles_to_try: int = 20) -> int | None:
    """
    Try to discover the timeline id (zone_id) for a category URL, e.g. /the-gioi.htm:

      - Load the category page (which may contain mixed-category content).
      - Extract candidate article links.
      - For each candidate, up to max_articles_to_try:
          + Check that it really belongs to this category.
          + If it has comments, read zone_id from the comment API.
      - Return the first valid zone_id we find.

    This avoids using zone_id from featured posts belonging to other categories.
    """
    resp = safe_get(category_url)
    if not resp:
        logging.warning("Cannot fetch category page to discover zone_id: %s", category_url)
        return None

    candidate_links = extract_post_links_from_category_html(resp.text, category_url)
    if not candidate_links:
        logging.warning("No candidate article links found on category page %s", category_url)
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
            # Found a zone_id from an article that belongs to this category and has comments
            return zone_id

    logging.warning(
        "Failed to discover zone_id for %s after trying %d articles",
        category_url,
        tried,
    )
    return None


def build_timeline_mapping(category_configs, mapping_file: str = TIMELINE_MAPPING_FILE) -> dict:
    """
    Build a mapping from category path (e.g. '/thoi-su.htm') to timeline id (e.g. 3)
    and save it as JSON.

    If mapping_file already exists, load and return it instead.
    """
    if os.path.exists(mapping_file):
        logging.info("Loading existing timeline mapping from %s", mapping_file)
        with open(mapping_file, "r", encoding="utf-8") as f:
            return json.load(f)

    mapping = {}
    for cfg in category_configs:
        url = cfg["url"]
        parsed = urlparse(url)
        cat_path = parsed.path  # e.g. '/thoi-su.htm'

        logging.info("Discovering timeline id for category %s (%s)", cfg.get("name", cat_path), url)
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
    Load the category -> timeline mapping from disk.
    If it does not exist, build a fresh mapping.
    """
    if not os.path.exists(mapping_file):
        logging.warning("Timeline mapping file %s not found, building a new one.", mapping_file)
        return build_timeline_mapping(CATEGORY_CONFIG, mapping_file)
    with open(mapping_file, "r", encoding="utf-8") as f:
        return json.load(f)


# -----------------------------
# Helper: robots.txt
# -----------------------------

def init_robots_parser():
    rp = robotparser.RobotFileParser()
    try:
        rp.set_url(ROBOTS_URL)
        rp.read()
        logging.info("Loaded robots.txt from %s", ROBOTS_URL)
    except Exception as e:
        logging.warning("Failed to read robots.txt: %s", e)
    return rp


robots_parser = init_robots_parser()


def can_fetch(url: str) -> bool:
    """
    Check robots.txt. If robots.txt cannot be read, fall back to allowing the request.
    """
    if not robots_parser:
        return True
    try:
        return robots_parser.can_fetch(USER_AGENT, url)
    except Exception:
        return True


# -----------------------------
# Generic HTTP helpers
# -----------------------------

def safe_get(url: str, params=None, stream: bool = False):
    """
    Wrapper around requests.get that respects robots.txt, adds a delay,
    and catches basic network errors.
    """
    if not can_fetch(url):
        logging.warning("Blocked by robots.txt: %s", url)
        return None

    try:
        logging.info("GET %s", url)
        resp = session.get(url, params=params, timeout=15, stream=stream)
        time.sleep(REQUEST_DELAY_SECS)
        resp.raise_for_status()
        return resp
    except Exception as e:
        logging.warning("Request failed (%s): %s", url, e)
        return None


def get_comment_count_only(article_url: str, category_name: str) -> int:
    """
    Fetch an article and count its total number of comments only.
    No JSON, images or audio are saved.

    Used in the search phase when we already stored (n-1) posts
    but still have not found any post with > 20 comments.
    """
    resp = safe_get(article_url)
    if not resp:
        return 0

    soup = BeautifulSoup(resp.text, "html.parser")

    # Reuse metadata extraction to get postId; ignore other fields here.
    meta = extract_article_metadata(soup, article_url, category_name)
    post_id = meta.get("postId") or extract_article_id_from_url(article_url)
    if not post_id:
        logging.warning("Could not determine postId (comment-count only) for %s", article_url)
        return 0

    _, total_comment_count = fetch_comments_for_post(post_id)
    logging.info("Comment-count-only: post %s has %d comments", post_id, total_comment_count)
    return total_comment_count


# -----------------------------
# URL & ID utilities
# -----------------------------

ARTICLE_ID_PATTERN = re.compile(r"-([0-9]{10,})\.htm")

def extract_article_id_from_url(url: str):
    """
    Extract numeric article ID from URLs like:
      https://tuoitre.vn/some-slug-20251206081858265.htm
    """
    m = ARTICLE_ID_PATTERN.search(url)
    if m:
        return m.group(1)
    return None


def normalize_url(href: str, base: str = BASE_DOMAIN) -> str:
    """
    Convert relative links to absolute tuoitre.vn URLs.
    """
    href = href.strip()
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return urljoin(base, href)


def extract_post_links_from_timeline_html(html: str, category_url: str):
    """
    Extract article URLs from a /timeline/<zone_id>/trang-N.htm page,
    but only for the given logical category (e.g. /thoi-su.htm).

    Structure (from trang-1.htm):
      <div class="box-category-item">
        ...
        <a class="box-category-category" href="https://tuoitre.vn/thoi-su.htm">Thời sự</a>
        ...
        <a class="box-category-link-title" data-id="2025..." href="https://tuoitre.vn/...-2025....htm">
          ...
        </a>
      </div>
    """
    soup = BeautifulSoup(html, "html.parser")
    links = set()

    parsed_cat = urlparse(category_url)
    target_cat_path = parsed_cat.path  # e.g. '/thoi-su.htm'

    for box in soup.find_all("div", class_="box-category-item"):
        cat_link = box.find("a", class_="box-category-category", href=True)
        if not cat_link:
            continue

        cat_href = cat_link["href"]
        cat_path = urlparse(cat_href).path

        # Only keep items whose category link matches the category we are crawling
        if cat_path != target_cat_path:
            continue

        title_link = box.find("a", class_="box-category-link-title", href=True)
        if not title_link:
            continue

        article_url = normalize_url(title_link["href"], base=BASE_DOMAIN)
        links.add(article_url)

    logging.info("From timeline page for %s found %d article links", target_cat_path, len(links))
    return sorted(links)


# -----------------------------
# Category page parsing
# -----------------------------

def extract_post_links_from_category_html(html: str, category_url: str):
    """
    Extract candidate article links from a category page using a simple heuristic:
      - scan all <a href="..."> links
      - keep ones that look like article URLs (long numeric ID before .htm).
    """
    soup = BeautifulSoup(html, "html.parser")
    links = set()

    for a in soup.find_all("a", href=True):
        href = normalize_url(a["href"], base=BASE_DOMAIN)
        # Only keep tuoitre.vn page links
        if "tuoitre.vn" not in href:
            continue
        # Heuristic: article URLs have a long numeric ID before .htm
        if ARTICLE_ID_PATTERN.search(href):
            links.add(href)

    logging.info("Found %d candidate article links on %s", len(links), category_url)
    return sorted(links)


def build_category_page_url(base_url: str, page_index: int) -> str:
    """
    Build the URL for a given category page index.

    Typical pattern:
      thoi-su.htm
      thoi-su/trang-2.htm
      thoi-su/trang-3.htm
    """
    if page_index == 1:
        return base_url
    # Insert "/trang-{page_index}" before ".htm"
    if base_url.endswith(".htm"):
        return base_url.replace(".htm", f"/trang-{page_index}.htm")
    # Fallback: append "trang-{n}.htm"
    if base_url.endswith("/"):
        return base_url + f"trang-{page_index}.htm"
    return base_url.rstrip("/") + f"/trang-{page_index}.htm"


# -----------------------------
# Article page parsing
# -----------------------------

def extract_article_metadata(soup: BeautifulSoup, article_url: str, category_name: str):
    """
    Extract title, author, date and main content from the article HTML.
    Uses a mix of meta tags and common content containers.
    """
    # Title: try <h1>, then og:title
    title_tag = soup.find("h1")
    if title_tag and title_tag.get_text(strip=True):
        title = title_tag.get_text(strip=True)
    else:
        og_title = soup.find("meta", attrs={"property": "og:title"})
        title = og_title["content"].strip() if og_title and og_title.get("content") else ""

    # Author: try some common patterns
    author = ""
    author_tag = soup.find(class_=re.compile(r"author", re.IGNORECASE))
    if author_tag:
        author = author_tag.get_text(strip=True)
    else:
        meta_author = soup.find("meta", attrs={"name": "author"})
        if meta_author and meta_author.get("content"):
            author = meta_author["content"].strip()

    # Date: prefer meta tags
    date = ""
    meta_date = soup.find("meta", attrs={"property": "article:published_time"})
    if meta_date and meta_date.get("content"):
        date = meta_date["content"].strip()
    else:
        # fallback: any element with a "date" class
        date_tag = soup.find(class_=re.compile(r"date", re.IGNORECASE))
        if date_tag:
            date = date_tag.get_text(strip=True)

    # Main content: try a few likely containers
    content = ""
    content_candidates = [
        soup.find("div", class_=re.compile(r"content", re.IGNORECASE)),
        soup.find("article"),
    ]
    for c in content_candidates:
        if c:
            content = c.get_text(separator="\n", strip=True)
            if content:
                break

    return {
        "postId": extract_article_id_from_url(article_url),
        "title": title,
        "content": content,
        "author": author,
        "date": date,
        "category": category_name,
        "url": article_url,
    }


def build_tts_audio_url(post_id: str, publish_date: str) -> str:
    """
    Construct the TTS audio URL using the fixed pattern:
      https://tts.mediacdn.vn/YYYY/MM/DD/tuoitre-nu-1-POST_ID.m4a

    publish_date must be in format YYYY-MM-DD or YYYY/MM/DD.
    """
    # Normalize publish date
    date = publish_date.replace("-", "/")  # convert YYYY-MM-DD → YYYY/MM/DD
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
    Download all <img> tags inside the article content and save them under
    ./images/<post_id>/. Keeps original filenames where possible and skips
    site-wide assets such as logos, banners, avatars, and default author images.
    """
    image_folder = os.path.join(IMAGES_DIR, post_id)
    os.makedirs(image_folder, exist_ok=True)

    image_paths = []

    for img in soup.find_all("img", src=True):
        src = img["src"].strip()
        if not src:
            continue

        # Skip base64/inlined images
        if src.startswith("data:"):
            continue

        src_lower = src.lower()

        # Skip irrelevant images
        if any(p in src_lower for p in IGNORE_IMAGE_PATTERNS):
            logging.info("Skipping irrelevant image: %s", src)
            continue

        # Build full URL
        img_url = normalize_url(src, base=BASE_DOMAIN)

        parsed = urlparse(img_url)
        filename = os.path.basename(parsed.path)
        if not filename:
            filename = f"{len(image_paths)+1}.jpg"

        local_path = os.path.join(image_folder, filename)

        # Avoid re-downloading
        if os.path.exists(local_path):
            image_paths.append(local_path)
            continue

        # Download image
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


# -----------------------------
# Reactions for the article
# -----------------------------

def fetch_article_reactions(post_id: str) -> dict:
    """
    Fetch reactions for an article using:
      https://s5.tuoitre.vn/showvote-reaction.htm?newsid=<post_id>&m=viewreact

    Typical response:
      {"Success": true, "Data": [{...}, {...}]}

    Data can also be null, or the response can be an error page.
    In all unexpected cases, returns {}.
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

    # Expect a dict with "Data". If not, treat as empty.
    if not isinstance(data, dict):
        logging.warning(
            "Unexpected reaction JSON type for post %s: %r",
            post_id, type(data)
        )
        return {}

    raw_items = data.get("Data")
    # Data can be null or missing; normalize to empty
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


# -----------------------------
# Comments & replies
# -----------------------------

def fetch_comments_for_post(post_id: str, pagesize: int = 20, max_pages: int = 100):
    """
    Fetch comments for a post using:
      https://id.tuoitre.vn/api/getlist-comment.api?pageindex=1&pagesize=...&objId=...

    The API returns JSON with "Data" as a JSON string containing a list of
    comment objects. Non-dict or None entries are skipped.
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

        # Append everything we got (can include None / unexpected entries)
        all_raw_comments.extend(comments_page)

    # -------------------------
    # Clean + deduplicate
    # -------------------------
    dedup: dict[str, dict] = {}
    for c in all_raw_comments:
        # Skip None or non-dict entries
        if not isinstance(c, dict):
            continue
        cid = c.get("id")
        if cid and cid not in dedup:
            dedup[cid] = c

    all_comments = list(dedup.values())

    # Build index only from valid dicts with ids
    comment_index: dict[str, dict] = {
        c["id"]: c for c in all_comments
        if isinstance(c, dict) and c.get("id")
    }
    # Prepare child mapping
    children_map: dict[str, list[dict]] = {cid: [] for cid in comment_index.keys()}

    # Attach children to parents
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


# -----------------------------
# Per-article crawling
# -----------------------------

def normalize_publish_date(raw: str) -> str | None:
    """
    Convert timestamps like '2025-12-04T14:19:57+07:00'
    into '2025-12-04' (YYYY-MM-DD).
    """
    if not raw:
        return None

    raw = raw.strip()

    # Typical TuoiTre format: '2025-12-04T14:19:57+07:00'
    if "T" in raw:
        date_part = raw.split("T", 1)[0]  # -> '2025-12-04'
    else:
        # Fallback: take first token as date
        date_part = raw.split(" ", 1)[0]

    # Light sanity check: 'YYYY-MM-DD'
    if len(date_part) == 10 and date_part[4] == "-" and date_part[7] == "-":
        return date_part

    return None


def crawl_single_article(article_url: str, category_name: str):
    """
    Fetch a single article and collect:
      - metadata
      - audio URL/file
      - images
      - reactions
      - comments and replies
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

    # Audio (deterministic TTS URL)
    publish_date = normalize_publish_date(meta.get("date"))
    audio_local_path = None
    audio_url = None

    if publish_date:
        audio_url = build_tts_audio_url(post_id, publish_date)
        audio_local_path = download_tts_audio(post_id, publish_date)
    else:
        logging.info(
            "No publish date found for %s; skipping TTS audio generation.",
            post_id
        )

    # Images
    image_paths = download_images_from_article(soup, post_id)

    # Reactions (article level)
    vote_reactions = fetch_article_reactions(post_id)

    # Comments (including nested replies)
    comments, total_comment_count = fetch_comments_for_post(post_id)

    post_data = {
        **meta,
        "audio_podcast": audio_local_path or audio_url,
        "images": image_paths,
        "vote_reactions": vote_reactions,
        "comments": comments,
        "total_comment_count": total_comment_count,
    }

    # Save to JSON
    out_path = os.path.join(DATA_DIR, f"{post_id}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(post_data, f, ensure_ascii=False, indent=2)

    logging.info(
        "Saved post %s (%s) with %d comments to %s",
        post_id, meta["title"], total_comment_count, out_path
    )

    return post_data, total_comment_count


# -----------------------------
# Per-category crawling logic
# -----------------------------

def crawl_category(
    category_cfg: dict,
    timeline_mapping: dict,
    found_post_with_20_global: bool,
    is_last_category: bool,
):
    """
    Category-level crawling with a global constraint on >= 20-comment posts.

    For every category:
      - Crawl timeline pages.
      - Save all posts retrieved (up to target_posts).
      - If any saved post has >= 20 comments, mark found_post_with_20_global = True.

    We no longer require each category to contain a >= 20-comment post.

    After finishing the normal crawl for a category:
      - If found_post_with_20_global is already True, just return.
      - Otherwise, if this is the last category:
          * enter a search phase:
              - keep crawling new posts beyond target_posts
              - do not save them yet
              - only fully crawl and save the first one with >= 20 comments.

    Returns (saved_posts, found_post_with_20_global).
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

    # -------------------------------
    # 1) NORMAL CRAWL PHASE
    # -------------------------------
    while True:
        # Stop normal saving when we reached target_posts for this category
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

            # Crawl and save every post in the normal phase
            post_data, comment_count = crawl_single_article(article_url, cat_name)
            if not post_data:
                continue

            saved_posts.append(post_data)

            # Update global flag if this post has >= 20 comments
            if comment_count is not None and comment_count >= 20:
                found_post_with_20_global = True

        page_index += 1

    logging.info(
        "Finished NORMAL phase for category %s: saved %d posts, "
        "found_post_with_20_global=%s",
        cat_name, len(saved_posts), found_post_with_20_global
    )

    # -------------------------------
    # 2) EXTRA SEARCH PHASE (last category only)
    #    - still no >=20-comment post
    # -------------------------------
    if not found_post_with_20_global and is_last_category:
        logging.info(
            "No >=20-comment post found in previous categories; "
            "entering SEARCH phase in last category %s",
            cat_name
        )

        # Continue crawling pages from where we left off
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

                # In search phase: first, only check comment count (no saving yet)
                comment_count = get_comment_count_only(article_url, cat_name)
                if comment_count is None:
                    comment_count = 0

                if comment_count >= 20:
                    # Now fully crawl and save this one post
                    post_data, final_cc = crawl_single_article(article_url, cat_name)
                    if post_data:
                        saved_posts.append(post_data)
                        found_post_with_20_global = True
                        logging.info(
                            "Found >=20-comment post in last category %s: %s with %d comments",
                            cat_name, article_url, final_cc
                        )
                        break  # done with search phase

            page_index += 1

        logging.info(
            "Finished SEARCH phase for last category %s. "
            "found_post_with_20_global=%s",
            cat_name, found_post_with_20_global
        )

    # -------------------------------
    # Done for this category
    # -------------------------------
    logging.info(
        "Finished category %s: total saved posts=%d, "
        "found_post_with_20_global=%s",
        cat_name, len(saved_posts), found_post_with_20_global
    )

    return saved_posts, found_post_with_20_global


# -----------------------------
# Main entry
# -----------------------------

def main():
    # Build or load mapping: category path -> timeline id
    timeline_mapping = load_timeline_mapping(TIMELINE_MAPPING_FILE)

    all_posts = []
    # Global flag: whether we already saw a post with >= 20 comments
    found_post_with_20_global = False

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
