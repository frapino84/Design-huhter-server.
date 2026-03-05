import os, re, json, hashlib, time, logging
from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

EBAY_APP_ID      = os.environ.get("EBAY_APP_ID", "")
EBAY_SECRET      = os.environ.get("EBAY_CLIENT_SECRET", "")
VINTED_TOKEN     = os.environ.get("VINTED_ACCESS_TOKEN", "")
ANTHROPIC_KEY    = os.environ.get("ANTHROPIC_API_KEY", "")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Accept-Language": "it-IT,it;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_ebay_token_cache = {"token": None, "expires": 0}

def get_ebay_token():
    if _ebay_token_cache["token"] and time.time() < _ebay_token_cache["expires"]:
        return _ebay_token_cache["token"]
    if not EBAY_APP_ID or not EBAY_SECRET:
        return None
    import base64
    creds = base64.b64encode(f"{EBAY_APP_ID}:{EBAY_SECRET}".encode()).decode()
    try:
        r = requests.post(
            "https://api.ebay.com/identity/v1/oauth2/token",
            headers={"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"},
            data="grant_type=client_credentials&scope=https://api.ebay.com/oauth/api_scope",
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        _ebay_token_cache["token"] = data["access_token"]
        _ebay_token_cache["expires"] = time.time() + data.get("expires_in", 7200) - 60
        return _ebay_token_cache["token"]
    except Exception as e:
        log.error(f"eBay token error: {e}")
        return None

def search_ebay(keyword, max_price, limit=12):
    token = get_ebay_token()
    if not token:
        return []
    try:
        r = requests.get(
            "https://api.ebay.com/buy/browse/v1/item_summary/search",
            headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_IT"},
            params={"q": keyword, "filter": f"price:[0..{max_price}],currency:EUR,conditions:{{USED}}", "sort": "newlyListed", "limit": limit},
            timeout=12,
        )
        r.raise_for_status()
        results = []
        for it in r.json().get("itemSummaries", []):
            price = float(it.get("price", {}).get("value", 0))
            if price <= 0 or price > max_price:
                continue
            uid = hashlib.md5(it["itemId"].encode()).hexdigest()[:10]
            results.append({"id": f"ebay_{uid}", "title": it.get("title", ""), "price": price, "url": it.get("itemWebUrl", "https://www.ebay.it"), "image": it.get("image", {}).get("imageUrl", ""), "source": "eBay", "location": it.get("itemLocation", {}).get("country", "")})
        return results
    except Exception as e:
        log.error(f"eBay error: {e}")
        return []

def search_subito(keyword, max_price, limit=10):
    url = f"https://www.subito.it/annunci-italia/vendita/usato/?q={keyword.replace(' ', '+')}&ps=0&pe={max_price}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        for s in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(s.string or "")
                items = data if isinstance(data, list) else data.get("itemListElement", [])
                for it in items[:limit]:
                    item = it.get("item", it)
                    name = item.get("name", "")
                    link = item.get("url", "")
                    price = float(item.get("offers", {}).get("price", 0))
                    if not name or not link or price <= 0 or price > max_price:
                        continue
                    uid = hashlib.md5(link.encode()).hexdigest()[:10]
                    results.append({"id": f"subito_{uid}", "title": name, "price": price, "url": link, "image": item.get("image", ""), "source": "Subito.it", "location": item.get("locationCreated", {}).get("name", "")})
            except Exception:
                continue
        return results
    except Exception as e:
        log.error(f"Subito error: {e}")
        return []

def search_vinted(keyword, max_price, limit=10):
    headers = {**HEADERS, "Accept": "application/json", "Referer": "https://www.vinted.it/"}
    if VINTED_TOKEN:
        headers["Cookie"] = f"access_token_web={VINTED_TOKEN}"
    try:
        r = requests.get("https://www.vinted.it/api/v2/catalog/items", headers=headers, params={"search_text": keyword, "price_to": max_price, "per_page": limit, "order": "newest_first"}, timeout=15)
        if r.status_code == 401:
            return []
        r.raise_for_status()
        results = []
        for it in r.json().get("items", []):
            price = float(it.get("price", {}).get("amount", 0))
            if price <= 0 or price > max_price:
                continue
            photos = it.get("photos", [])
            results.append({"id": f"vinted_{it['id']}", "title": it.get("title", ""), "price": price, "url": f"https://www.vinted.it/items/{it['id']}", "image": photos[0].get("url", "") if photos else "", "source": "Vinted", "location": it.get("city", "")})
        return results
    except Exception as e:
        log.error(f"Vinted error: {e}")
        return []

MARKET_REFS = {"arteluce": 400, "stilnovo": 350, "arredoluce": 380, "sarfatti": 900, "colombo": 600, "magistretti": 500, "cassina": 700, "zanotta": 400, "gavina": 800, "mid century": 200, "vintage design": 160, "lampada vintage": 140}

def estimate_market(title):
    t = title.lower()
    for kw, price in MARKET_REFS.items():
        if kw in t:
            return price
    return 180

def analyze_listings(items):
    if not items:
        return items
    if ANTHROPIC_KEY:
        prompt = f"""Sei un esperto di design vintage mid-century italiano. Analizza questi annunci per rivendita.
Per ogni annuncio: score 0-100, marketPrice stima mercato €, aiAnalysis 2 frasi pratiche.
Annunci: {json.dumps([{"id": l["id"], "title": l["title"], "price": l["price"]} for l in items], ensure_ascii=False)}
Rispondi SOLO con JSON. Array: [{{id, score, marketPrice, aiAnalysis}}]"""
        try:
            r = requests.post("https://api.anthropic.com/v1/messages", headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}, json={"model": "claude-sonnet-4-20250514", "max_tokens": 2000, "messages": [{"role": "user", "content": prompt}]}, timeout=30)
            text = r.json()["content"][0]["text"].replace("```json", "").replace("```", "").strip()
            ai_results = {res["id"]: res for res in json.loads(text)}
            for item in items:
                ai = ai_results.get(item["id"], {})
                mp = int(ai.get("marketPrice", estimate_market(item["title"])))
                item["score"] = ai.get("score", 50)
                item["marketPrice"] = mp
                item["margin"] = round(((mp - item["price"]) / item["price"]) * 100)
                item["aiAnalysis"] = ai.get("aiAnalysis", "")
            return items
        except Exception as e:
            log.error(f"AI error: {e}")
    for item in items:
        mp = estimate_market(item["title"])
        item["score"] = min(90, max(20, round(((mp - item["price"]) / item["price"]) * 30)))
        item["marketPrice"] = mp
        item["margin"] = round(((mp - item["price"]) / item["price"]) * 100)
        item["aiAnalysis"] = f"Stima locale: prezzo mercato ~€{mp}"
    return items

def parse_price(text):
    text = text.replace(".", "").replace(",", ".")
    m = re.search(r"\d+(?:\.\d+)?", text)
    return float(m.group()) if m else None

@app.route("/")
def home():
    return jsonify({"status": "ok", "service": "DesignHunter API"})

@app.route("/search")
def search():
    keyword = request.args.get("q", "lampada vintage")
    max_price = int(request.args.get("max_price", 300))
    min_margin = int(request.args.get("min_margin", 80))
    sources = request.args.get("sources", "ebay,subito,vinted").lower().split(",")
    all_items = []
    if "ebay" in sources:
        all_items += search_ebay(keyword, max_price)
    if "subito" in sources:
        all_items += search_subito(keyword, max_price)
    if "vinted" in sources:
        all_items += search_vinted(keyword, max_price)
    enriched = analyze_listings(all_items)
    filtered = [i for i in enriched if i.get("margin", 0) >= min_margin]
    filtered.sort(key=lambda x: x.get("score", 0), reverse=True)
    return jsonify({"keyword": keyword, "total": len(filtered), "results": filtered})

@app.route("/health")
def health():
    return jsonify({"status": "ok", "ebay": bool(EBAY_APP_ID), "vinted": bool(VINTED_TOKEN), "ai": bool(ANTHROPIC_KEY)})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
