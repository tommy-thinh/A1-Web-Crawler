import time
import logging
from urllib import robotparser

import requests

from config import (
    USER_AGENT,
    ROBOTS_URL,
    REQUEST_DELAY_SECS,
)

# Shared requests session
session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})


# -----------------------------
# robots.txt helper
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
    Check robots.txt. If robots cannot be read, default to True.
    """
    if not robots_parser:
        return True
    try:
        return robots_parser.can_fetch(USER_AGENT, url)
    except Exception:
        return True


# -----------------------------
# Generic HTTP helper
# -----------------------------

def safe_get(url: str, params=None, stream: bool = False):
    """
    Wrapper around requests.get with robots.txt checking, delay, and error handling.
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
