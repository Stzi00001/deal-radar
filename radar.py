#!/usr/bin/env python3
"""Deal-Radar: holt Deal-Feeds, filtert nach deiner Watchlist, bewertet mit
einem lernenden Modell (aus deinen 👍/👎) und schickt Push über ntfy.

Nur Python-Standardbibliothek – keine Installation nötig.
Läuft per GitHub Actions alle paar Minuten (siehe .github/workflows/radar.yml).
"""
import html
import json
import math
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(ROOT, "config.json")
DEALS = os.path.join(ROOT, "data", "deals.json")
FEEDBACK = os.path.join(ROOT, "data", "feedback.json")

MAX_DEALS = 400
MAX_AGE_DAYS = 30
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")
NS = {"pepper": "http://www.pepper.com/rss", "media": "http://search.yahoo.com/mrss/"}

STOPWORDS = set("""und oder der die das mit fuer von bei auf aus ein eine einer
inkl statt nur jetzt neu neue alle versch verschiedene sorten prime amazon ab bis
zum zur den dem des the and for with gratis eur euro""".split())


# ---------------------------------------------------------------- Hilfsfunktionen
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def norm(text):
    """Kleinschreibung, Umlaute auflösen, Sonderzeichen -> Leerzeichen."""
    t = (text or "").lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        t = t.replace(a, b)
    t = re.sub(r"[^a-z0-9€]+", " ", t)
    return " " + re.sub(r"\s+", " ", t).strip() + " "


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def to_float(s):
    if s is None:
        return None
    s = str(s).replace("€", "").strip()
    if re.search(r"\d\.\d{3}(,|$)", s):      # 1.646 oder 1.646,50
        s = s.replace(".", "")
    s = s.replace(",", ".")
    m = re.search(r"\d+(\.\d+)?", s)
    return float(m.group()) if m else None


# ---------------------------------------------------------------- Feeds
def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml,application/xml,text/xml,*/*"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read()


def parse_feed(raw, source, rubrik=None):
    root = ET.fromstring(raw)
    out = []
    for it in root.iter("item"):
        title = strip_html(it.findtext("title"))
        link = (it.findtext("link") or "").strip()
        if not title or not link:
            continue
        desc_html = it.findtext("description") or ""
        merchant, price = "", None
        m = it.find("pepper:merchant", NS)
        if m is not None:
            merchant = m.get("name", "")
            price = to_float(m.get("price"))
        if price is None:  # Fallback: erster Eurobetrag im Titel
            pm = re.search(r"(\d{1,5}(?:[.,]\d{1,2})?)\s?(?:€|eur)", title, re.I)
            price = to_float(pm.group(1)) if pm else None
        img = ""
        th = it.find("media:thumbnail", NS)
        if th is None:
            th = it.find("media:content", NS)
        if th is not None:
            img = th.get("url", "")
        pub = it.findtext("pubDate")
        try:
            pub_dt = parsedate_to_datetime(pub).astimezone(timezone.utc) if pub else datetime.now(timezone.utc)
        except (TypeError, ValueError):
            pub_dt = datetime.now(timezone.utc)
        guid = (it.findtext("guid") or link).strip()
        out.append({
            "id": re.sub(r"[^a-zA-Z0-9]+", "-", guid)[-90:],
            "title": title,
            "link": link,
            "merchant": merchant,
            "price": price,
            "category": strip_html(it.findtext("category")),
            "image": img,
            "pub": pub_dt.isoformat(),
            "source": source,
            "_rubriken": [rubrik] if rubrik else [],
            "_text": title + " " + strip_html(desc_html)[:1500],
        })
    return out


# ---------------------------------------------------------------- Matching
TARIF_HINTS = (" monat", " mtl ", "monatlich", "grundgebuehr", " vertrag", " tarif", "allnet", "laufzeit")


def phrase_in(phrase, normtext):
    p = norm(phrase)
    return p.strip() != "" and p in normtext


def match_watch(deal, w):
    title_n = norm(deal["title"])
    full_n = norm(deal["_text"])
    if any(phrase_in(x, title_n) for x in w.get("exclude", [])):
        return False
    cat_n = norm(deal.get("category"))
    if any(phrase_in(x, cat_n) for x in w.get("exclude_categories", [])):
        return False
    if w.get("type") == "rubrik":
        if w["id"] in deal.get("_rubriken", []):
            return True
        return any(phrase_in(k, title_n) for k in w.get("keywords", []))
    if w.get("type") == "tarif":
        if not any(phrase_in(k, title_n) for k in w.get("keywords", [])):
            return False
        return any(h in full_n for h in TARIF_HINTS)
    if not any(phrase_in(k, title_n) for k in w.get("keywords", [])):
        return False
    mp = w.get("max_price")
    if mp and deal.get("price") and deal["price"] > float(mp):
        return False
    return True


# ---------------------------------------------------------------- Vergleichspreis
def compare_price(deal):
    """Liest einen Vergleichspreis (idealo/PVG/UVP/statt) aus dem Deal-Text."""
    low = deal["_text"].lower()
    m = re.search(r"(vergleichspreis|pvg|idealo|geizhals|preisvergleich|statt|uvp|sonst)\D{0,30}?(\d{1,5}(?:[.,]\d{3})*(?:[.,]\d{1,2})?)\s?(?:€|eur)", low)
    if not m:
        return None
    ref = to_float(m.group(2))
    p = deal.get("price")
    if not ref or not p or ref <= p or ref > p * 10:
        return None
    return {"ref": ref, "pct": round(100 * (ref - p) / ref)}


# ---------------------------------------------------------------- Tarif-Rechner
AMOUNT = r"(\d{1,4}(?:[.,]\d{1,2})?)\s?(?:€|eur\b|euro\b)"


def analyse_tarif(deal, resale, settings):
    text = deal["_text"]
    low = text.lower()
    monthly = upfront = fee = None
    bonus = 0.0
    for m in re.finditer(AMOUNT, low):
        val = to_float(m.group(1))
        before = low[max(0, m.start() - 28):m.start()]
        after = low[m.end():m.end() + 16]
        if re.search(r"^\s*(/|pro|im|je|mtl|monatl)\s*(monat|mon\b|mtl)?|^\s*/\s*m", after) or re.search(r"(mtl\.?|monatlich|grundgeb\w*)\s*(nur\s*)?$", before):
            if monthly is None:
                monthly = val
        elif re.search(r"anschlu\w*\s*(geb\w*|preis)?\s*(von\s*)?$", before):
            if fee is None:
                fee = val
        elif re.search(r"(bonus|wechselbonus|cashback|auszahlung|praemie|prämie)\s*(von\s*)?$", before) or re.match(r"\s*(wechsel)?bonus|\s*cashback|\s*auszahlung", after):
            bonus += val
        elif re.search(r"(zuzahlung|einmalig|ger[äa]tepreis|f[üu]r)\s*(nur\s*)?$", before):
            if upfront is None:
                upfront = val
    if upfront is None and deal.get("price") is not None and deal["price"] != monthly:
        upfront = deal["price"]
    mm = re.search(r"(\d{1,2})\s*monate", low)
    months = int(mm.group(1)) if mm and 1 <= int(mm.group(1)) <= 36 else int(settings.get("contract_months", 24))
    if fee is None:
        fee = float(settings.get("default_connection_fee", 39.99))
    upfront = upfront or 0.0

    device, value = None, None
    text_n = norm(text)
    for key in sorted(resale, key=len, reverse=True):
        if phrase_in(key, text_n):
            device, value = key, float(resale[key] or 0) or None
            break
    cost = None if monthly is None else round(upfront + monthly * months + fee - bonus, 2)
    profit = None if (cost is None or value is None) else round(value - cost, 2)
    return {"monthly": monthly, "upfront": upfront, "fee": fee, "bonus": bonus,
            "months": months, "device": device, "resale": value, "cost": cost, "profit": profit}


# ---------------------------------------------------------------- Lernen (Naive Bayes)
def features(deal):
    f = set()
    for tok in norm(deal["title"]).split():
        if len(tok) >= 3 and tok not in STOPWORDS and not tok.isdigit():
            f.add(tok)
    if deal.get("merchant"):
        f.add("m:" + norm(deal["merchant"]).strip())
    if deal.get("category"):
        f.add("c:" + norm(deal["category"]).strip())
    for w in deal.get("watch", []):
        f.add("w:" + w)
    p = deal.get("price")
    if p is not None:
        f.add("p:" + ("<20" if p < 20 else "<100" if p < 100 else "<300" if p < 300 else "<800" if p < 800 else "800+"))
    return sorted(f)


def train(votes):
    pos, neg = {}, {}
    npos = nneg = 0
    vocab = set()
    for v in votes.values():
        target = pos if v.get("label") == 1 else neg
        if v.get("label") == 1:
            npos += 1
        else:
            nneg += 1
        for ft in v.get("features", []):
            target[ft] = target.get(ft, 0) + 1
            vocab.add(ft)
    return {"pos": pos, "neg": neg, "npos": npos, "nneg": nneg, "V": max(len(vocab), 1)}


def score(model, feats):
    if model["npos"] < 2 or model["nneg"] < 2:
        return None  # noch zu wenig Bewertungen
    lo = math.log(model["npos"] / model["nneg"])
    tp, tn = sum(model["pos"].values()), sum(model["neg"].values())
    for ft in feats:
        lo += math.log((model["pos"].get(ft, 0) + 1) / (tp + model["V"]))
        lo -= math.log((model["neg"].get(ft, 0) + 1) / (tn + model["V"]))
    lo = max(min(lo, 30), -30)
    return round(100 / (1 + math.exp(-lo)))


# ---------------------------------------------------------------- Push
def push(topic, deal, names):
    price = f"{deal['price']:.2f} €".replace(".", ",") if deal.get("price") is not None else ""
    lines = [", ".join(names)]
    if price or deal.get("merchant"):
        lines.append(" – ".join(x for x in (price, deal.get("merchant")) if x))
    t = deal.get("tarif")
    if t and t.get("profit") is not None:
        lines.append(f"Rechnerischer Gewinn: {t['profit']:.0f} €")
    if deal.get("score") is not None:
        lines.append(f"Passt zu dir: {deal['score']} %")
    body = json.dumps({
        "topic": topic,
        "title": deal["title"][:180],
        "message": "\n".join(lines),
        "click": deal["link"],
        "tags": ["moneybag"],
        "priority": 4 if (deal.get("score") or 0) >= 70 else 3,
    }).encode("utf-8")
    req = urllib.request.Request("https://ntfy.sh/", data=body, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=15).read()


# ---------------------------------------------------------------- Hauptlauf
def run(feed_override=None, send_push=True):
    cfg = load_json(CONFIG, {})
    store = load_json(DEALS, {"deals": []})
    votes = load_json(FEEDBACK, {"votes": {}}).get("votes", {})
    model = train(votes)
    known = {d["id"]: d for d in store.get("deals", [])}
    watch = cfg.get("watchlist", [])
    settings = cfg.get("settings", {})
    topic = os.environ.get("NTFY_TOPIC", "").strip()

    items, feed_status = [], []
    for f in cfg.get("feeds", []):
        try:
            raw = feed_override[f["url"]] if feed_override else fetch(f["url"])
            got = parse_feed(raw, f["name"], f.get("rubrik"))
            items += got
            feed_status.append({"name": f["name"], "ok": True, "count": len(got)})
        except Exception as e:  # eine kaputte Quelle stoppt nicht den Rest
            feed_status.append({"name": f["name"], "ok": False, "error": str(e)[:120]})
            print(f"[Quelle übersprungen] {f['name']}: {e}", file=sys.stderr)

    merged = {}
    for d in items:
        if d["id"] in merged:
            merged[d["id"]]["_rubriken"] += d["_rubriken"]
        else:
            merged[d["id"]] = d

    new = []
    for d in merged.values():
        hits = [w for w in watch if match_watch(d, w)]
        if not hits:
            continue
        d["watch"] = [w["id"] for w in hits]
        if any(w.get("type") == "tarif" for w in hits):
            d["tarif"] = analyse_tarif(d, cfg.get("resale", {}), settings)
        d["compare"] = compare_price(d)
        d["features"] = features(d)
        d["score"] = score(model, d["features"])
        d.pop("_text", None)
        d.pop("_rubriken", None)
        if d["id"] in known:
            known[d["id"]].update({k: d[k] for k in ("score", "tarif", "watch", "features", "compare") if k in d})
            continue
        known[d["id"]] = d
        new.append((d, hits))

    pushed = 0
    min_score = settings.get("push_min_score", 0) or 0
    for d, hits in new:
        if d["id"] in votes:
            continue
        hits = [w for w in hits if w.get("push", True)]
        if not hits:
            continue
        if d["score"] is not None and d["score"] < min_score:
            continue
        tw = [w for w in hits if w.get("type") == "tarif"]
        if tw and len(tw) == len(hits):
            p = d["tarif"]["profit"]
            if p is not None and p < float(tw[0].get("min_profit", 0)):
                continue
        if send_push and topic:
            try:
                push(topic, d, [w["name"] for w in hits])
                pushed += 1
            except Exception as e:
                print(f"[Push fehlgeschlagen] {e}", file=sys.stderr)

    # Rescore alles mit dem aktuellen Modell, alte Deals aufräumen
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    deals = []
    for d in known.values():
        try:
            if datetime.fromisoformat(d["pub"]) < cutoff:
                continue
        except (KeyError, ValueError):
            pass
        if "features" in d:
            d["score"] = score(model, d["features"])
        deals.append(d)
    deals.sort(key=lambda d: d.get("pub", ""), reverse=True)
    best = load_json(DEALS, {}).get("best", {})
    for w in watch:
        if w.get("type", "product") != "product":
            continue
        for d in deals:
            if w["id"] in d.get("watch", []) and d.get("price"):
                b = best.get(w["id"])
                if b is None or d["price"] < b["price"]:
                    best[w["id"]] = {"price": d["price"], "title": d["title"], "link": d["link"], "date": d.get("pub", "")[:10]}
    store = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "feeds": feed_status,
        "votes": len(votes),
        "best": best,
        "deals": deals[:MAX_DEALS],
    }
    save_json(DEALS, store)
    print(f"{len(items)} Einträge geprüft, {len(new)} neue Treffer, {pushed} Pushes gesendet.")
    return store, new


if __name__ == "__main__":
    run()
