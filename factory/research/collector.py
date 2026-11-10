"""Coleta de notícias via RSS/Atom e extração de texto dos artigos.

Somente métodos públicos: feeds RSS oficiais/editoriais e páginas públicas. Nenhum login,
nenhuma automação de navegador, sem burlar paywall.
"""
from __future__ import annotations

import html
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import feedparser
import requests

from factory.config import settings
from factory.database.db import DB, now_iso
from factory.log import get_logger
from factory.textutil import domain_of, norm

log = get_logger("research")

TIER_RELIABILITY = {"official": 1.0, "press": 0.7, "aggregator": 0.5, "community": 0.2}


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = settings().get("research.user_agent")
    s.headers["Accept-Language"] = "en-US,en;q=0.8,pt-BR;q=0.7"
    return s


def _get(sess: requests.Session, url: str, timeout: int = 25) -> requests.Response | None:
    for attempt in range(3):
        try:
            r = sess.get(url, timeout=timeout, allow_redirects=True)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt * 2)
                continue
            log.info("GET %s -> HTTP %s", url, r.status_code)
            return None
        except requests.RequestException as e:
            log.info("GET %s falhou (%s)", url, e)
            time.sleep(2 ** attempt)
    return None


def _clean(s: str) -> str:
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def _parse_date(entry: Any) -> datetime | None:
    for k in ("published_parsed", "updated_parsed"):
        v = entry.get(k)
        if v:
            return datetime(*v[:6], tzinfo=timezone.utc)
    for k in ("published", "updated"):
        v = entry.get(k)
        if v:
            try:
                return parsedate_to_datetime(v).astimezone(timezone.utc)
            except (TypeError, ValueError):
                pass
    return None


def is_gta6_related(text: str) -> bool:
    t = norm(text)
    return any(norm(k) in t for k in settings().sources["keywords"])


def classify_domain(url: str) -> str | None:
    d = domain_of(url)
    src = settings().sources
    if any(d == od or d.endswith("." + od) for od in src["official_domains"] if "/" not in od):
        return "official"
    if any(d == pd or d.endswith("." + pd) for pd in src["press_domains"]):
        return "press"
    return None


def extract_article(sess: requests.Session, url: str) -> dict[str, str]:
    """Baixa um artigo público e extrai texto principal + metadados og:."""
    r = _get(sess, url)
    if r is None:
        return {}
    page = r.text
    out: dict[str, str] = {"final_url": r.url}
    for prop in ("og:title", "og:description", "og:image", "article:published_time"):
        m = re.search(rf'<meta[^>]+property="{re.escape(prop)}"[^>]+content="([^"]*)"', page) or \
            re.search(rf'<meta[^>]+content="([^"]*)"[^>]+property="{re.escape(prop)}"', page)
        if m:
            out[prop] = html.unescape(m.group(1)).strip()
    try:
        import trafilatura
        txt = trafilatura.extract(page, include_comments=False, include_tables=False, favor_precision=True) or ""
    except Exception as e:  # extração é melhor-esforço
        log.info("trafilatura falhou em %s: %s", url, e)
        txt = ""
    out["text"] = txt[:7000]
    return out


def _resolve_google_news(sess: requests.Session, link: str) -> str:
    """Resolve o link do Google News para a URL original do veículo.

    O Google News não redireciona mais via HTTP; a página do artigo traz uma assinatura
    (data-n-a-sg/ts) que o próprio front-end troca pela URL real via `batchexecute`.
    Se falhar, devolvemos o link do Google News (o item fica com evidência mais fraca).
    """
    if "news.google.com" not in link:
        return link
    import json as _json
    try:
        m = re.search(r"/articles/([^?/]+)", link)
        if not m:
            return link
        art_id = m.group(1)
        page = sess.get(f"https://news.google.com/rss/articles/{art_id}", timeout=20).text
        sg = re.search(r'data-n-a-sg="([^"]+)"', page)
        ts = re.search(r'data-n-a-ts="([^"]+)"', page)
        if not (sg and ts):
            return link
        inner = ["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1],
                                "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0], art_id, int(ts.group(1)), sg.group(1)]
        payload = [[["Fbv4je", _json.dumps(inner), None, "generic"]]]
        r = sess.post("https://news.google.com/_/DotsSplashUi/data/batchexecute",
                      data={"f.req": _json.dumps(payload)}, timeout=20,
                      headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
        body = r.text.split("\n\n", 1)[-1]
        data = _json.loads(body)
        url = _json.loads(data[0][2])[1]
        if isinstance(url, str) and url.startswith("http"):
            return url
    except (requests.RequestException, ValueError, IndexError, TypeError, KeyError) as e:
        log.info("Não foi possível resolver link do Google News (%s)", e)
    return link


def collect(db: DB, max_per_feed: int = 30) -> dict[str, int]:
    """Varre todas as fontes configuradas e grava itens novos. Retorna contagem por fonte."""
    cfg = settings()
    sess = _session()
    max_age = timedelta(days=int(cfg.get("research.max_article_age_days", 21)))
    stats: dict[str, int] = {}
    for feed in cfg.sources["feeds"]:
        r = _get(sess, feed["url"])
        if r is None:
            stats[feed["id"]] = -1
            continue
        parsed = feedparser.parse(r.content)
        new = 0
        for e in parsed.entries[:max_per_feed]:
            title = _clean(e.get("title", ""))
            summary = _clean(e.get("summary", "") or e.get("description", ""))
            link = e.get("link", "")
            if not title or not link:
                continue
            if feed.get("keyword_filter") or feed["tier"] == "aggregator":
                if not is_gta6_related(f"{title} {summary}"):
                    continue
            pub = _parse_date(e)
            if pub and datetime.now(timezone.utc) - pub > max_age:
                continue
            source_name = feed["name"]
            tier = feed["tier"]
            if "news.google.com" in link:
                # Google News: "Título - Veículo"; tier real depende do domínio original
                src = e.get("source", {})
                if isinstance(src, dict) and src.get("title"):
                    source_name = src["title"]
                    title = re.sub(rf"\s+-\s+{re.escape(src['title'])}$", "", title)
                link = _resolve_google_news(sess, link)
            dom = domain_of(link)
            if feed.get("require_domain") and feed["require_domain"] not in dom:
                # Para a fonte oficial via Google News, aceitamos se o veículo indicado é a Rockstar
                if not (isinstance(e.get("source"), dict) and "rockstargames.com" in str(e["source"].get("href", ""))):
                    continue
            if tier == "aggregator":
                tier = classify_domain(link) or "aggregator"
            if db.one("SELECT 1 FROM news_items WHERE url=?", link):
                continue
            body, image = "", ""
            if cfg.get("research.fetch_article_text") and tier in ("official", "press") and "youtube.com" not in dom \
                    and "news.google.com" not in link:
                art = extract_article(sess, link)
                # Newswire da Rockstar é renderizada no cliente: o HTML só traz o lead (og:description).
                body = art.get("text", "") or art.get("og:description", "")
                image = art.get("og:image", "")
                if art.get("og:title") and tier == "official":
                    title = re.sub(r"\s+-\s+Rockstar Games$", "", art["og:title"])
                if not summary and art.get("og:description"):
                    summary = art["og:description"]
            if "youtube.com" in dom:
                # Feed do YouTube: a descrição do vídeo é o texto oficial
                body = _clean(e.get("media_description", "") or summary)
                thumbs = e.get("media_thumbnail") or []
                image = thumbs[0]["url"] if thumbs else ""
            db.x("INSERT OR IGNORE INTO news_items(url,title,title_norm,summary,body,source_id,source_name,tier,domain,"
                 "published_at,collected_at,reliability,image_url) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 link, title, norm(title), summary[:2000], body, feed["id"], source_name, tier, dom,
                 pub.isoformat() if pub else None, now_iso(), TIER_RELIABILITY[tier], image)
            new += 1
        stats[feed["id"]] = new
        log.info("Fonte %-24s novos=%d", feed["id"], new)
    return stats


def collect_official_pages(db: DB) -> int:
    """Captura páginas oficiais (textos de personagens/locais) como evidência atemporal."""
    sess = _session()
    n = 0
    for page in settings().sources.get("official_pages", []):
        art = extract_article(sess, page["url"])
        text = art.get("text", "")
        if len(text) < 200:
            log.warning("Página oficial %s sem texto extraível", page["url"])
            continue
        title = art.get("og:title") or page["id"]
        existing = db.one("SELECT id, body FROM news_items WHERE url=?", page["url"])
        if existing:
            if existing["body"] != text:
                db.x("UPDATE news_items SET body=?, collected_at=? WHERE id=?", text, now_iso(), existing["id"])
            continue
        db.x("INSERT INTO news_items(url,title,title_norm,summary,body,source_id,source_name,tier,domain,"
             "published_at,collected_at,reliability,image_url,confirmation,cluster_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             page["url"], title, norm(title), art.get("og:description", "")[:2000], text, page["id"],
             "Rockstar Games (site oficial)", "official", domain_of(page["url"]), None, now_iso(), 1.0,
             art.get("og:image", ""), "CONFIRMADO", f"official:{page['id']}")
        n += 1
    return n
