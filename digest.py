#!/usr/bin/env python3
"""
digest.py — NewsDigest core
Fetches top articles for configured topics, extracts excerpts, and builds a digest.

Usage:
  python digest.py --run-once                  # fetch now and exit
  python digest.py --run-scheduler             # run daily on schedule (blocks)
  python digest.py --topics "AI" "Apple"       # override topics for this run
  python digest.py --export digest.html        # write HTML file
  python digest.py --config /path/config.json  # alternate config
  python digest.py -v                          # verbose logging
"""

import argparse
import copy
import datetime
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote_plus, urlparse, urlencode, parse_qs, urlunparse

import feedparser
import requests
import schedule
from bs4 import BeautifulSoup

# ── Paths ──────────────────────────────────────────────────────────────────────

_HERE         = Path(__file__).parent
CONFIG_PATH   = _HERE / "config.json"
DATA_DIR      = _HERE / "data"
HISTORY_PATH  = DATA_DIR / "history.json"
LATEST_PATH   = DATA_DIR / "latest.json"

HISTORY_MAX_DAYS = 30

# ── Default RSS feeds ──────────────────────────────────────────────────────────
# Articles are filtered by topic keyword match on title + summary.
# Add or remove feeds in config.json under "rss_feeds".

DEFAULT_RSS_FEEDS = [
    # Tech
    {"name": "TechCrunch",   "url": "https://techcrunch.com/feed/"},
    {"name": "The Verge",    "url": "https://www.theverge.com/rss/index.xml"},
    {"name": "Ars Technica", "url": "https://feeds.arstechnica.com/arstechnica/index"},
    {"name": "Wired",        "url": "https://www.wired.com/feed/rss"},
    {"name": "Hacker News",  "url": "https://hnrss.org/frontpage"},
    # General news
    {"name": "BBC News",     "url": "https://feeds.bbci.co.uk/news/rss.xml"},
    {"name": "Reuters",      "url": "https://feeds.reuters.com/reuters/topNews"},
    {"name": "AP News",      "url": "https://rsshub.app/apnews/topics/apf-topnews"},
]

# NewsAPI endpoint (used when newsapi_key is set)
NEWSAPI_URL = (
    "https://newsapi.org/v2/everything"
    "?q={query}&sortBy=publishedAt&pageSize={n}&language=en&apiKey={key}"
)

# UTM and tracking params stripped during URL canonicalization
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "source", "mc_cid", "mc_eid",
}

# ── Embedded CSS ───────────────────────────────────────────────────────────────

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; overflow: hidden; }
body {
    font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif;
    background: #f2f2f7;
    color: #1c1c1e;
    display: flex;
    flex-direction: column;
}

/* ── Layout ── */
.layout {
    display: flex;
    flex: 1;
    height: 100vh;
    overflow: hidden;
}

/* ── Sidebar ── */
.sidebar {
    width: 320px;
    min-width: 320px;
    background: #fff;
    border-right: 1px solid #e5e5ea;
    display: flex;
    flex-direction: column;
    overflow: hidden;
}
.sidebar-header {
    padding: 16px 16px 12px;
    border-bottom: 1px solid #e5e5ea;
    background: #fff;
    flex-shrink: 0;
}
.sidebar-header h1 {
    font-size: 20px;
    font-weight: 700;
    margin-bottom: 2px;
}
.sidebar-header .meta {
    font-size: 11px;
    color: #8e8e93;
    margin-bottom: 10px;
}
.sidebar-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
}
.refresh-btn {
    padding: 7px 16px;
    background: #007aff;
    color: #fff;
    border: none;
    border-radius: 16px;
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    transition: background .15s;
    white-space: nowrap;
}
.refresh-btn:hover { background: #0060df; }
.refresh-btn:disabled { background: #aeaeb2; cursor: default; }
.excerpt-loading { color: #8e8e93; font-style: italic; font-size: 14px; }
.settings-inline {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    color: #8e8e93;
}
.settings-inline label { font-weight: 500; color: #3a3a3c; white-space: nowrap; }
.settings-inline input[type=number] {
    width: 48px;
    padding: 4px 6px;
    border: 1px solid #d1d1d6;
    border-radius: 7px;
    font-size: 12px;
    text-align: center;
}
.settings-inline .save-btn {
    padding: 4px 10px;
    background: #34c759;
    color: #fff;
    border: none;
    border-radius: 10px;
    font-size: 12px;
    cursor: pointer;
}
.settings-inline .save-btn:hover { background: #28a745; }
.saved-msg { color: #34c759; font-weight: 500; font-size: 12px; display: none; }

/* ── Feed list (scrollable) ── */
.feed-list {
    flex: 1;
    overflow-y: auto;
    -webkit-overflow-scrolling: touch;
}
.feed-group { border-bottom: 1px solid #f2f2f7; }
.feed-group.collapsed .article-row { display: none; }
.feed-name {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: #8e8e93;
    padding: 8px 12px 8px 16px;
    background: #f9f9fb;
    position: sticky;
    top: 0;
    z-index: 1;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 6px;
    user-select: none;
}
.feed-name:hover { background: #efefef; }
.feed-toggle { font-size: 8px; color: #c7c7cc; flex-shrink: 0; transition: transform .2s; }
.feed-group:not(.collapsed) .feed-toggle { transform: rotate(90deg); }
.feed-label { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.feed-count { background: #e5e5ea; border-radius: 8px; padding: 1px 6px; font-size: 10px; color: #8e8e93; flex-shrink: 0; }
.feed-max-wrap { display: flex; align-items: center; gap: 3px; flex-shrink: 0; }
.feed-max-input {
    width: 34px; padding: 2px 4px;
    border: 1px solid #d1d1d6; border-radius: 5px;
    font-size: 11px; text-align: center; background: #fff;
}
.feed-max-save {
    padding: 2px 6px; background: #34c759; color: #fff;
    border: none; border-radius: 5px; font-size: 11px; cursor: pointer;
}
.feed-max-save:hover { background: #28a745; }
.article-row {
    padding: 10px 16px;
    border-bottom: 1px solid #f2f2f7;
    cursor: pointer;
    transition: background .1s;
    user-select: none;
}
.article-row:hover { background: #f0f4ff; }
.article-row.active { background: #e8f0fe; }
.article-row-title {
    font-size: 13px;
    font-weight: 500;
    color: #1c1c1e;
    line-height: 1.4;
    margin-bottom: 3px;
}
.article-row-meta {
    font-size: 11px;
    color: #8e8e93;
}
.empty-list {
    text-align: center;
    color: #8e8e93;
    padding: 48px 24px;
    font-size: 14px;
    line-height: 1.7;
}

/* ── Detail pane ── */
.detail-pane {
    flex: 1;
    overflow-y: auto;
    -webkit-overflow-scrolling: touch;
    background: #f2f2f7;
    display: flex;
    flex-direction: column;
}
.detail-empty {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    color: #8e8e93;
    font-size: 15px;
    padding: 48px;
    text-align: center;
    line-height: 1.7;
}
.detail-empty-icon { font-size: 48px; margin-bottom: 16px; }
.detail-content { display: none; flex-direction: column; flex-shrink: 0; padding: 32px 32px 0; max-width: 100%; }
.detail-content.visible { display: flex; }
.detail-nav {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 24px;
    flex-shrink: 0;
}
.back-btn {
    display: none;
    padding: 6px 14px;
    background: #e5e5ea;
    border: none;
    border-radius: 14px;
    font-size: 13px;
    cursor: pointer;
    color: #1c1c1e;
}
.back-btn:hover { background: #d1d1d6; }
.nav-btn {
    padding: 6px 14px;
    background: #007aff;
    color: #fff;
    border: none;
    border-radius: 14px;
    font-size: 13px;
    cursor: pointer;
    transition: background .15s;
}
.nav-btn:hover { background: #0060df; }
.nav-btn:disabled { background: #aeaeb2; cursor: default; }
.nav-counter { font-size: 13px; color: #8e8e93; flex: 1; text-align: center; }
.detail-feed {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: #007aff;
    margin-bottom: 10px;
}
.detail-title {
    font-size: 24px;
    font-weight: 700;
    line-height: 1.3;
    color: #1c1c1e;
    margin-bottom: 10px;
}
.detail-meta {
    font-size: 13px;
    color: #8e8e93;
    margin-bottom: 20px;
}
.detail-excerpt-wrap {
    background: #fff;
    border-radius: 12px;
    box-shadow: 0 1px 6px rgba(0,0,0,.08);
    padding: 24px 28px;
    margin-bottom: 20px;
    flex-shrink: 0;
}
.detail-excerpt-wrap p {
    font-size: 15px;
    line-height: 1.7;
    color: #3a3a3c;
    margin-bottom: 14px;
}
.detail-excerpt-wrap p:last-child { margin-bottom: 0; }
.detail-excerpt-wrap h2, .detail-excerpt-wrap h3, .detail-excerpt-wrap h4 {
    font-size: 16px;
    font-weight: 600;
    color: #1c1c1e;
    margin: 18px 0 8px;
    line-height: 1.4;
}
.detail-excerpt-wrap ul, .detail-excerpt-wrap ol {
    margin: 6px 0 14px 20px;
    font-size: 15px;
    line-height: 1.6;
    color: #3a3a3c;
}
.detail-excerpt-wrap li { margin-bottom: 5px; }
.detail-excerpt-wrap blockquote {
    border-left: 3px solid #007aff;
    padding: 4px 0 4px 14px;
    color: #636366;
    font-style: italic;
    margin: 12px 0;
    font-size: 15px;
    line-height: 1.6;
}
.detail-excerpt-wrap a { color: #007aff; text-decoration: none; }
.detail-excerpt-wrap a:hover { text-decoration: underline; }
.detail-excerpt-wrap strong, .detail-excerpt-wrap b { color: #1c1c1e; font-weight: 600; }
.detail-excerpt-empty {
    font-size: 14px;
    color: #aeaeb2;
    font-style: italic;
}
.read-btn {
    display: inline-block;
    padding: 11px 28px;
    background: #007aff;
    color: #fff;
    text-decoration: none;
    border-radius: 20px;
    font-size: 15px;
    font-weight: 500;
    transition: background .15s;
    flex-shrink: 0;
    align-self: flex-start;
    margin-bottom: 32px;
}
.read-btn:hover { background: #0060df; }

/* ── Mobile (<= 640px) ── */
@media (max-width: 640px) {
    html, body { overflow: hidden; }
    .layout { position: relative; overflow: hidden; }
    .sidebar {
        position: absolute;
        inset: 0;
        width: 100%;
        min-width: unset;
        transform: translateX(0);
        transition: transform .3s ease;
        z-index: 2;
    }
    .sidebar.hidden { transform: translateX(-100%); }
    .detail-pane {
        position: absolute;
        inset: 0;
        transform: translateX(100%);
        transition: transform .3s ease;
        z-index: 1;
    }
    .detail-pane.visible { transform: translateX(0); z-index: 3; }
    .back-btn { display: inline-block; }
    .detail-content { padding: 12px 12px 0; }
    .detail-title { font-size: 18px; }
}
"""

_JS = """
(function() {
    var ARTICLES   = [];
    var currentIdx = -1;

    var sidebar       = document.getElementById('sidebar');
    var detailPane    = document.getElementById('detail-pane');
    var detailContent = document.getElementById('detail-content');
    var detailEmpty   = document.getElementById('detail-empty');

    var detailFeed    = document.getElementById('detail-feed');
    var detailTitle   = document.getElementById('detail-title');
    var detailMeta    = document.getElementById('detail-meta');
    var detailExcerpt = document.getElementById('detail-excerpt');
    var detailLink    = document.getElementById('detail-link');
    var navCounter    = document.getElementById('nav-counter');
    var prevBtn       = document.getElementById('prev-btn');
    var nextBtn       = document.getElementById('next-btn');
    var backBtn       = document.getElementById('back-btn');
    var refreshBtn    = document.getElementById('refresh-btn');

    function isMobile() { return window.innerWidth <= 640; }

    function escHtml(s) {
        return String(s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    function setExcerpt(html) {
        if (!html) {
            detailExcerpt.innerHTML = '<p class="detail-excerpt-empty">No preview available.</p>';
            return;
        }
        // html is already sanitized server-side — render directly
        detailExcerpt.innerHTML = html;
    }

    function showArticle(idx) {
        if (idx < 0 || idx >= ARTICLES.length) return;
        currentIdx = idx;
        var a = ARTICLES[idx];

        document.querySelectorAll('.article-row').forEach(function(r) { r.classList.remove('active'); });
        var row = document.querySelector('.article-row[data-idx="' + idx + '"]');
        if (row) { row.classList.add('active'); row.scrollIntoView({block: 'nearest'}); }

        detailFeed.textContent  = a.feed || '';
        detailTitle.textContent = a.title || '';
        detailMeta.textContent  = [a.source, a.published].filter(Boolean).join(' \u00b7 ');
        detailLink.href         = a.url || '#';

        if (!a.excerpt) {
            // No RSS preview — fetch and extract from article URL on demand
            detailExcerpt.innerHTML = '<p class="excerpt-loading">Extracting article\u2026</p>';
            var fetchIdx = idx;
            fetch('/api/extract?url=' + encodeURIComponent(a.url))
                .then(function(r) { return r.json(); })
                .then(function(d) {
                    if (currentIdx !== fetchIdx) return;
                    if (d.text) { a.excerpt = d.text; }  // cache for this session
                    setExcerpt(d.text || '');
                })
                .catch(function() {
                    if (currentIdx !== fetchIdx) return;
                    detailExcerpt.innerHTML = '<p class="detail-excerpt-empty">Failed to load article.</p>';
                });
        } else {
            setExcerpt(a.excerpt);
        }

        navCounter.textContent = (idx + 1) + ' / ' + ARTICLES.length;
        prevBtn.disabled = (idx === 0);
        nextBtn.disabled = (idx === ARTICLES.length - 1);

        detailEmpty.style.display   = 'none';
        detailContent.style.display = 'flex';

        if (isMobile()) {
            sidebar.classList.add('hidden');
            detailPane.classList.add('visible');
        }
    }

    function showSidebar() {
        sidebar.classList.remove('hidden');
        detailPane.classList.remove('visible');
    }

    function bindFeedCollapse(nameEl) {
        nameEl.addEventListener('click', function() {
            nameEl.closest('.feed-group').classList.toggle('collapsed');
        });
    }

    function bindArticleRows(container) {
        container.querySelectorAll('.article-row').forEach(function(row) {
            row.addEventListener('click', function() {
                showArticle(parseInt(row.dataset.idx, 10));
            });
        });
    }

    function renderLiveSidebar(articles) {
        var feedList = document.querySelector('.feed-list');
        var groups = {}, order = [];
        articles.forEach(function(a, idx) {
            if (!groups[a.feed]) { groups[a.feed] = []; order.push(a.feed); }
            groups[a.feed].push({idx: idx, a: a});
        });

        if (order.length === 0) {
            feedList.innerHTML = '<div class="empty-list"><p>No articles found.</p></div>';
            return;
        }

        var html = '';
        order.forEach(function(feedName) {
            var items = groups[feedName];
            html += '<div class="feed-group collapsed">'
                  + '<div class="feed-name">'
                  + '<span class="feed-toggle">&#9654;</span>'
                  + '<span class="feed-label">' + escHtml(feedName) + '</span>'
                  + '<span class="feed-count">' + items.length + '</span>'
                  + '</div>';
            items.forEach(function(item) {
                var meta = [item.a.source, item.a.published].filter(Boolean).join(' \u00b7 ');
                html += '<div class="article-row" data-idx="' + item.idx + '">'
                      + '<div class="article-row-title">' + escHtml(item.a.title) + '</div>'
                      + '<div class="article-row-meta">' + escHtml(meta) + '</div>'
                      + '</div>';
            });
            html += '</div>';
        });

        feedList.innerHTML = html;
        feedList.querySelectorAll('.feed-name').forEach(bindFeedCollapse);
        bindArticleRows(feedList);
    }

    if (prevBtn) prevBtn.addEventListener('click', function() { showArticle(currentIdx - 1); });
    if (nextBtn) nextBtn.addEventListener('click', function() { showArticle(currentIdx + 1); });
    if (backBtn) backBtn.addEventListener('click', showSidebar);

    document.addEventListener('keydown', function(e) {
        if (e.key === 'ArrowDown' || e.key === 'j') showArticle(currentIdx + 1);
        if (e.key === 'ArrowUp'   || e.key === 'k') showArticle(currentIdx - 1);
        if (e.key === 'Escape') showSidebar();
    });

    // ── Load feeds ───────────────────────────────────────────────────────────
    function loadFeeds(forceRefresh) {
        var feedList = document.querySelector('.feed-list');
        var url = forceRefresh ? '/api/feeds?refresh=true' : '/api/feeds';

        if (forceRefresh) {
            // Keep old articles visible while refreshing in background
            if (refreshBtn) { refreshBtn.disabled = true; refreshBtn.textContent = 'Refreshing\u2026'; }
        } else {
            feedList.innerHTML = '<div class="empty-list"><p>Loading feeds\u2026</p></div>';
            currentIdx = -1;
            detailEmpty.style.display   = '';
            detailContent.style.display = 'none';
        }

        fetch(url)
            .then(function(r) { return r.json(); })
            .then(function(d) {
                ARTICLES = d.articles || [];
                renderLiveSidebar(ARTICLES);
                if (forceRefresh) {
                    currentIdx = -1;
                    detailEmpty.style.display   = '';
                    detailContent.style.display = 'none';
                }
                if (refreshBtn) { refreshBtn.disabled = false; refreshBtn.textContent = '\u21bb Refresh'; }
            })
            .catch(function() {
                if (!forceRefresh) {
                    feedList.innerHTML = '<div class="empty-list"><p>Failed to load feeds. Check server.</p></div>';
                }
                if (refreshBtn) { refreshBtn.disabled = false; refreshBtn.textContent = '\u21bb Refresh'; }
            });
    }

    if (refreshBtn) {
        refreshBtn.addEventListener('click', function() { loadFeeds(true); });
    }

    // ── Settings ─────────────────────────────────────────────────────────────
    var saveBtn  = document.getElementById('save-settings');
    var savedMsg = document.getElementById('saved-msg');
    var maxInput = document.getElementById('max-articles');

    fetch('/api/config').then(function(r) { return r.json(); }).then(function(d) {
        if (maxInput) maxInput.value = d.max_articles_per_topic;
    });

    if (saveBtn) {
        saveBtn.addEventListener('click', function() {
            var val = parseInt(maxInput.value, 10);
            if (!val || val < 1) return;
            fetch('/api/config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({max_articles_per_topic: val})
            }).then(function(r) { return r.json(); }).then(function() {
                if (savedMsg) { savedMsg.style.display = 'inline'; setTimeout(function() { savedMsg.style.display = 'none'; }, 2000); }
            });
        });
    }

    // ── Auto-load on start (uses cache if available) ─────────────────────────
    loadFeeds(false);
})();
"""


# ── Helpers ────────────────────────────────────────────────────────────────────

def esc(s) -> str:
    """HTML-escape a string."""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def canonical_url(url: str) -> str:
    """Strip tracking params, fragment, trailing slash; lowercase scheme+host."""
    try:
        p = urlparse(url.strip())
        qs = parse_qs(p.query, keep_blank_values=False)
        filtered = {k: v for k, v in qs.items() if k.lower() not in _TRACKING_PARAMS}
        new_query = urlencode(filtered, doseq=True)
        clean = urlunparse((
            p.scheme.lower(),
            p.netloc.lower(),
            p.path.rstrip("/") or "/",
            p.params,
            new_query,
            "",   # no fragment
        ))
        return clean
    except Exception:
        return url


_STOP_WORDS = {"a", "an", "the", "and", "or", "of", "in", "to", "for", "on", "at", "by"}

def _topic_matches(topic: str, title: str, summary: str) -> bool:
    """
    Return True if the topic matches title or summary.
    - Tries the full phrase first (e.g. "artificial intelligence")
    - Then tries any significant word from the topic (len >= 4, not a stop word)
      so "Swift programming" matches articles that mention "Swift".
    """
    haystack = (title + " " + summary).lower()
    needle   = topic.lower()

    if re.search(r'\b' + re.escape(needle) + r'\b', haystack):
        return True

    words = [w for w in needle.split() if len(w) >= 4 and w not in _STOP_WORDS]
    for word in words:
        if re.search(r'\b' + re.escape(word) + r'\b', haystack):
            return True

    return False


# ── History ────────────────────────────────────────────────────────────────────

def load_history() -> dict:
    """Return {canonical_url: {title, topic, date_seen}} or {} on any error."""
    try:
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_history(history: dict) -> None:
    """Prune old entries and write atomically."""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=HISTORY_MAX_DAYS)
    pruned = {
        url: meta for url, meta in history.items()
        if datetime.datetime.fromisoformat(
            meta.get("date_seen", "2000-01-01T00:00:00+00:00")
        ) >= cutoff
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = HISTORY_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(pruned, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(HISTORY_PATH)


def is_seen(url: str, history: dict) -> bool:
    return canonical_url(url) in history


def mark_seen(url: str, title: str, topic: str, history: dict) -> None:
    history[canonical_url(url)] = {
        "title":     title,
        "topic":     topic,
        "date_seen": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


# ── RSS feed fetcher ───────────────────────────────────────────────────────────

def _resolve_google_news_url(url: str, config: dict) -> str:
    """Follow a news.google.com redirect URL to get the real article URL."""
    try:
        resp = requests.head(
            url,
            allow_redirects=True,
            timeout=min(config.get("request_timeout_seconds", 10), 6),
            headers={"User-Agent": config.get("user_agent", "Mozilla/5.0")},
        )
        return resp.url or url
    except Exception:
        return url


def _fetch_one_feed(feed_cfg: dict, config: dict) -> list:
    """Fetch a single RSS feed and return all valid entries as article dicts."""
    feed_url  = feed_cfg.get("url", "")
    feed_name = feed_cfg.get("name", feed_url)
    ua        = config.get("user_agent", "Mozilla/5.0")
    timeout   = config.get("request_timeout_seconds", 10)
    try:
        resp = requests.get(feed_url, timeout=timeout, headers={"User-Agent": ua})
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
    except Exception as e:
        logging.debug("RSS feed %r failed: %s", feed_name, e)
        return []

    articles = []
    for entry in feed.entries:
        link = entry.get("link", "")
        if not link:
            continue
        # Google News links redirect to the real article — resolve them
        if "news.google.com" in link:
            link = _resolve_google_news_url(link, config)
        elif "google.com" in link:
            continue  # skip Google Search / other google.com links
        published = entry.get("published") or entry.get("updated") or ""
        articles.append({
            "title":       entry.get("title", "").strip(),
            "url":         link,
            "source":      feed_name,
            "published":   published,
            "fetched_via": "rss",
            "_summary":    BeautifulSoup(entry.get("summary", ""), "lxml").get_text(),
            # Full content from content:encoded if the feed provides it
            "_content_html": (
                (getattr(entry, "content", None) or [{}])[0].get("value", "")
                or entry.get("summary", "")
            ),
        })
    return articles


def fetch_rss_feeds(topic: str, max_articles: int, config: dict) -> list:
    """
    Fetch all configured RSS feeds, filter articles by topic keyword,
    and return up to max_articles matching articles with real URLs.
    """
    feeds     = config.get("rss_feeds", DEFAULT_RSS_FEEDS)
    candidates = []
    seen_urls: set = set()

    for feed_cfg in feeds:
        for a in _fetch_one_feed(feed_cfg, config):
            if not _topic_matches(topic, a["title"], a["_summary"]):
                continue
            c = canonical_url(a["url"])
            if c in seen_urls:
                continue
            seen_urls.add(c)
            candidates.append(a)
        if len(candidates) >= max_articles * 2:
            break

    logging.info("  RSS feeds [%s]: %d articles matched", topic, len(candidates))
    return candidates[:max_articles]


def fetch_all_feeds(config: dict, history: dict) -> list[dict]:
    """
    No-topics mode: fetch every article from every configured feed,
    deduplicate against history, group by feed name.
    Returns list of {topic (feed name), articles} — same shape as topics mode.
    """
    feeds        = config.get("rss_feeds", DEFAULT_RSS_FEEDS)
    global_max_n = config.get("max_articles_per_topic", 5)
    by_feed: dict[str, list] = {}
    seen_urls: set = set()

    for feed_cfg in feeds:
        # Per-feed override, falls back to global setting
        max_n     = feed_cfg.get("max_articles", global_max_n)
        feed_name = feed_cfg.get("name", feed_cfg.get("url", ""))
        articles  = []
        for a in _fetch_one_feed(feed_cfg, config):
            c = canonical_url(a["url"])
            if c in seen_urls or is_seen(a["url"], history):
                continue
            seen_urls.add(c)
            articles.append(a)
            if len(articles) >= max_n:
                break
        if articles:
            by_feed[feed_name] = articles
        logging.info("  %s: %d new articles", feed_name, len(articles))

    return [{"topic": name, "articles": arts} for name, arts in by_feed.items()]


def fetch_all_feeds_live(config: dict) -> list[dict]:
    """
    Fetch all configured RSS feeds in parallel — no history filter.
    Returns a flat list of article dicts in feed-config order.
    """
    feeds        = config.get("rss_feeds", DEFAULT_RSS_FEEDS)
    global_max_n = config.get("max_articles_per_topic", 5)

    def _fetch_feed(feed_cfg: dict) -> tuple[str, list]:
        max_n     = feed_cfg.get("max_articles", global_max_n)
        feed_name = feed_cfg.get("name", feed_cfg.get("url", ""))
        articles  = []
        for a in _fetch_one_feed(feed_cfg, config):
            rss_preview = html_to_excerpt(a.get("_content_html", ""), max_chars=2000)
            articles.append({
                "feed":      feed_name,
                "title":     a["title"],
                "url":       a["url"],
                "source":    a["source"],
                "published": _fmt_date(a.get("published", "")),
                "excerpt":   rss_preview,
            })
            if len(articles) >= max_n:
                break
        return feed_name, articles

    # Fetch all feeds concurrently (up to 8 at a time)
    by_feed: dict[str, list] = {}
    with ThreadPoolExecutor(max_workers=min(len(feeds), 8)) as pool:
        futures = {pool.submit(_fetch_feed, fc): fc for fc in feeds}
        for future in as_completed(futures):
            try:
                feed_name, articles = future.result()
                by_feed[feed_name] = articles
            except Exception as e:
                logging.debug("Feed fetch error: %s", e)

    # Assemble in original config order, deduplicate URLs
    result: list[dict] = []
    seen_urls: set = set()
    for feed_cfg in feeds:
        feed_name = feed_cfg.get("name", feed_cfg.get("url", ""))
        for a in by_feed.get(feed_name, []):
            c = canonical_url(a["url"])
            if c not in seen_urls:
                seen_urls.add(c)
                result.append(a)

    logging.info("Live fetch: %d articles from %d feeds", len(result), len(feeds))
    return result


# ── NewsAPI ────────────────────────────────────────────────────────────────────

def fetch_newsapi(topic: str, max_articles: int, config: dict) -> list:
    key = config.get("newsapi_key", "").strip()
    if not key:
        return []
    url = NEWSAPI_URL.format(
        query=quote_plus(topic),
        n=max_articles,
        key=key,
    )
    try:
        resp = requests.get(
            url,
            timeout=config.get("request_timeout_seconds", 10),
            headers={"User-Agent": config.get("user_agent", "NewsDigest/1.0")},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logging.warning("NewsAPI failed for %r: %s", topic, e)
        return []

    articles = []
    for item in data.get("articles", [])[:max_articles]:
        url_val = item.get("url", "")
        if not url_val or url_val == "https://removed.com":
            continue
        articles.append({
            "title":       (item.get("title") or "").strip(),
            "url":         url_val,
            "source":      (item.get("source") or {}).get("name", ""),
            "published":   item.get("publishedAt", ""),
            "fetched_via": "newsapi",
        })

    logging.info("  NewsAPI [%s]: %d articles", topic, len(articles))
    return articles


# ── Per-topic orchestration ────────────────────────────────────────────────────

def fetch_articles_for_topic(topic: str, config: dict, history: dict) -> list:
    max_n = config.get("max_articles_per_topic", 5)

    rss_results = fetch_rss_feeds(topic, max_n, config)

    # Deduplicate within this batch and against history
    seen_in_batch: set = set()
    combined = []
    for a in rss_results:
        c = canonical_url(a["url"])
        if c not in seen_in_batch and not is_seen(a["url"], history):
            seen_in_batch.add(c)
            combined.append(a)

    # Fill remaining slots from NewsAPI if key is configured
    remaining = max_n - len(combined)
    if remaining > 0 and config.get("newsapi_key", "").strip():
        api_results = fetch_newsapi(topic, remaining + 3, config)
        for a in api_results:
            c = canonical_url(a["url"])
            if c not in seen_in_batch and not is_seen(a["url"], history):
                seen_in_batch.add(c)
                combined.append(a)
                if len(combined) >= max_n:
                    break

    return combined[:max_n]


# ── Excerpt extraction ─────────────────────────────────────────────────────────

_SAFE_INLINE = frozenset({"strong", "em", "b", "i", "a", "code", "mark", "br"})


def _clean_inline_html(tag) -> str:
    """Return safe inner HTML for a tag, keeping only inline formatting tags."""
    t = copy.deepcopy(tag)
    for child in t.find_all(True):
        if child.name in _SAFE_INLINE:
            if child.name == "a":
                href = child.get("href", "")
                child.attrs = {}
                if href and href.startswith(("http://", "https://")):
                    child["href"] = href
                    child["target"] = "_blank"
                    child["rel"] = "noopener noreferrer"
            else:
                child.attrs = {}
        else:
            child.unwrap()
    return t.decode_contents()


def _score_container(el) -> int:
    """Score how likely an element is the main article body (higher = better)."""
    paras = [p for p in el.find_all("p") if len(p.get_text(strip=True)) >= 50]
    if not paras:
        return 0
    para_text   = sum(len(p.get_text(strip=True)) for p in paras)
    total_text  = max(len(el.get_text(strip=True)), 1)
    link_text   = sum(len(a.get_text(strip=True)) for a in el.find_all("a"))
    link_ratio  = link_text / total_text
    return int(para_text * max(0, 1 - link_ratio * 2))


def _soup_to_html(soup_el, max_chars: int) -> str:
    """
    Walk a BeautifulSoup container and return clean article HTML.
    Preserves paragraphs, headings, lists, blockquotes; strips everything else.
    """
    parts  = []
    total  = 0
    seen   = set()

    for elem in soup_el.find_all(["p", "h1", "h2", "h3", "h4", "blockquote", "ul", "ol"]):
        eid = id(elem)
        if eid in seen:
            continue
        seen.add(eid)

        text = elem.get_text(" ", strip=True)

        if elem.name == "p":
            if len(text) < 40:
                continue
            html = f"<p>{_clean_inline_html(elem)}</p>"

        elif elem.name in ("h1", "h2", "h3", "h4"):
            if not text:
                continue
            html = f"<h3>{esc(text)}</h3>"

        elif elem.name == "blockquote":
            if not text:
                continue
            html = f"<blockquote>{esc(text)}</blockquote>"

        elif elem.name in ("ul", "ol"):
            items = [
                f"<li>{esc(li.get_text(' ', strip=True))}</li>"
                for li in elem.find_all("li")
                if li.get_text(strip=True)
            ]
            if not items:
                continue
            tag = elem.name
            html = f"<{tag}>{''.join(items)}</{tag}>"

        else:
            continue

        parts.append(html)
        total += len(html)
        if total >= max_chars:
            break

    return "\n".join(parts)


def html_to_excerpt(raw_html: str, max_chars: int = 2000) -> str:
    """Extract clean article HTML from an arbitrary HTML string (e.g. RSS content)."""
    if not raw_html:
        return ""
    soup = BeautifulSoup(raw_html, "lxml")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside",
                     "form", "noscript", "iframe", "button", "figure"]):
        tag.decompose()
    result = _soup_to_html(soup, max_chars)
    return result


def extract_excerpt(url: str, config: dict, max_chars: int = 2000) -> str:
    """
    Fetch an article URL and return clean, sanitized HTML with formatting preserved.
    Uses a scoring approach to find the main content container.
    Returns "" on any failure — never raises.
    """
    timeout = config.get("request_timeout_seconds", 10)
    ua = config.get("user_agent", "Mozilla/5.0")
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": ua}, stream=True)
        resp.raise_for_status()

        chunk = b""
        for data in resp.iter_content(chunk_size=8192):
            chunk += data
            if len(chunk) >= 200_000:
                break
        resp.close()

        soup = BeautifulSoup(chunk, "lxml")

        # Strip noise elements
        for tag in soup(["script", "style", "nav", "header", "footer", "aside",
                         "form", "noscript", "iframe", "button", "input", "select",
                         "figure", "figcaption", "svg", "canvas", "video", "audio"]):
            tag.decompose()

        # 1. Try named semantic containers first
        _CONTENT_ID  = re.compile(r'\b(article|content|story|main|post|body)\b', re.I)
        _CONTENT_CLS = re.compile(
            r'\b(article[_-]body|article[_-]content|post[_-]body|story[_-]body'
            r'|entry[_-]content|content[_-]body|article__body|story__body'
            r'|article__content|post__content|td-post-content)\b', re.I
        )
        named = [
            soup.find("article"),
            soup.find(id=_CONTENT_ID),
            soup.find(class_=_CONTENT_CLS),
            soup.find("main"),
        ]
        best       = None
        best_score = 0
        for cand in named:
            if cand:
                s = _score_container(cand)
                if s > best_score:
                    best_score, best = s, cand

        # 2. If no good semantic match, score all divs/sections
        if best_score < 300:
            for div in soup.find_all(["div", "section"]):
                s = _score_container(div)
                if s > best_score:
                    best_score, best = s, div

        container = best or soup.body
        if container:
            result = _soup_to_html(container, max_chars)
            if result:
                return result

        # 3. Last resort: meta description
        for meta_tag in [
            soup.find("meta", property="og:description"),
            soup.find("meta", attrs={"name": "description"}),
            soup.find("meta", attrs={"name": "twitter:description"}),
        ]:
            if meta_tag and meta_tag.get("content", "").strip():
                return f"<p>{esc(meta_tag['content'].strip())}</p>"

    except Exception as e:
        logging.debug("excerpt failed for %s: %s", url, e)

    return ""


# ── Email stub ─────────────────────────────────────────────────────────────────

def stub_send_email(digest: dict, config: dict) -> None:
    """Email stub — no-op until email is configured."""
    if config.get("email", {}).get("enabled"):
        logging.warning(
            "Email sending is not yet implemented. "
            "Configure SMTP settings and implement stub_send_email()."
        )


# ── HTML report ────────────────────────────────────────────────────────────────

def _fmt_date(iso_str: str) -> str:
    """Convert ISO date string to 'Mar 7, 2026' format."""
    try:
        dt = datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%b %-d, %Y")
    except Exception:
        return iso_str[:10] if iso_str else ""


def build_html_report(digest: dict) -> str:
    """Generate a complete self-contained HTML page from a digest dict."""
    generated_at = digest.get("generated_at", "")
    try:
        dt = datetime.datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        gen_str = dt.strftime("%b %-d, %-I:%M %p")
    except Exception:
        gen_str = generated_at

    # Build flat article list for JS navigation + sidebar rows
    all_articles = []   # [{feed, title, url, source, published, excerpt}, ...]
    feed_groups  = []   # [{feed_name, start_idx, count}, ...]

    for group in digest.get("topics", []):
        feed_name = group.get("topic", "")
        articles  = group.get("articles", [])
        if not articles:
            continue
        start = len(all_articles)
        for a in articles:
            all_articles.append({
                "feed":      feed_name,
                "title":     a.get("title", "Untitled"),
                "url":       a.get("url", "#"),
                "source":    a.get("source", ""),
                "published": _fmt_date(a.get("published", "")),
                "excerpt":   a.get("excerpt", ""),
            })
        feed_groups.append({"feed_name": feed_name, "start": start, "count": len(articles)})

    # Build sidebar HTML
    if feed_groups:
        rows_html = []
        for fg in feed_groups:
            fname = esc(fg["feed_name"])
            rows_html.append(f'<div class="feed-group collapsed">')
            rows_html.append(
                f'<div class="feed-name" data-feed="{fname}">'
                f'<span class="feed-toggle">&#9654;</span>'
                f'<span class="feed-label">{fname}</span>'
                f'<span class="feed-count">{fg["count"]}</span>'
                f'<div class="feed-max-wrap" onclick="event.stopPropagation()">'
                f'<input class="feed-max-input" type="number" min="1" max="50" value="8" data-feed="{fname}">'
                f'<button class="feed-max-save" data-feed="{fname}">&#10003;</button>'
                f'</div>'
                f'</div>'
            )
            for i in range(fg["start"], fg["start"] + fg["count"]):
                a = all_articles[i]
                meta = " &middot; ".join(p for p in [esc(a["source"]), esc(a["published"])] if p)
                rows_html.append(
                    f'<div class="article-row" data-idx="{i}">'
                    f'<div class="article-row-title">{esc(a["title"])}</div>'
                    f'<div class="article-row-meta">{meta}</div>'
                    f'</div>'
                )
            rows_html.append('</div>')
        sidebar_body = "\n".join(rows_html)
    else:
        sidebar_body = (
            '<div class="empty-list">'
            '<p>No articles yet.</p>'
            '<p>Click <strong>Refresh</strong> to fetch the latest news.</p>'
            '</div>'
        )

    articles_json = json.dumps(all_articles, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>News Digest</title>
  <style>{_CSS}</style>
</head>
<body>
<div class="layout">

  <!-- ── Sidebar ── -->
  <aside class="sidebar" id="sidebar">
    <div class="sidebar-header">
      <h1>&#128240; News Digest</h1>
      <p class="meta">{esc(gen_str)}</p>
      <div class="sidebar-actions">
        <button id="refresh-btn" class="refresh-btn">&#8635; Refresh</button>
        <div class="settings-inline">
          <label for="max-articles">Per feed:</label>
          <input id="max-articles" type="number" min="1" max="50" value="8">
          <button id="save-settings" class="save-btn">Save</button>
          <span id="saved-msg" class="saved-msg">&#10003;</span>
        </div>
      </div>
    </div>
    <div class="feed-list">
      {sidebar_body}
    </div>
  </aside>

  <!-- ── Detail pane ── -->
  <main class="detail-pane" id="detail-pane">
    <div class="detail-empty" id="detail-empty">
      <div class="detail-empty-icon">&#128240;</div>
      <p>Select an article to read</p>
    </div>
    <div class="detail-content" id="detail-content">
      <div class="detail-nav">
        <button class="back-btn" id="back-btn">&#8592; Back</button>
        <button class="nav-btn" id="prev-btn">&#8249; Prev</button>
        <span class="nav-counter" id="nav-counter"></span>
        <button class="nav-btn" id="next-btn">Next &#8250;</button>
      </div>
      <div class="detail-feed" id="detail-feed"></div>
      <h2 class="detail-title" id="detail-title"></h2>
      <div class="detail-meta" id="detail-meta"></div>
      <div class="detail-excerpt-wrap" id="detail-excerpt"></div>
      <a class="read-btn" id="detail-link" href="#" target="newsreader">Read Full Article &#8594;</a>
    </div>
  </main>

</div>
<script>window.__ARTICLES__ = {articles_json};</script>
<script>{_JS}</script>
</body>
</html>"""


# ── Digest builder ─────────────────────────────────────────────────────────────

def build_digest(config: dict) -> dict:
    """Fetch articles, extract excerpts, save latest.json."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    history = load_history()

    topics = config.get("topics", [])

    if topics:
        # Topic-filtered mode: group by topic
        topics_data = []
        for topic in topics:
            logging.info("Fetching: %s", topic)
            articles = fetch_articles_for_topic(topic, config, history)
            for a in articles:
                logging.info("  Extracting excerpt: %s", a["url"][:80])
                a["excerpt"] = extract_excerpt(a["url"], config)
                mark_seen(a["url"], a["title"], topic, history)
            topics_data.append({"topic": topic, "articles": articles})
            logging.info("  → %d new articles for %r", len(articles), topic)
    else:
        # Feed mode: fetch everything, group by feed name
        logging.info("No topics set — fetching all articles from configured feeds")
        topics_data = fetch_all_feeds(config, history)
        for group in topics_data:
            for a in group["articles"]:
                logging.info("  Extracting excerpt: %s", a["url"][:80])
                a["excerpt"] = extract_excerpt(a["url"], config)
                mark_seen(a["url"], a["title"], group["topic"], history)

    save_history(history)

    result = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "topics": topics_data,
    }

    tmp = LATEST_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(LATEST_PATH)
    logging.info("Digest saved to %s", LATEST_PATH)

    stub_send_email(result, config)
    return result


# ── Scheduler ──────────────────────────────────────────────────────────────────

def run_scheduler(config: dict) -> None:
    """Block forever, running build_digest() daily at the configured time."""
    time_str = config.get("schedule", {}).get("time", "07:00")
    logging.info("Scheduler started — will run daily at %s", time_str)
    schedule.every().day.at(time_str).do(build_digest, config=config)
    while True:
        schedule.run_pending()
        time.sleep(60)


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="NewsDigest — fetch and summarise top news by topic.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python digest.py --run-once
  python digest.py --run-once --topics "AI" "Apple"
  python digest.py --run-scheduler
  python digest.py --export ~/Desktop/digest.html
  python digest.py --config /path/to/other-config.json -v
""",
    )
    ap.add_argument("--run-once",       action="store_true", help="Fetch now and exit")
    ap.add_argument("--run-scheduler",  action="store_true", help="Run on daily schedule (blocks)")
    ap.add_argument("--export",  "-e",  metavar="PATH",      help="Write HTML digest to file")
    ap.add_argument("--topics",  "-t",  metavar="TOPIC", nargs="+", help="Override topics for this run")
    ap.add_argument("--config",  "-c",  metavar="PATH",      help="Alternate config.json path")
    ap.add_argument("--verbose", "-v",  action="store_true", help="Debug logging")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    config_path = Path(args.config) if args.config else CONFIG_PATH
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        logging.error("Config not found: %s", config_path)
        return 1

    if args.topics:
        config["topics"] = args.topics

    if args.run_once or args.export:
        digest = build_digest(config)
        if args.export:
            html = build_html_report(digest)
            out = Path(args.export).expanduser()
            out.write_text(html, encoding="utf-8")
            logging.info("HTML written to %s", out)
        return 0

    if args.run_scheduler:
        run_scheduler(config)
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
