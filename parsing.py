import re
import logging
from urllib.parse import urljoin, urlparse, unquote

from bs4 import BeautifulSoup

from config import BASE_DOMAIN

# -----------------------------
# URL & ID utilities
# -----------------------------

ARTICLE_ID_PATTERN = re.compile(r"-([0-9]{10,})\.htm")


def extract_article_id_from_url(url: str):
    """
    Extract numeric article ID from URL like:
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


# -----------------------------
# Article/category / timeline parsing
# -----------------------------

def article_has_category(html: str, target_cat_path: str) -> bool:
    """
    Return True iff this article really belongs to the target category
    (e.g. target_cat_path = '/khoa-hoc.htm').

    Priority:
      1. Use JS variable _ADM_Channel (e.g. '%2Fkhoa-hoc%2Fdetail%2F')
      2. Fallback to an article-level breadcrumb/category block.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Normalize target: '/khoa-hoc.htm' -> 'khoa-hoc'
    target_slug = target_cat_path.strip("/").replace(".htm", "")

    # 1) Look for `_ADM_Channel` in <script> tags
    for script in soup.find_all("script"):
        txt = script.string or script.get_text(" ", strip=False)
        if not txt:
            continue

        m = re.search(r"_ADM_Channel\s*=\s*'([^']+)'", txt)
        if m:
            # Example: '%2Fkhoa-hoc%2Fdetail%2F'
            decoded = unquote(m.group(1))                     # '/khoa-hoc/detail/'
            first_seg = decoded.strip("/").split("/", 1)[0]   # 'khoa-hoc'
            return first_seg == target_slug

    # 2) Fallback: article-level category/breadcrumb (not global nav)
    cat_link = soup.select_one(
        ".detail-cate a, .detail__category a, .breadcrumb a.active"
    )
    if cat_link and cat_link.has_attr("href"):
        path = urlparse(cat_link["href"]).path
        return path == target_cat_path

    # If category cannot be determined, default to False
    return False


def extract_post_links_from_timeline_html(html: str, category_url: str):
    """
    Extract article URLs from a /timeline/<zone_id>/trang-N.htm page, but only
    for the given logical category (e.g. /thoi-su.htm).

    Structure (from trang-1.htm):
      <div class="box-category-item">
        ...
        <a class="box-category-category" href="https://tuoitre.vn/thoi-su.htm" ...>Thời sự</a>
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

    logging.info(
        "From timeline page for %s found %d article links",
        target_cat_path, len(links)
    )
    return sorted(links)


def extract_post_links_from_category_html(html: str, category_url: str):
    """
    Extract candidate article links from a category page.

    Strategy:
      - find all <a href="...">,
      - keep ones that look like article URLs (have a long numeric ID + .htm).
    """
    soup = BeautifulSoup(html, "html.parser")
    links = set()

    for a in soup.find_all("a", href=True):
        href = normalize_url(a["href"], base=BASE_DOMAIN)
        if "tuoitre.vn" not in href:
            continue
        if ARTICLE_ID_PATTERN.search(href):
            links.add(href)

    logging.info("Found %d candidate article links on %s", len(links), category_url)
    return sorted(links)


def build_category_page_url(base_url: str, page_index: int) -> str:
    """
    Guess pagination scheme for tuoitre.vn categories.
    Pattern:
      thoi-su.htm
      thoi-su/trang-2.htm
      thoi-su/trang-3.htm
    """
    if page_index == 1:
        return base_url
    if base_url.endswith(".htm"):
        return base_url.replace(".htm", f"/trang-{page_index}.htm")
    if base_url.endswith("/"):
        return base_url + f"trang-{page_index}.htm"
    return base_url.rstrip("/") + f"/trang-{page_index}.htm"


def extract_article_metadata(soup: BeautifulSoup, article_url: str, category_name: str):
    """
    Extract title, author, date, and main content from article HTML.
    Uses simple heuristics and meta tags.
    """
    # Title: try <h1>, then og:title
    title_tag = soup.find("h1")
    if title_tag and title_tag.get_text(strip=True):
        title = title_tag.get_text(strip=True)
    else:
        og_title = soup.find("meta", attrs={"property": "og:title"})
        title = og_title["content"].strip() if og_title and og_title.get("content") else ""

    # Author: try common patterns
    author = ""
    author_tag = soup.find(class_=re.compile(r"author", re.IGNORECASE))
    if author_tag:
        author = author_tag.get_text(strip=True)
    else:
        meta_author = soup.find("meta", attrs={"name": "author"})
        if meta_author and meta_author.get("content"):
            author = meta_author["content"].strip()

    # Date: try meta tags first
    date = ""
    meta_date = soup.find("meta", attrs={"property": "article:published_time"})
    if meta_date and meta_date.get("content"):
        date = meta_date["content"].strip()
    else:
        # fallback: look for an element with class containing "date"
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


def normalize_publish_date(raw: str) -> str | None:
    """
    Convert things like '2025-12-04T14:19:57+07:00' into '2025-12-04'.
    """
    if not raw:
        return None

    raw = raw.strip()

    if "T" in raw:
        date_part = raw.split("T", 1)[0]
    else:
        date_part = raw.split(" ", 1)[0]

    if len(date_part) == 10 and date_part[4] == "-" and date_part[7] == "-":
        return date_part

    return None
