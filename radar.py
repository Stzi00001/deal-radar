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
NS = {"pepper": "http://www.pepper.com/rss", "media": "http://search.yahoo.com/mrss/",
      "content": "http://purl.org/rss/1.0/modules/content/"}
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Berlin")
except Exception:  # pragma: no cover
    TZ = timezone(timedelta(hours=2))

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
def sharpen(url):
    """mydealz liefert 150px-Vorschaubilder – größere Variante anfordern."""
    return re.sub(r"/re/\d+x\d+/qt/\d+/", "/re/450x450/qt/70/", url or "")


def summary(desc_html, title):
    """Kurze Beschreibung ohne Preiszeile/Bilder, max. ~320 Zeichen."""
    t = strip_html(re.sub(r"<strong>[^<]*</strong>\s*(<br\s*/?>)?", " ", desc_html or "", count=1))
    t = re.sub(r"\[shortcode[^\]]*\]", " ", t)
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if t.lower().startswith(title.lower()[:40]):
        t = t[len(title):].strip(" -–:")
    if len(t) > 320:
        t = t[:320].rsplit(" ", 1)[0] + " …"
    return t
CODE_STOP = {"GRATIS", "PRIME", "MBW", "EUR", "FREE", "PAYBACK", "SHOOP", "NEWSLETTER", "APP", "DEAL", "LINK", "HIER", "UND", "OHNE"}


def find_code(text):
    """Gutscheincode aus Titel/Text, z. B. 'Code: ARDA', 'Gutscheincode APP15'."""
    for m in re.finditer(r"(?i:gutschein-?code|rabatt-?code|aktionscode|promo-?code|gutschein|code)\s*(?:[:=]|lautet|ist)?\s*[„\"'“‚»]?([A-Z0-9][A-Z0-9_-]{2,23})\b", text or ""):
        c = m.group(1).strip("-_")
        if c in CODE_STOP or len(c) < 4 or not re.search(r"[A-Z]", c) and not re.search(r"\d{4,}", c):
            continue
        if re.fullmatch(r"\d+([.,]\d+)?", c):  # reine Zahl/Betrag
            continue
        return c
    return None


DATE = r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})?"


def _mkdate(d, m, y, ref):
    try:
        yy = int(y) if y else ref.year
        if yy < 100:
            yy += 2000
        dt = datetime(yy, int(m), int(d), 23, 59, tzinfo=TZ)
    except ValueError:
        return None
    if not y and dt < ref - timedelta(days=2):
        dt = dt.replace(year=dt.year + 1)
    if not y and dt > ref + timedelta(days=120):  # ohne Jahr nur nahe Daten glauben
        return None
    return dt


def find_until(text, pub):
    """Ablaufdatum / Einsendeschluss aus dem Text (Datum am Ende eines Zeitraums oder nach 'bis')."""
    t = text or ""
    cands = []
    for m in re.finditer(DATE + r"\s*(?:-|–|bis)\s*" + DATE, t):
        cands.append(m.groups()[3:6])
    for m in re.finditer(r"(?i)(?:gültig bis|gueltig bis|bis zum|bis einschließlich|bis einschl\.|endet am|läuft bis|einsendeschluss:?|teilnahmeschluss:?|nur bis|aktion bis|bis)\s*(?:[A-Za-zäöüÄÖÜ]+,?\s+)?(?:den\s*)?" + DATE, t):
        cands.append(m.groups()[-3:])
    for d, mo, y in cands:
        dt = _mkdate(d, mo, y, pub)
        if dt and pub - timedelta(days=90) <= dt <= pub + timedelta(days=400):
            return dt.isoformat()
    return None


GW_TYPES = [("Instagram", r"instagram"), ("Facebook", r"facebook"), ("TikTok", r"tiktok"), ("App", r"\bapp\b"),
            ("Postkarte", r"postkarte|per post|einsenden"), ("Kassenbon", r"kassenbon|kaufbeleg|beim kauf|aktionsprodukt"),
            ("Quiz", r"quiz|frage beantworten|lösungswort|loesungswort"), ("Newsletter", r"newsletter"), ("Online", r"online|formular|website|webseite")]


def gewinnspiel_info(text):
    """Teilnahmeart und Gewinn-Wert aus einem Gewinnspiel-Text."""
    low = (text or "").lower()
    kind = next((name for name, rx in GW_TYPES if re.search(rx, low)), "Online")
    value = None
    m = re.search(r"(?:im (?:gesamt)?wert von|gesamtwert(?: von)?|wert:?|im wert)\s*(?:über|ueber|bis zu|ca\.?)?\s*(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?)\s?(?:€|eur|euro)", low)
    if m:
        value = to_float(m.group(1))
    else:
        m = re.search(r"(\d{1,3}(?:\.\d{3})+|\d{3,6})\s?(?:€|eur|euro)\s*(?:bargeld|cash|gutschein|reisegutschein|einkaufsgutschein)", low)
        value = to_float(m.group(1)) if m else None
    return {"kind": kind, "value": value}


def first_img(html_text):
    for m in re.finditer(r"<img[^>]+src=[\"']([^\"']+)[\"']", html_text or ""):
        u = m.group(1)
        if u.startswith("http") and not re.search(r"emoji|\.svg|gravatar|pixel|logo|avatar|1x1", u, re.I):
            return u
    return ""


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml,application/xml,text/xml,*/*"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read()


def parse_feed(raw, source, rubrik=None):
    root = ET.fromstring(raw)
    out = []
    for it in root.iter("item"):
        raw_title = strip_html(it.findtext("title"))
        tm = re.match(r"^\s*(-?\d+)°\s*-\s*", raw_title)
        temp = int(tm.group(1)) if tm else None
        title = raw_title[tm.end():] if tm else raw_title
        link = (it.findtext("link") or "").strip()
        if not title or not link:
            continue
        desc_html = it.findtext("content:encoded", namespaces=NS) or it.findtext("description") or ""
        merchant, price, discount = "", None, None
        m = it.find("pepper:merchant", NS)
        if m is not None:
            merchant = m.get("name", "")
            price = to_float(m.get("price"))
            dm = re.search(r"\d+", m.get("discount") or "")
            discount = int(dm.group()) if dm and "%" in (m.get("discount") or "") else None
        if price is None:  # Fallback: erster Eurobetrag im Titel
            pm = re.search(r"(\d{1,5}(?:[.,]\d{1,2})?)\s?(?:€|eur)", title, re.I)
            price = to_float(pm.group(1)) if pm else None
        img = ""
        th = it.find("media:thumbnail", NS)
        if th is None:
            th = it.find("media:content", NS)
        if th is not None:
            img = th.get("url", "")
        if not img:
            enc = it.find("enclosure")
            img = enc.get("url", "") if enc is not None and "image" in (enc.get("type") or "image") else ""
        if not img:
            img = first_img(desc_html)
        pub = it.findtext("pubDate")
        try:
            pub_dt = parsedate_to_datetime(pub).astimezone(timezone.utc) if pub else datetime.now(timezone.utc)
        except (TypeError, ValueError):
            pub_dt = datetime.now(timezone.utc)
        guid = (it.findtext("guid") or link).strip()
        plain = strip_html(desc_html)
        out.append({
            "id": re.sub(r"[^a-zA-Z0-9]+", "-", guid)[-90:],
            "title": title,
            "link": link,
            "merchant": merchant,
            "price": price,
            "category": ", ".join(strip_html(c.text) for c in it.findall("category") if c.text)[:120],
            "image": img,
            "temp": temp,
            "discount": discount,
            "desc": summary(desc_html, title),
            "code": find_code(title + " " + plain[:3000]),
            "until": find_until(title + " " + plain[:3000], pub_dt.astimezone(TZ)),
            "pub": pub_dt.isoformat(),
            "source": source,
            "_rubriken": [rubrik] if rubrik else [],
            "_text": title + " " + plain[:1500],
        })
    return out


# ---------------------------------------------------------------- Matching
TARIF_HINTS = ("€ monat", " pro monat", " im monat", " mtl ", "monatlich", "grundgebuehr", " vertrag", " tarif", "allnet", "laufzeit", " flat",
               " netz ", " o2 ", " vodafone ", " telekom ", " freenet ", " 1und1 ", " congstar ", " otelo ")


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
        return any(h in title_n for h in TARIF_HINTS)
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
    seen_bonus = set()
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
        elif re.search(r"(bonus|wechselbonus|cashback|auszahlung|praemie|prämie)\s*(von\s*)?$", before) or re.match(r"\s*(wechsel)?bonus|\s*cashback|\s*auszahlung|\s*(start)?guthaben|\s*gutschrift", after):
            if val not in seen_bonus:  # gleicher Bonus in Titel und Text nur einmal zählen
                seen_bonus.add(val)
                bonus += val
        elif re.search(r"(zuzahlung|einmalig|ger[äa]tepreis|f[üu]r)\s*(nur\s*)?$", before) or re.match(r"\s*(zuzahlung|einmalig)", after):
            if upfront is None:
                upfront = val
            elif monthly is None and val < 150 and re.search(r"(gb|allnet|flat|tarif)", before):
                monthly = val  # "Handy für 79€ + 50GB Allnet für 29,99€"
    if upfront is None and deal.get("price") is not None and deal["price"] != monthly and deal["price"] <= 600:
        upfront = deal["price"]
    mm = re.search(r"(\d{1,2})\s*monate", low)
    months = int(mm.group(1)) if mm and 1 <= int(mm.group(1)) <= 36 else int(settings.get("contract_months", 24))
    if fee is None:
        fee = float(settings.get("default_connection_fee", 39.99))
    upfront = upfront or 0.0

    device, value = None, None
    for src in (norm(deal["title"]), norm(text)):  # Titel zuerst – die Beschreibung nennt oft Alternativmodelle
        for key in sorted(resale, key=len, reverse=True):
            if phrase_in(key, src):
                device, value = key, float(resale[key] or 0) or None
                break
        if device:
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
def app_url():
    """https://<owner>.github.io/<repo>/ – für Links aus Push-Nachrichten."""
    r = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in r:
        return ""
    o, n = r.split("/", 1)
    return f"https://{o.lower()}.github.io/{n}/"


def ntfy(payload):
    req = urllib.request.Request("https://ntfy.sh/", data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=15).read()


def push(topic, deal, names):
    t = deal.get("tarif")
    if t and t.get("monthly") is not None:
        price = f"{t['monthly']:.2f} €/Monat".replace(".", ",")
    elif deal.get("price") is not None:
        price = f"{deal['price']:.2f} €".replace(".", ",")
    else:
        price = f"−{deal['discount']} %" if deal.get("discount") else ""
    first = " · ".join(x for x in (price, deal.get("merchant")) if x)
    if deal.get("compare"):
        first += f" (−{deal['compare']['pct']} %)"
    lines = [first] if first else []
    if t and t.get("profit") is not None:
        lines.append(f"{'Gewinn' if t['profit'] >= 0 else 'Verlust'} nach Verkauf: {abs(t['profit']):.0f} €")
    if deal.get("code"):
        lines.append(f"Code: {deal['code']}")
    lines.append(", ".join(names))
    app = app_url()
    payload = {
        "topic": topic,
        "title": deal["title"][:180],
        "message": "\n".join(lines),
        "click": f"{app}?open={deal['id']}" if app else deal["link"],
        "tags": ["fire"] if (deal.get("temp") or 0) >= 200 else ["moneybag"],
        "priority": 4 if (deal.get("score") or 0) >= 70 else 3,
        "actions": [{"action": "view", "label": "Zum Deal", "url": deal["link"], "clear": True}],
    }
    if app:
        payload["actions"].append({"action": "view", "label": "Kein Interesse", "url": f"{app}?hide={deal['id']}", "clear": True})
    if deal.get("image"):
        payload["attach"] = sharpen(deal["image"]) if "mydealz" in deal["image"] else deal["image"]
    ntfy(payload)


def is_quiet(settings, now=None):
    q = settings.get("quiet_hours", [23, 7])
    if not q or q[0] == q[1]:
        return False
    h = (now or datetime.now(TZ)).hour
    return h >= q[0] or h < q[1] if q[0] > q[1] else q[0] <= h < q[1]


def night_summary(topic, queue):
    app = app_url()
    n = len(queue)
    body = "\n".join("• " + q["title"][:80] for q in queue[:5]) + (f"\n… und {n - 5} weitere" if n > 5 else "")
    ntfy({"topic": topic, "title": f"Über Nacht: {n} neue Treffer", "message": body,
          "click": app or "https://ntfy.sh", "tags": ["sunrise"], "priority": 3})


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
    by_link = {}
    for d in items:
        if d["link"] in by_link and by_link[d["link"]] != d["id"]:
            d["id"] = by_link[d["link"]]
        by_link.setdefault(d["link"], d["id"])
        if d["id"] in merged:
            m = merged[d["id"]]
            m["_rubriken"] += d["_rubriken"]
            for k in ("temp", "discount", "price", "image"):
                if m.get(k) in (None, "") and d.get(k) not in (None, ""):
                    m[k] = d[k]
        else:
            merged[d["id"]] = d

    new = []
    for d in merged.values():
        hits = [w for w in watch if match_watch(d, w)]
        if not hits:
            continue
        d["watch"] = [w["id"] for w in hits]
        if "gewinnspiele" in d["watch"]:
            d["gw"] = gewinnspiel_info(d["_text"])
        if any(w.get("type") == "tarif" for w in hits):
            d["tarif"] = analyse_tarif(d, cfg.get("resale", {}), settings)
        d["compare"] = compare_price(d)
        d["features"] = features(d)
        d["score"] = score(model, d["features"])
        d.pop("_text", None)
        d.pop("_rubriken", None)
        if d["id"] in known:
            k = known[d["id"]]
            k.update({x: d[x] for x in ("score", "tarif", "watch", "features", "compare", "desc", "image", "discount", "code", "until", "gw") if x in d})
            if d.get("temp") is not None:
                k["temp"] = d["temp"]  # Hitze steigt mit der Zeit – immer den neuesten Wert
            continue
        d["found"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        known[d["id"]] = d
        new.append((d, hits))

    pushed = 0
    min_score = settings.get("push_min_score", 0) or 0
    quiet = is_quiet(settings)
    night = store.get("night", [])
    if not quiet and night and send_push and topic:
        try:
            night_summary(topic, night)
            night = []
        except Exception as e:
            print(f"[Push fehlgeschlagen] {e}", file=sys.stderr)
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
        if quiet:
            night.append({"id": d["id"], "title": d["title"]})
            continue
        if send_push and topic:
            try:
                push(topic, d, [w["name"] for w in hits])
                pushed += 1
            except Exception as e:
                print(f"[Push fehlgeschlagen] {e}", file=sys.stderr)

    # Gespeicherte Deals gegen die aktuelle Watchlist prüfen (gelöschte Einträge, neue Ausschlüsse)
    wmap = {w["id"]: w for w in watch}
    for d in list(known.values()):
        d["title"] = re.sub(r"^\s*-?\d+°\s*-\s*", "", d["title"])
        tn, cn = norm(d["title"]), norm(d.get("category"))
        keep = []
        for wid in d.get("watch", []):
            w = wmap.get(wid)
            if not w or any(phrase_in(x, tn) for x in w.get("exclude", [])) \
                    or any(phrase_in(x, cn) for x in w.get("exclude_categories", [])):
                continue
            keep.append(wid)
        if keep:
            d["watch"] = keep
            # ältere Einträge nachträglich anreichern
            txt = d["title"] + " " + (d.get("desc") or "")
            if "gewinnspiele" in keep and not d.get("gw"):
                d["gw"] = gewinnspiel_info(txt)
            if "code" not in d:
                d["code"] = find_code(txt)
            if "until" not in d:
                try:
                    d["until"] = find_until(txt, datetime.fromisoformat(d["pub"]).astimezone(TZ))
                except (KeyError, ValueError):
                    d["until"] = None
            if d.get("tarif") and not any(wmap[w].get("type") == "tarif" for w in keep):
                d.pop("tarif")
        else:
            known.pop(d["id"], None)

    # Tarif-Gewinne mit den aktuellen Verkaufswerten neu rechnen
    resale = cfg.get("resale", {})
    for d in known.values():
        t = d.get("tarif")
        if t:
            tn = norm(d["title"])
            for key in sorted(resale, key=len, reverse=True):
                if phrase_in(key, tn):
                    t["device"] = key
                    break
        if t and t.get("device") in resale:
            t["resale"] = float(resale[t["device"]] or 0) or None
            t["profit"] = None if (t.get("cost") is None or t["resale"] is None) else round(t["resale"] - t["cost"], 2)

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
    now_utc = datetime.now(timezone.utc)
    deals = [d for d in deals if not d.get("until") or datetime.fromisoformat(d["until"]) >= now_utc]
    deals.sort(key=lambda d: d.get("pub", ""), reverse=True)

    # Preisverlauf: günstigster Preis pro Tag und Watchlist-Produkt
    history = load_json(DEALS, {}).get("history", {})
    for w in watch:
        if w.get("type", "product") != "product":
            continue
        live = {d["link"] for d in deals if w["id"] in d.get("watch", [])}
        pts = {p["d"]: p for p in history.get(w["id"], []) if p.get("l") in live or p["d"] < (now_utc - timedelta(days=MAX_AGE_DAYS)).date().isoformat()}
        for d in deals:
            if w["id"] in d.get("watch", []) and d.get("price"):
                day = d.get("pub", "")[:10]
                if day not in pts or d["price"] < pts[day]["p"]:
                    pts[day] = {"d": day, "p": d["price"], "t": d["title"][:90], "l": d["link"]}
        history[w["id"]] = sorted(pts.values(), key=lambda p: p["d"])[-120:]
    history = {k: v for k, v in history.items() if any(w["id"] == k for w in watch)}
    # Bestpreis aus dem (bereinigten) Verlauf – alte Fehltreffer fallen so wieder raus
    best = {}
    for k, pts in history.items():
        valid = [p for p in pts if any(d["link"] == p.get("l") for d in deals)] or pts
        if valid:
            p = min(valid, key=lambda p: p["p"])
            best[k] = {"price": p["p"], "title": p.get("t", ""), "link": p.get("l", ""), "date": p["d"]}
    store = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "feeds": feed_status,
        "votes": len(votes),
        "best": best,
        "history": history,
        "night": night[-50:],
        "deals": deals[:MAX_DEALS],
    }
    save_json(DEALS, store)
    print(f"{len(items)} Einträge geprüft, {len(new)} neue Treffer, {pushed} Pushes gesendet.")
    return store, new


def test_push():
    """Schickt den neuesten Watchlist-Treffer als Probe-Push (Workflow-Option „Test-Push“)."""
    cfg, store = load_json(CONFIG, {}), load_json(DEALS, {"deals": []})
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    personal = {w["id"]: w["name"] for w in cfg.get("watchlist", []) if w.get("type") != "rubrik"}
    ds = [d for d in store.get("deals", []) if any(w in personal for w in d.get("watch", []))] or store.get("deals", [])
    if not topic or not ds:
        print("Kein Push-Thema oder keine Deals.")
        return
    d = ds[0]
    push(topic, d, [personal.get(w, w) for w in d.get("watch", [])] or ["Test"])
    print("Test-Push gesendet:", d["title"])


if __name__ == "__main__":
    if os.environ.get("TEST_PUSH") == "true":
        test_push()
    else:
        run()
