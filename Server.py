"""
DesignHunter — Server API
=========================
Gira gratis su Render.com e risponde all'app sul telefono.
Cerca annunci su eBay (API ufficiale), Subito.it e Vinted (scraping).

DEPLOY SU RENDER (vedi README per istruzioni passo passo):
  1. Carica questa cartella su GitHub
  2. Crea account su render.com
  3. "New Web Service" → collega il repo → deploy automatico
"""

import os, re, json, hashlib, time, logging
from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)  # permette all'app sul telefono di fare richieste al server

# ── Credenziali (impostate come variabili d'ambiente su Render) ───────────────
EBAY_APP_ID      = os.environ.get("EBAY_APP_ID", "")
EBAY_SECRET      = os.environ.get("EBAY_CLIENT_SECRET", "")
VINTED_TOKEN     = os.environ.get("VINTED_ACCESS_TOKEN", "")
ANTHROPIC_KEY    = os.environ.get("ANTHROPIC_API_KEY", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Mobile Safari/537.36"
    ),
    "Accept-Language": "it-IT,it;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ═══════════════════════════════════════════════════════════════════════════════
#  eBay — API ufficiale gratuita
# ═══════════════════════════════════════════════════════════════════════════════

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


def search_ebay(keyword: str, max_price: int, limit: int = 12) -> list:
    token = get_ebay_token()
    if not token:
        log.warning("eBay: nessun token disponibile")
        return []

    try:
        r = requests.get(
            "https://api.ebay.com/buy/browse/v1/item_summary/search",
            headers={
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": "EBAY_IT",
            },
            params={
                "q": keyword,
                "filter": f"price:[0..{max_price}],currency:EUR,conditions:{{USED}}",
                "sort": "newlyListed",
                "limit": limit,
            },
            timeout=12,
        )
        r.raise_for_status()
        items = r.json().get("itemSummaries", [])
        results = []
        for it in items:
            price = float(it.get("price", {}).get("value", 0))
            if price <= 0 or price > max_price:
                continue
            uid = hashlib.md5(it["itemId"].encode()).hexdigest()[:10]
            results.append({
                "id": f"ebay_{uid}",
                "title": it.get("title", ""),
                "price": price,
                "url": it.get("itemWebUrl", "https://www.ebay.it"),
                "image": it.get("image", {}).get("imageUrl", ""),
                "source": "eBay",
                "location": it.get("itemLocation", {}).get("country", ""),
            })
        log.info(f"eBay: {len(results)} risultati per '{keyword}'")
        return results
    except Exception as e:
        log.error(f"eBay search error: {e}")
        return []


# ═══════════════════════════════════════════════════════════════════════════════
#  Subito.it — scraping
# ═══════════════════════════════════════════════════════════════════════════════

def search_subito(keyword: str, max_price: int, limit: int = 10) -> list:
    url = (
        f"https://www.subito.it/annunci-italia/vendita/usato/"
        f"?q={keyword.replace(' ', '+')}&ps=0&pe={max_price}"
    )
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # Subito usa JSON-LD o data attributes — proviamo più selettori
        results = []

        # Tentiamo prima con i dati strutturati JSON-LD
        scripts = soup.find_all("script", type="application/ld+json")
        for s in scripts:
            try:
                data = json.loads(s.string or "")
                items = data if isinstance(data, list) else data.get("itemListElement", [])
                for it in items[:limit]:
                    item = it.get("item", it)
                    name = item.get("name", "")
                    link = item.get("url", "")
                    price_info = item.get("offers", {})
                    price = float(price_info.get("price", 0))
                    if not name or not link or price <= 0 or price > max_price:
                        continue
                    uid = hashlib.md5(link.encode()).hexdigest()[:10]
                    results.append({
                        "id": f"subito_{uid}",
                        "title": name,
                        "price": price,
                        "url": link,
                        "image": item.get("image", ""),
                        "source": "Subito.it",
                        "location": item.get("locationCreated", {}).get("name", ""),
                    })
            except Exception:
                continue

        # Fallback: parsing HTML diretto
        if not results:
            cards = soup.select("div[class*='SmallCard'], article[class*='item']")
            for card in cards[:limit]:
                try:
                    title_el = card.select_one("h2, [class*='title']")
                    price_el = card.select_one("[class*='price']")
                    link_el  = card.select_one("a[href]")
                    img_el   = card.select_one("img")
                    loc_el   = card.select_one("[class*='town'], [class*='geo']")

                    if not title_el or not price_el or not link_el:
                        continue

                    price = parse_price(price_el.get_text())
                    if price is None or price > max_price:
                        continue

                    href = link_el["href"]
                    if not href.startswith("http"):
                        href = "https://www.subito.it" + href

                    uid = hashlib.md5(href.encode()).hexdigest()[:10]
                    results.append({
                        "id": f"subito_{uid}",
                        "title": title_el.get_text(strip=True),
                        "price": price,
                        "url": href,
                        "image": img_el.get("src", "") if img_el else "",
                        "source": "Subito.it",
                        "location": loc_el.get_text(strip=True) if loc_el else "",
                    })
                except Exception:
                    continue

        log.info(f"Subito: {len(results)} risultati per '{keyword}'")
        return results
    except Exception as e:
        log.error(f"Subito error: {e}")
        return []


# ═══════════════════════════════════════════════════════════════════════════════
#  Vinted — API interna
# ═══════════════════════════════════════════════════════════════════════════════

def search_vinted(keyword: str, max_price: int, limit: int = 10) -> list:
    headers = {
        **HEADERS,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.vinted.it/",
    }
    if VINTED_TOKEN:
        headers["Cookie"] = f"access_token_web={VINTED_TOKEN}"

    try:
        r = requests.get(
            "https://www.vinted.it/api/v2/catalog/items",
            headers=headers,
            params={
                "search_text": keyword,
                "price_to": max_price,
                "per_page": limit,
                "order": "newest_first",
            },
            timeout=15,
        )
        if r.status_code == 401:
            log.warning("Vinted: token non valido")
            return []
        r.raise_for_status()
        items = r.json().get("items", [])
        results = []
        for it in items:
            price = float(it.get("price", {}).get("amount", 0))
            if price <= 0 or price > max_price:
                continue
            photos = it.get("photos", [])
            image = photos[0].get("url", "") if photos else ""
            results.append({
                "id": f"vinted_{it['id']}",
                "title": it.get("title", ""),
                "price": price,
                "url": f"https://www.vinted.it/items/{it['id']}",
                "image": image,
                "source": "Vinted",
                "location": it.get("city", ""),
            })
        log.info(f"Vinted: {len(results)} risultati per '{keyword}'")
        return results
    except Exception as e:
        log.error(f"Vinted error: {e}")
        return []


# ═══════════════════════════════════════════════════════════════════════════════
#  AI Analysis — Claude
# ═══════════════════════════════════════════════════════════════════════════════

MARKET_REFS = {
    "arteluce": 400, "stilnovo": 350, "arredoluce": 380,
    "sarfatti": 900, "colombo": 600, "magistretti": 500,
    "cassina": 700, "zanotta": 400, "gavina": 800,
    "mid century": 200, "vintage design": 160, "lampada vintage": 140,
}

def estimate_market(title: str) -> int:
    t = title.lower()
    for kw, price in MARKET_REFS.items():
        if kw in t:
            return price
    return 180

def analyze_listings(items: list) -> list:
    if not items:
        return items

    if ANTHROPIC_KEY:
        prompt = f"""Sei un esperto di design vintage e mid-century italiano ed europeo.
Brand: Arteluce, Stilnovo, Arredoluce, Cassina, Gavina, Zanotta, Arflex.
Designer: Gino Sarfatti, Joe Colombo, Vico Magistretti, Gio Ponti, Marco Zanuso.

Analizza questi annunci per rivendita su Vinted/Subito.

Per ogni annuncio:
- score: 0-100 (opportunità rivendita: autenticità, prezzo sotto mercato, domanda)
- marketPrice: stima prezzo mercato reale in € (intero)
- aiAnalysis: max 2 frasi pratiche (cosa controllare, urgenza, rischi)

Annunci:
{json.dumps([{"id": l["id"], "title": l["title"], "price": l["price"], "source": l["source"]} for l in items], ensure_ascii=False)}

Rispondi SOLO con JSON valido. Array: [{{id, score, marketPrice, aiAnalysis}}]"""

        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 2000,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=30,
            )
            text = r.json()["content"][0]["text"]
            text = text.replace("```json", "").replace("```", "").strip()
            ai_results = {res["id"]: res for res in json.loads(text)}

            for item in items:
                ai = ai_results.get(item["id"], {})
                mp = int(ai.get("marketPrice", estimate_market(item["title"])))
                item["score"]       = ai.get("score", 50)
                item["marketPrice"] = mp
                item["margin"]      = round(((mp - item["price"]) / item["price"]) * 100)
                item["aiAnalysis"]  = ai.get("aiAnalysis", "")
            return items

        except Exception as e:
            log.error(f"AI error: {e}")

    # Fallback senza AI
    for item in items:
        mp = estimate_market(item["title"])
        item["score"]       = min(90, max(20, round(((mp - item["price"]) / item["price"]) * 30)))
        item["marketPrice"] = mp
        item["margin"]      = round(((mp - item["price"]) / item["price"]) * 100)
        item["aiAnalysis"]  = f"Stima locale: prezzo mercato ~€{mp}"
    return items


# ═══════════════════════════════════════════════════════════════════════════════
#  UTILS
# ═══════════════════════════════════════════════════════════════════════════════

def parse_price(text: str):
    text = text.replace(".", "").replace(",", ".")
    m = re.search(r"\d+(?:\.\d+)?", text)
    return float(m.group()) if m else None


# ═══════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def home():
    return jsonify({"status": "ok", "service": "DesignHunter API"})


@app.route("/search")
def search():
    keyword   = request.args.get("q", "lampada vintage")
    max_price = int(request.args.get("max_price", 300))
    min_margin = int(request.args.get("min_margin", 80))
    sources   = request.args.get("sources", "ebay,subito,vinted").lower().split(",")

    all_items = []
    if "ebay" in sources:
        all_items += search_ebay(keyword, max_price)
    if "subito" in sources:
        all_items += search_subito(keyword, max_price)
    if "vinted" in sources:
        all_items += search_vinted(keyword, max_price)

    # Analisi AI
    enriched = analyze_listings(all_items)

    # Filtra per margine minimo e ordina per score
    filtered = [
        i for i in enriched
        if i.get("margin", 0) >= min_margin
    ]
    filtered.sort(key=lambda x: x.get("score", 0), reverse=True)

    return jsonify({
        "keyword": keyword,
        "total": len(filtered),
        "results": filtered,
    })


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "ebay": bool(EBAY_APP_ID),
        "vinted": bool(VINTED_TOKEN),
        "ai": bool(ANTHROPIC_KEY),
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
