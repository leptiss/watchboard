"""
WatchBoard — collecte + classement + historique des annonces.

Lit les alertes Gmail (Chrono24 et Leboncoin), extrait les annonces,
les classe automatiquement par référence de montre (via le titre),
et fusionne le résultat avec l'historique existant dans data/listings.json
(rien n'est jamais effacé, seulement ajouté — dédoublonnage par URL).

Secrets attendus en variables d'environnement (fournis par GitHub Actions) :
    GMAIL_ADDRESS
    GMAIL_APP_PASSWORD
"""

import imaplib
import email
import json
import os
import re
from email.header import decode_header
from pathlib import Path

from bs4 import BeautifulSoup

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")

LABELS = ["CHRONO24 ALERT", "LBC ALERT"]  # adapte si tes libellés Gmail sont nommés autrement
MAX_EMAILS_PER_LABEL = 15
OUTPUT_PATH = Path("data/listings.json")

# ---------------------------------------------------------------------------
# Table de classement par référence — matché contre le titre de l'annonce.
# "exact" = la référence telle qu'elle apparaît chez les vendeurs (prioritaire).
# "keywords" = repli si la référence exacte n'est pas écrite dans le titre.
# ---------------------------------------------------------------------------
REF_RULES = {
    "bb-chrono":    {"exact": ["79360N"], "keywords": ["black bay chrono", "bb chrono"]},
    "bb-58":        {"exact": ["M79030N-0001", "79030N"], "keywords": ["black bay 58", "bb58", "bb 58"]},
    "reverso":      {"exact": ["Q2618430"], "keywords": ["reverso classic small", "reverso small"]},
    "bb-gmt-pepsi": {"exact": ["79830RB"], "keywords": ["black bay gmt", "pepsi"]},
    "br05-gmt":     {"exact": ["BR05G-BL-ST/SRB", "BR05G-BL-ST"], "keywords": ["br 05 gmt", "br05 gmt"]},
    "gp-wwtc":      {"exact": ["49805"], "keywords": ["ww.tc", "wwtc", "world timer"]},
}
UNSORTED_KEY = "unsorted"


def classify(title: str) -> str:
    t = title.lower()
    for ref_id, rules in REF_RULES.items():
        for exact in rules["exact"]:
            if exact.lower() in t:
                return ref_id
    for ref_id, rules in REF_RULES.items():
        for kw in rules["keywords"]:
            if kw.lower() in t:
                return ref_id
    return UNSORTED_KEY


def decode_str(raw: str) -> str:
    parts = decode_header(raw)
    out = ""
    for text, enc in parts:
        out += text.decode(enc or "utf-8", errors="replace") if isinstance(text, bytes) else text
    return out


def get_html_body(msg) -> str | None:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
        return None
    if msg.get_content_type() == "text/html":
        return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", errors="replace")
    return None


def parse_chrono24(html: str):
    """Retourne une liste de dicts {title, price, meta, url} pour un email Chrono24."""
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for table in soup.select("table.article-inner-table"):
        link = table.find("a", href=True)
        if not link:
            continue
        url = link["href"].split("?")[0]
        bold = table.find("b")
        title = bold.get_text(strip=True) if bold else "Annonce Chrono24"
        price_tag = table.find("p", string=re.compile("€"))
        price = price_tag.get_text(strip=True) if price_tag else "Prix sur demande"
        country_p = table.find_all("p")[-1] if table.find_all("p") else None
        meta = country_p.get_text(strip=True) if country_p else ""
        results.append({"source": "Chrono24", "title": title, "price": price, "meta": meta, "url": url})
    return results


def parse_leboncoin(html: str, search_name: str):
    """Retourne une liste de dicts {title, price, meta, url} pour un email Leboncoin."""
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for a in soup.select('a[href*="/vi/"]'):
        href = a["href"]
        if not href.startswith("http"):
            href = "https://www.leboncoin.fr" + href
        text = a.get_text(" ", strip=True)
        m = re.match(r"^(.*?)(\d[\d\s]*€)\s*(.*)$", text)
        if not m:
            continue
        title, price, lieu = m.group(1).strip(" -–"), m.group(2).strip(), m.group(3).strip()
        results.append({
            "source": "Leboncoin",
            "title": title,
            "price": price,
            "meta": f"{search_name} · {lieu}" if search_name else lieu,
            "url": href,
        })
    return results


def fetch_label(imap, label: str):
    status, _ = imap.select(f'"{label}"', readonly=True)
    if status != "OK":
        print(f"Label introuvable, ignoré : {label}")
        return []

    status, data = imap.search(None, "ALL")
    ids = data[0].split()
    ids = ids[-MAX_EMAILS_PER_LABEL:] if len(ids) > MAX_EMAILS_PER_LABEL else ids

    listings = []
    for eid in ids:
        status, msg_data = imap.fetch(eid, "(RFC822)")
        msg = email.message_from_bytes(msg_data[0][1])
        subject = decode_str(msg.get("Subject", ""))

        html = get_html_body(msg)
        if not html:
            continue

        if "chrono24" in label.lower():
            if "recherche sauvegardée" not in subject.lower():
                continue
            listings.extend(parse_chrono24(html))
        else:
            if "nouveaux résultats" not in subject.lower():
                continue
            search_name = subject.split(":")[0].strip()
            listings.extend(parse_leboncoin(html, search_name))

    return listings


def merge_and_save(new_listings):
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if OUTPUT_PATH.exists():
        existing = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))

    added = 0
    for item in new_listings:
        ref_id = classify(item["title"])
        bucket = existing.setdefault(ref_id, [])
        if any(l["url"] == item["url"] for l in bucket):
            continue  # déjà en historique, on ne duplique pas
        bucket.append({
            "source": item["source"],
            "title": item["title"],
            "meta": item["meta"],
            "price": item["price"],
            "date": item.get("date") or __import__("datetime").date.today().isoformat(),
            "url": item["url"],
        })
        added += 1

    OUTPUT_PATH.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{added} nouvelle(s) annonce(s) ajoutée(s) à l'historique. Total par case :")
    for k, v in existing.items():
        print(f"  {k}: {len(v)}")


def main():
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        raise SystemExit("GMAIL_ADDRESS et GMAIL_APP_PASSWORD doivent être définis (secrets GitHub Actions).")

    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)

    all_new = []
    for label in LABELS:
        all_new.extend(fetch_label(imap, label))

    imap.logout()
    merge_and_save(all_new)


if __name__ == "__main__":
    main()
