# NewsDigest

Fetches top news articles for your chosen topics, extracts excerpts, and presents them as a clean web page. Also exposes a JSON API for future iPhone app integration.

---

## Setup

**1. Install dependencies**
```bash
cd /path/to/NewsDigest
pip install -r requirements.txt
```

**2. Edit `config.json`**
```json
{
  "topics": ["artificial intelligence", "Apple", "your topic here"],
  "newsapi_key": "",          ← optional, see below
  "max_articles_per_topic": 5,
  "schedule": { "time": "07:00", "enabled": true }
}
```

**3. Test a fetch**
```bash
python digest.py --run-once
```

**4. Start the web UI**
```bash
python app.py
```
Open **http://localhost:5050** in your browser.

---

## Usage

| Command | What it does |
|---------|-------------|
| `python digest.py --run-once` | Fetch now, write `data/latest.json`, exit |
| `python digest.py --run-scheduler` | Run daily at the scheduled time (blocks) |
| `python digest.py --topics "AI" "Swift"` | Override topics for this run |
| `python digest.py --export ~/digest.html` | Write a standalone HTML file |
| `python digest.py -v` | Verbose/debug logging |
| `python app.py` | Start web server on port 5050 |
| `python app.py --port 8080` | Custom port |

---

## NewsAPI (optional)

Google News RSS is used by default — no key needed.

For richer results, add a free NewsAPI key:
1. Sign up at [newsapi.org](https://newsapi.org) (free tier: 100 requests/day)
2. Add your key to `config.json`: `"newsapi_key": "your_key_here"`

When a key is set, NewsAPI fills any remaining article slots after Google News RSS.

---

## Web API (for iPhone app)

| Endpoint | Description |
|----------|-------------|
| `GET /api/digest` | Full digest as JSON |
| `GET /api/status` | Status: last run time, running state, topics |
| `POST /api/run` | Trigger a new fetch (returns immediately, runs in background) |

All API responses include `Access-Control-Allow-Origin: *` for cross-origin access.

### Example `/api/digest` response
```json
{
  "generated_at": "2026-03-07T07:00:00+00:00",
  "topics": [
    {
      "topic": "artificial intelligence",
      "articles": [
        {
          "title": "OpenAI releases new model",
          "url": "https://techcrunch.com/...",
          "source": "TechCrunch",
          "published": "2026-03-07T06:00:00Z",
          "excerpt": "OpenAI today announced...",
          "fetched_via": "google_news_rss"
        }
      ]
    }
  ]
}
```

---

## Linux server deployment

**Run the fetch via cron:**
```cron
0 7 * * * cd /opt/NewsDigest && /opt/NewsDigest/venv/bin/python digest.py --run-once >> /var/log/newsdigest.log 2>&1
```

**Run the web server with gunicorn:**
```bash
pip install gunicorn
gunicorn -w 2 -b 0.0.0.0:5050 app:app
```

---

## Data files (auto-created)

| File | Purpose |
|------|---------|
| `data/latest.json` | Most recent digest, served by the web UI and API |
| `data/history.json` | Seen article URLs — prevents repeats across runs (pruned after 30 days) |

---

## Config reference

| Key | Default | Description |
|-----|---------|-------------|
| `topics` | `["artificial intelligence", ...]` | List of topics to search |
| `newsapi_key` | `""` | NewsAPI key (optional) |
| `max_articles_per_topic` | `5` | Max articles shown per topic |
| `schedule.time` | `"07:00"` | Daily run time (24h, local time) |
| `schedule.enabled` | `true` | Enable/disable scheduler |
| `email.enabled` | `false` | Email sending (not yet implemented) |
| `request_timeout_seconds` | `10` | HTTP timeout for all requests |
| `user_agent` | `"NewsDigest/1.0"` | User-Agent header for requests |
