#!/usr/bin/env python3
"""Coleta RSS/Atom estáveis e grava data/news.json sem dependências externas.

Foco: notícias fiscais, tributárias, reforma tributária e contábeis.
O coletor é tolerante a falhas de fonte: se uma origem cair, mantém o
instantâneo anterior e registra o status em `sourcesFailed`/`sources`.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET
import ssl

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "collector" / "sources.json"
OUTPUT = ROOT / "data" / "news.json"
MAX_ITEMS = 300
PER_SOURCE_LIMIT = 35
TIMEOUT = 25

RELEVANCE_TERMS = {
    "tribut": 5,
    "fiscal": 5,
    "imposto": 5,
    "receita federal": 5,
    "reforma tributaria": 7,
    "ibs": 6,
    "cbs": 6,
    "imposto seletivo": 6,
    "split payment": 6,
    "simples nacional": 6,
    "mei": 4,
    "irpf": 5,
    "irpj": 5,
    "csll": 5,
    "pis": 4,
    "cofins": 4,
    "icms": 6,
    "iss": 6,
    "ipi": 5,
    "itr": 5,
    "iof": 5,
    "carf": 6,
    "contencioso": 4,
    "aduaneir": 5,
    "importacao": 4,
    "exportacao": 4,
    "nfe": 4,
    "nf-e": 4,
    "nota fiscal": 5,
    "sped": 5,
    "efd": 4,
    "ecf": 4,
    "dctf": 4,
    "dirf": 4,
    "esocial": 3,
    "fgts": 3,
    "inss": 3,
    "contabil": 4,
    "contabeis": 4,
    "obrigacao acessoria": 5,
    "beneficio fiscal": 5,
    "transacao tributaria": 5,
    "divida ativa": 5,
    "pgfn": 5,
    "sefaz": 4,
    "confaz": 5,
    "contribuinte": 3,
    "arrecadacao": 4,
    "sonegacao": 4,
    "malha fina": 4,
    "diario oficial": 5,
    "dou": 5,
    "convenio icms": 6,
    "ajuste sinief": 6,
    "protocolo icms": 5,
    "itcd": 4,
    "ipva": 3,
    "taxa": 3,
    "municipios": 3,
    "minas gerais": 3,
    "regularize": 4,
}

SSL_CONTEXT = ssl.create_default_context()

GLOBAL_EXCLUDE_TERMS = {
    "eleicao",
    "votar",
    "urna",
    "video",
    "audio",
    "saude mental",
    "consignado",
    "palestra",
    "congresso",
    "conbcon",
    "curso",
    "webinar",
    "homenagem",
    "sessao especial",
    "carreira",
    "gestao de pessoas",
    "contabilidade e ia",
    "futebol",
    "loteria",
    "celebridade",
}

CATEGORY_TERMS = [
    ("reforma", ("reforma tributaria", "ibs", "cbs", "imposto seletivo", "split payment", "comite gestor")),
    ("carf", ("carf", "conselho administrativo de recursos fiscais", "contencioso administrativo")),
    ("icms", ("icms", "confaz", "sefaz", "substituicao tributaria", "difal")),
    ("municipal", ("iss", "municipio", "municipal")),
    ("aduaneiro", ("aduaneir", "importacao", "exportacao", "comercio exterior", "despachante aduaneiro")),
    ("legislacao", ("lei", "decreto", "portaria", "instrucao normativa", "medida provisoria", "dou", "diario oficial")),
    ("judicial", ("stf", "stj", "supremo", "tribunal", "repercussao geral", "tema ")),
    ("receita", ("receita federal", "simples nacional", "irpf", "irpj", "pis", "cofins", "pgfn", "malha fina", "arrecadacao")),
]

TRACKING_PARAMS_PREFIXES = ("utm_",)
TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid"}

NON_CONTENT_TITLES = {
    "brasil", "servicos", "legislacao", "noticias", "rss", "voltar",
    "proxima", "anterior", "contato", "institucional", "quem somos",
    "ouvidoria", "acesso a informacao",
}
NON_CONTENT_PATH_PARTS = {
    "institucional", "contato", "quem-somos", "menu", "ouvidoria",
    "acesso-a-informacao",
}
ALLOWED_DATE_EVIDENCE = {
    "feed:pubdate", "feed:published", "feed:date",
    "html:time[datetime]", "html:time", "html:configured-regex",
    "html:detail:configured-regex", "dou:pubDate",
}

IMPACT_RULES = {
    "caixa": ("split payment", "fluxo de caixa", "caixa", "credito", "crédito", "ressarcimento", "aliquota", "alíquota", "precificacao", "precificação"),
    "obrigacao": ("efd", "reinf", "dctf", "pgdas", "nfe", "nf-e", "nota fiscal", "sped", "declaracao", "declaração", "obrigacao", "obrigação", "ditr", "dirbi"),
    "risco": ("stf", "stj", "carf", "multa", "autuacao", "autuação", "debito", "débito", "cobranca", "cobrança", "risco", "rejeicao", "rejeição"),
    "reforma": ("reforma tributaria", "ibs", "cbs", "imposto seletivo", "split payment", "comite gestor", "comitê gestor"),
    "judicial": ("stf", "stj", "tribunal", "tese", "repercussao geral", "repercussão geral"),
    "prazo": ("vence", "prazo", "ate ", "até ", "amanha", "amanhã"),
}

IMPACT_LABELS = {
    "caixa": "Impacto no caixa",
    "obrigacao": "Obrigação acessória",
    "risco": "Risco fiscal",
    "oportunidade": "Oportunidade consultiva",
    "reforma": "Reforma Tributária",
    "judicial": "Decisão judicial",
    "prazo": "Prazo urgente",
}


def clean(value: str | None) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def norm(value: str | None) -> str:
    value = clean(value).lower()
    value = unicodedata.normalize("NFD", value)
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
    return value


def child_text(node: ET.Element, names: tuple[str, ...]) -> str:
    value, _ = child_value(node, names)
    return value


def child_value(node: ET.Element, names: tuple[str, ...]) -> tuple[str, str | None]:
    wanted = set(names)
    for child in list(node):
        local = child.tag.rsplit("}", 1)[-1].lower()
        if local in wanted and child.text:
            return clean(child.text), local
    return "", None


def child_attr(node: ET.Element, tag: str, attr: str, rel: str | None = None) -> str:
    for child in list(node):
        local = child.tag.rsplit("}", 1)[-1].lower()
        if local != tag:
            continue
        if rel and child.attrib.get("rel") not in (None, rel):
            continue
        if child.attrib.get(attr):
            return clean(child.attrib[attr])
    return ""


def canonical_url(url: str) -> str:
    parts = urlsplit(clean(url))
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k not in TRACKING_PARAMS and not any(k.startswith(prefix) for prefix in TRACKING_PARAMS_PREFIXES)
    ]
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path.rstrip("/") or "/", urlencode(query), ""))


def is_valid_content_link(href: str, title: str, source: dict) -> bool:
    raw_href = clean(href).strip()
    if not raw_href or raw_href.startswith("#"):
        return False
    url = canonical_url(urljoin(source["url"], raw_href))
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return False
    source_host = urlsplit(source["url"]).hostname or ""
    target_host = parts.hostname or ""
    if target_host != source_host and not target_host.endswith(f".{source_host}"):
        return False
    if norm(title) in NON_CONTENT_TITLES:
        return False
    path_parts = {part for part in norm(parts.path).split("/") if part}
    if path_parts & NON_CONTENT_PATH_PARTS:
        return False
    if url == canonical_url(source["url"]):
        return False
    return True


def parse_date(value: str) -> str | None:
    raw = clean(value)
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        pass
    try:
        mail_raw = re.sub(r"\bBRT\b", "-0300", raw)
        dt = parsedate_to_datetime(mail_raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%d/%m/%Y %H:%M", "%d.%m.%y"):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except Exception:
            continue
    return None


def relevance_score(title: str, summary: str, source: dict) -> int:
    blob = norm(f"{title} {summary}")
    if any(term_matches(blob, term) for term in GLOBAL_EXCLUDE_TERMS):
        return -100
    score = int(source.get("baseScore", 0))
    for term, weight in RELEVANCE_TERMS.items():
        if term_matches(blob, term):
            score += weight
    for term in source.get("includeKeywords", []):
        if term_matches(blob, term):
            score += int(source.get("includeWeight", 6))
    for term in source.get("excludeKeywords", []):
        if term_matches(blob, term):
            return -100
    return score


def term_matches(blob: str, term: str) -> bool:
    """Casa termos sem aceitar falso positivo como 'fiscal' dentro de 'oficial'."""
    term_norm = norm(term)
    if not term_norm:
        return False
    # Radicais intencionais para cobrir flexões: tributário/tributária, aduaneiro/aduaneira etc.
    if term_norm in {"tribut", "aduaneir", "empres"}:
        return term_norm in blob
    return re.search(rf"(?<![a-z0-9]){re.escape(term_norm)}(?![a-z0-9])", blob) is not None


def classify(title: str, summary: str, source: dict) -> str:
    blob = norm(f"{title} {summary} {source.get('name', '')}")
    for category, terms in CATEGORY_TERMS:
        if any(term in blob for term in terms):
            return category
    return source.get("category", "receita")



def strategic_analysis(
    title: str,
    summary: str,
    category: str,
    score: int,
    deadline: str | None = None,
    now: datetime | None = None,
) -> dict:
    blob = norm(f"{title} {summary} {category}")
    tags: list[str] = []
    for key, terms in IMPACT_RULES.items():
        if any(term_matches(blob, term) if term.strip() == term else term in blob for term in terms):
            tags.append(key)
    parsed_deadline = parse_date(deadline or "")
    now = now or datetime.now(timezone.utc)
    has_future_deadline = bool(
        parsed_deadline
        and datetime.fromisoformat(parsed_deadline).astimezone(timezone.utc) > now.astimezone(timezone.utc)
    )
    if not has_future_deadline and "prazo" in tags:
        tags.remove("prazo")
    if category in {"reforma", "cgibs"} and "reforma" not in tags:
        tags.append("reforma")
    if category == "judicial" and "judicial" not in tags:
        tags.append("judicial")
    if tags or any(term_matches(blob, term) for term in ("simples nacional", "mei", "empresas", "contabilidade", "contabil", "governanca", "governança")):
        tags.append("oportunidade")
    tags = list(dict.fromkeys(tags))

    opportunity_score = score + len(tags) * 5
    if "prazo" in tags:
        opportunity_score += 18
    if "caixa" in tags:
        opportunity_score += 12
    if "risco" in tags:
        opportunity_score += 10
    if "reforma" in tags:
        opportunity_score += 8

    if opportunity_score >= 55 or "prazo" in tags:
        priority = "alta"
    elif opportunity_score >= 32 or "caixa" in tags or "risco" in tags:
        priority = "media"
    else:
        priority = "baixa"

    who = "Empresas e contabilidades que acompanham rotina fiscal."
    if "reforma" in tags:
        who = "Empresas no regime regular, contabilidades e áreas fiscal/financeira em adaptação ao IBS/CBS."
    if "obrigacao" in tags:
        who = "Contabilidades, financeiro e empresas com obrigações acessórias no período."
    if "caixa" in tags:
        who = "Empresas com vendas B2B, estoque, créditos tributários ou necessidade de precificação."
    if "judicial" in tags:
        who = "Empresas com teses tributárias, créditos, autuações ou operações similares."

    action = "Ler a fonte original, salvar evidências e avaliar se há impacto em clientes ativos."
    if "prazo" in tags:
        action = "Checar o calendário fiscal, responsáveis e documentos necessários antes do vencimento."
    elif "caixa" in tags:
        action = "Simular impacto no fluxo de caixa, preços, créditos e contratos antes de 2027."
    elif "reforma" in tags:
        action = "Mapear processos, documentos fiscais, sistemas e decisões comerciais afetadas pela transição IBS/CBS."
    elif "risco" in tags:
        action = "Revisar exposição fiscal, documentação de suporte e oportunidade de tese/regularização."

    if "prazo" in tags:
        why = "há risco de perda de prazo ou multa"
    elif "caixa" in tags:
        why = "pode alterar fluxo de caixa e precificação"
    elif "reforma" in tags:
        why = "antecipa adaptação à Reforma Tributária"
    elif "risco" in tags:
        why = "reduz exposição fiscal e contencioso"
    else:
        why = "gera pauta consultiva para orientar clientes"

    result = {
        "impactTags": tags,
        "impactLabels": [IMPACT_LABELS[t] for t in tags if t in IMPACT_LABELS],
        "priority": priority,
        "opportunityScore": opportunity_score,
        "whoAffected": who,
        "recommendedAction": action,
        "whyItMatters": why,
        "contentAngles": {
            "linkedin": f"Explique o impacto de '{title}' para empresas e contabilidades, com orientação prática e CTA consultivo.",
            "reels": f"Use '{title}' como gancho, explique o risco em 20 segundos e finalize com uma ação recomendada.",
            "whatsapp": f"Alerta curto para clientes: resumo, impacto e próximo passo sobre '{title}'.",
        },
    }
    if has_future_deadline:
        result["deadline"] = parsed_deadline
    return result

def iter_entries(root: ET.Element) -> list[ET.Element]:
    entries = []
    for node in root.iter():
        local = node.tag.rsplit("}", 1)[-1].lower()
        if local in {"item", "entry"}:
            entries.append(node)
    return entries


def parse_feed(raw: bytes, source: dict, collected_at: datetime | None = None) -> list[dict]:
    root = ET.fromstring(raw)
    result = []
    collected_at = collected_at or datetime.now(timezone.utc)
    min_score = int(source.get("minScore", 5))
    for node in iter_entries(root)[:PER_SOURCE_LIMIT]:
        title = child_text(node, ("title",))
        link = child_text(node, ("link",)) or child_attr(node, "link", "href", "alternate") or child_attr(node, "link", "href")
        summary = child_text(node, ("description", "summary", "content", "encoded"))
        published, published_field = child_value(
            node, ("pubdate", "published", "date", "dc:date")
        )
        if not title or not link or not is_valid_content_link(link, title, source):
            continue
        published_at = parse_date(published)
        if published_at is None or published_field is None:
            continue
        if datetime.fromisoformat(published_at) > collected_at.astimezone(timezone.utc):
            continue
        score = relevance_score(title, summary, source)
        if score < min_score:
            continue
        url = canonical_url(link)
        category = classify(title, summary, source)
        item_summary = (summary or "Publicação coletada automaticamente. Confirme o teor na fonte original.")[:360]
        item = {
            "id": hashlib.sha256(url.encode()).hexdigest()[:16],
            "title": title[:220],
            "url": url,
            "source": source["name"],
            "category": category,
            "summary": item_summary,
            "publishedAt": published_at,
            "publicationDateEvidence": f"feed:{published_field}",
            "collectedAt": collected_at.astimezone(timezone.utc).isoformat(),
            "score": score,
        }
        item.update(strategic_analysis(title, item_summary, category, score))
        result.append(item)
    return result



def parse_html_links(raw: bytes, source: dict, collected_at: datetime | None = None) -> list[dict]:
    """Extrai somente resultados HTML com data de publicação no mesmo bloco."""
    page_url = source["url"]
    html_text = raw.decode(source.get("encoding", "utf-8"), "replace")
    collected_at = collected_at or datetime.now(timezone.utc)
    html_text = re.sub(
        r"<script[\s\S]*?</script>|<style[\s\S]*?</style>",
        " ",
        html_text,
        flags=re.I,
    )
    container_tag = source.get("htmlContainerTag")
    if container_tag:
        tag = re.escape(container_tag)
        contexts = re.findall(
            rf"<{tag}\b[^>]*>[\s\S]*?</{tag}>", html_text, flags=re.I
        )
    else:
        contexts = re.findall(r"<a\b[^>]*>[\s\S]*?</a>", html_text, flags=re.I)

    result = []
    seen = set()
    allow = source.get("urlAllowRegex")
    deny = source.get("urlDenyRegex")
    for context in contexts[: int(source.get("scanLimit", 450))]:
        link_match = re.search(
            r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>([\s\S]*?)</a>",
            context,
            flags=re.I,
        )
        if not link_match:
            continue
        href, inner = link_match.groups()
        title_match = None
        if source.get("htmlTitleRegex"):
            title_match = re.search(source["htmlTitleRegex"], context, flags=re.I)
        title = clean(title_match.group(1) if title_match else inner)
        if not title or len(title) < int(source.get("minTitleLength", 8)):
            continue
        if not is_valid_content_link(href, title, source):
            continue
        url = canonical_url(urljoin(page_url, href))
        if url in seen or url == canonical_url(page_url):
            continue
        if allow and not re.search(allow, url, flags=re.I):
            continue
        if deny and re.search(deny, url, flags=re.I):
            continue

        published_at = None
        date_evidence = None
        time_attr = re.search(
            r"<time\b[^>]*datetime=[\"']([^\"']+)[\"'][^>]*>", context, flags=re.I
        )
        if time_attr:
            published_at = parse_date(time_attr.group(1))
            date_evidence = "html:time[datetime]"
        if published_at is None:
            time_text = re.search(r"<time\b[^>]*>([\s\S]*?)</time>", context, flags=re.I)
            if time_text:
                published_at = parse_date(clean(time_text.group(1)))
                date_evidence = "html:time"
        if published_at is None and source.get("htmlPublishedDateRegex"):
            date_match = re.search(
                source["htmlPublishedDateRegex"], context, flags=re.I
            )
            if date_match:
                published_at = parse_date(date_match.group(1))
                date_evidence = "html:configured-regex"
        if published_at is None or date_evidence is None:
            continue
        if datetime.fromisoformat(published_at) > collected_at.astimezone(timezone.utc):
            continue

        summary = clean(f"Publicação monitorada em {source['name']}: {title}")
        score = relevance_score(title, summary, source)
        if score < int(source.get("minScore", 5)):
            continue
        seen.add(url)
        category = classify(title, summary, source)
        item = {
            "id": hashlib.sha256(url.encode()).hexdigest()[:16],
            "title": title[:220],
            "url": url,
            "source": source["name"],
            "category": category,
            "summary": summary[:360],
            "publishedAt": published_at,
            "publicationDateEvidence": date_evidence,
            "collectedAt": collected_at.astimezone(timezone.utc).isoformat(),
            "score": score,
        }
        item.update(strategic_analysis(title, summary, category, score))
        result.append(item)
        if len(result) >= int(source.get("htmlLimit", PER_SOURCE_LIMIT)):
            break
    return result


def _dou_payload(raw: bytes, source: dict) -> dict | None:
    text = raw.decode(source.get("encoding", "utf-8"), "replace")
    match = re.search(
        r"<[^>]+id=[\"'][^\"']*BuscaDouPortlet_params[\"'][^>]*>([\s\S]*?)</[^>]+>",
        text,
        flags=re.I,
    )
    if not match:
        return None
    try:
        payload = json.loads(html.unescape(match.group(1)).strip())
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload.get("jsonArray"), list) else None


def parse_dou_html(raw: bytes, source: dict, collected_at: datetime | None = None) -> list[dict]:
    """Extrai atos do JSON oficial embutido na busca do DOU."""
    payload = _dou_payload(raw, source)
    if payload is None:
        return []
    collected_at = collected_at or datetime.now(timezone.utc)
    result = []
    for entry in payload["jsonArray"][: int(source.get("htmlLimit", PER_SOURCE_LIMIT))]:
        title = clean(entry.get("title"))
        slug = clean(entry.get("urlTitle"))
        published_at = parse_date(entry.get("pubDate", ""))
        if not title or not slug or published_at is None:
            continue
        if datetime.fromisoformat(published_at) > collected_at.astimezone(timezone.utc):
            continue
        url = canonical_url(urljoin("https://www.in.gov.br/web/dou/-/", slug))
        if not is_valid_content_link(url, title, source):
            continue
        summary = clean(entry.get("content"))
        edition = clean(entry.get("editionNumber"))
        page = clean(entry.get("numberPage"))
        provenance = " · ".join(
            part
            for part in (
                f"Edição {edition}" if edition else "",
                f"p. {page}" if page else "",
            )
            if part
        )
        if provenance:
            summary = clean(f"{summary} {provenance}")
        score = relevance_score(title, summary, source)
        if score < int(source.get("minScore", 5)):
            continue
        category = classify(title, summary, source)
        item = {
            "id": hashlib.sha256(url.encode()).hexdigest()[:16],
            "title": title[:220],
            "url": url,
            "source": source["name"],
            "category": category,
            "summary": summary[:360],
            "publishedAt": published_at,
            "publicationDateEvidence": "dou:pubDate",
            "collectedAt": collected_at.astimezone(timezone.utc).isoformat(),
            "score": score,
        }
        item.update(strategic_analysis(title, summary, category, score))
        result.append(item)
    return result


def _fetch_detail(url: str, source: dict) -> bytes:
    req = Request(
        url,
        headers={
            "User-Agent": "RadarFiscal/2.0 (fontes fiscais públicas)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urlopen(
        req,
        timeout=int(source.get("timeout", TIMEOUT)),
        context=SSL_CONTEXT,
    ) as response:
        return response.read(int(source.get("detailMaxBytes", 700_000)))


def _confaz_act_date(title: str) -> datetime | None:
    match = re.search(
        r"\bDE\s+([0-3]?\d)\s+DE\s+([A-ZÇ]+)\s+DE\s+(20\d{2})\b",
        title,
        flags=re.I,
    )
    if not match:
        return None
    months = {
        "janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4,
        "maio": 5, "junho": 6, "julho": 7, "agosto": 8,
        "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
    }
    try:
        return datetime(
            int(match.group(3)), months[norm(match.group(2))], int(match.group(1)),
            tzinfo=timezone.utc,
        )
    except (KeyError, ValueError):
        return None


def parse_html_detail_links(
    raw: bytes,
    source: dict,
    collected_at: datetime | None = None,
    detail_fetcher=None,
) -> list[dict]:
    """Segue links permitidos e exige título e data oficial na página de detalhe."""
    collected_at = collected_at or datetime.now(timezone.utc)
    detail_fetcher = detail_fetcher or (lambda url: _fetch_detail(url, source))
    text = raw.decode(source.get("encoding", "utf-8"), "replace")
    tag = re.escape(source.get("htmlContainerTag", "tr"))
    contexts = re.findall(rf"<{tag}\b[^>]*>[\s\S]*?</{tag}>", text, flags=re.I)
    title_pattern = source.get("htmlDetailTitleRegex")
    date_pattern = source.get("htmlPublishedDateRegex")
    allow = source.get("urlAllowRegex")
    deny = source.get("urlDenyRegex")
    if not title_pattern or not date_pattern or not allow:
        return []

    result = []
    seen = set()
    source_year_match = re.search(r"/(20\d{2})/\1/?$", urlsplit(source["url"]).path)
    source_year = source_year_match.group(1) if source_year_match else None
    for context in contexts[: int(source.get("scanLimit", 450))]:
        anchor = re.search(r"<a\b([^>]*)>([\s\S]*?)</a>", context, flags=re.I)
        if not anchor:
            continue
        attrs, inner = anchor.groups()
        href_match = re.search(r"href=[\"']([^\"']+)[\"']", attrs, flags=re.I)
        attr_title = re.search(r"title=[\"']([^\"']+)[\"']", attrs, flags=re.I)
        if not href_match:
            continue
        href = href_match.group(1)
        link_title = clean(attr_title.group(1) if attr_title else inner)
        url = canonical_url(urljoin(source["url"], href))
        linked_year_match = re.search(r"/(20\d{2})/", urlsplit(url).path)
        if source_year and (not linked_year_match or linked_year_match.group(1) != source_year):
            continue
        if url in seen or not is_valid_content_link(href, link_title, source):
            continue
        if not re.search(allow, url, flags=re.I) or (deny and re.search(deny, url, flags=re.I)):
            continue
        detail = detail_fetcher(url)
        detail_text = detail.decode(source.get("encoding", "utf-8"), "replace")
        title_match = re.search(title_pattern, detail_text, flags=re.I)
        date_match = re.search(date_pattern, detail_text, flags=re.I)
        if not title_match or not date_match:
            continue
        title = clean(title_match.group(1))
        published_at = parse_date(date_match.group(1))
        if not title or published_at is None:
            continue
        title_year_match = re.search(r"\b(20\d{2})\b", title)
        if source_year and (
            not title_year_match or title_year_match.group(1) != source_year
        ):
            continue
        published_date = datetime.fromisoformat(published_at)
        if title_year_match:
            title_year = int(title_year_match.group(1))
            if published_date.year < title_year or published_date.year > title_year + 1:
                continue
        act_date = _confaz_act_date(title)
        if act_date is not None and published_date < act_date:
            continue
        if datetime.fromisoformat(published_at) > collected_at.astimezone(timezone.utc):
            continue
        summary = clean(f"Publicação oficial monitorada em {source['name']}: {title}")
        score = relevance_score(title, summary, source)
        if score < int(source.get("minScore", 5)):
            continue
        seen.add(url)
        category = classify(title, summary, source)
        item = {
            "id": hashlib.sha256(url.encode()).hexdigest()[:16],
            "title": title[:220],
            "url": url,
            "source": source["name"],
            "category": category,
            "summary": summary[:360],
            "publishedAt": published_at,
            "publicationDateEvidence": "html:detail:configured-regex",
            "collectedAt": collected_at.astimezone(timezone.utc).isoformat(),
            "score": score,
        }
        item.update(strategic_analysis(title, summary, category, score))
        result.append(item)
        if len(result) >= int(source.get("htmlLimit", PER_SOURCE_LIMIT)):
            break
    return result


def parse_source_document(
    raw: bytes,
    source: dict,
    collected_at: datetime | None = None,
    detail_fetcher=None,
) -> list[dict]:
    source_type = source.get("type")
    if source_type == "dou":
        return parse_dou_html(raw, source, collected_at)
    if source_type == "html" and source.get("htmlFollowDetail"):
        return parse_html_detail_links(raw, source, collected_at, detail_fetcher)
    if source_type == "html":
        return parse_html_links(raw, source, collected_at)
    return parse_feed(raw, source, collected_at)


def recognizes_source_document(raw: bytes, source: dict) -> bool:
    """Confirma que o documento corresponde à estrutura que o adaptador conhece."""
    source_type = source.get("type")
    if source_type == "dou":
        return _dou_payload(raw, source) is not None
    if source_type != "html":
        try:
            return bool(iter_entries(ET.fromstring(raw)))
        except ET.ParseError:
            return False
    container_tag = source.get("htmlContainerTag")
    if not container_tag:
        return False
    text = raw.decode(source.get("encoding", "utf-8"), "replace")
    tag = re.escape(container_tag)
    contexts = re.findall(rf"<{tag}\b[^>]*>[\s\S]*?</{tag}>", text, flags=re.I)
    if source.get("htmlFollowDetail"):
        allow = source.get("urlAllowRegex")
        return bool(
            source.get("htmlDetailTitleRegex")
            and source.get("htmlPublishedDateRegex")
            and allow
            and any(
                re.search(allow, canonical_url(urljoin(source["url"], match.group(1))), flags=re.I)
                for context in contexts
                if (match := re.search(r"<a\b[^>]*href=[\"']([^\"']+)[\"']", context, flags=re.I))
            )
        )
    date_pattern = source.get("htmlPublishedDateRegex")
    for context in contexts:
        has_link = re.search(r"<a\b[^>]*href=[\"'][^\"']+[\"']", context, flags=re.I)
        has_time = re.search(r"<time\b[^>]*(?:datetime=[\"'][^\"']+[\"'])?[^>]*>[\s\S]*?</time>", context, flags=re.I)
        has_configured_date = date_pattern and re.search(date_pattern, context, flags=re.I)
        if has_link and (has_time or has_configured_date):
            return True
    return False


def load_old() -> dict:
    if not OUTPUT.exists():
        return {"items": []}
    try:
        return json.loads(OUTPUT.read_text(encoding="utf-8"))
    except Exception:
        return {"items": []}


def keep_existing(item: dict) -> bool:
    """Reutiliza apenas itens cuja data tem origem comprovável e reanalisa o risco."""
    required = (
        item.get("title"),
        item.get("url"),
        item.get("collectedAt"),
        item.get("publicationDateEvidence"),
    )
    evidence = item.get("publicationDateEvidence")
    if not all(required) or evidence not in ALLOWED_DATE_EVIDENCE:
        return False
    pseudo_source = {
        "baseScore": 0,
        "includeKeywords": [],
        "excludeKeywords": [],
        "minScore": 5,
    }
    score = relevance_score(
        item.get("title", ""), item.get("summary", ""), pseudo_source
    )
    published_at = parse_date(item.get("publishedAt", ""))
    collected_at = parse_date(item.get("collectedAt", ""))
    if (
        score < 5
        or published_at is None
        or collected_at is None
        or published_at > collected_at
    ):
        return False
    item["score"] = score
    item["publishedAt"] = published_at
    item["collectedAt"] = collected_at
    item.pop("deadline", None)
    item.update(
        strategic_analysis(
            item.get("title", ""),
            item.get("summary", ""),
            item.get("category", "receita"),
            score,
        )
    )
    return True


def source_health(statuses: list[dict]) -> dict:
    failed = [
        status["source"]
        for status in statuses
        if status.get("critical") and not status.get("ok")
    ]
    unrecognized = [
        status["source"]
        for status in statuses
        if status.get("critical")
        and status.get("ok")
        and not status.get("parserRecognized", False)
    ]
    empty = [
        status["source"]
        for status in statuses
        if status.get("critical")
        and status.get("ok")
        and status.get("parserRecognized", False)
        and status.get("items", 0) == 0
    ]
    return {
        "status": "degraded" if failed or unrecognized or empty else "healthy",
        "criticalSourcesFailed": failed,
        "criticalSourcesUnrecognized": unrecognized,
        "criticalSourcesEmpty": empty,
    }


def health_gate_exit_code(health: dict) -> int:
    return 0 if health.get("status") == "healthy" else 1


def persist_payload(payload: dict, health: dict, output: Path = OUTPUT) -> bool:
    if health.get("status") != "healthy":
        return False
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output)
    return True


def resolve_source(source: dict, now: datetime | None = None) -> dict:
    resolved = dict(source)
    year = (now or datetime.now(timezone.utc)).year
    resolved["url"] = resolved["url"].replace("{year}", str(year))
    return resolved


def main():
    sources = json.loads(CONFIG.read_text(encoding="utf-8"))
    old = load_old()
    by_id = {
        item.get("id") or hashlib.sha256(canonical_url(item.get("url", "")).encode()).hexdigest()[:16]: item
        for item in old.get("items", [])
        if keep_existing(item)
    }
    ok = 0
    failed = []
    statuses = []

    for configured_source in sources:
        source = resolve_source(configured_source)
        source_status = {
            "source": source["name"],
            "url": source["url"],
            "critical": bool(source.get("critical")),
            "ok": False,
            "parserRecognized": False,
            "items": 0,
        }
        try:
            req = Request(
                source["url"],
                headers={
                    "User-Agent": "RadarFiscal/2.0 (RSS fiscal; contato: painel local)",
                    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
                },
            )
            with urlopen(req, timeout=int(source.get("timeout", TIMEOUT)), context=SSL_CONTEXT) as response:
                max_bytes = int(source.get("maxBytes", 900_000 if source.get("type") in {"html", "dou"} else 0))
                raw = response.read(max_bytes) if max_bytes > 0 else response.read()
            parser_recognized = recognizes_source_document(raw, source)
            found = parse_source_document(raw, source)
            for item in found:
                by_id[item["id"]] = item
            ok += 1
            source_status.update({
                "ok": True,
                "parserRecognized": parser_recognized,
                "items": len(found),
            })
        except Exception as exc:
            error = str(exc)[:180]
            failed.append({"source": source["name"], "url": source["url"], "error": error})
            source_status["error"] = error
        statuses.append(source_status)

    for item in by_id.values():
        if not item.get("impactTags"):
            item.update(strategic_analysis(item.get("title", ""), item.get("summary", ""), item.get("category", "receita"), int(item.get("score", 0))))
    items = sorted(by_id.values(), key=lambda x: x.get("publishedAt", ""), reverse=True)[:MAX_ITEMS]
    health = source_health(statuses)
    payload = {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "items": items,
        "sourcesOk": ok,
        "sourcesFailed": failed,
        "sources": statuses,
        "health": health,
        "meta": {
            "collector": "collector/collect.py",
            "policy": "RSS/Atom e páginas HTML públicas com filtro fiscal/tributário, deduplicação por URL canônica e fallback do histórico local.",
            "maxItems": MAX_ITEMS,
        },
    }
    written = persist_payload(payload, health, OUTPUT)
    print(f"Coleta concluída: {len(items)} itens; {ok} fontes OK; {len(failed)} falhas.")
    if failed:
        for item in failed:
            print(f"- FALHA {item['source']}: {item['error']}")
    for status in statuses:
        if status["ok"]:
            print(f"- OK {status['source']}: {status['items']} itens relevantes")
    if health["status"] == "degraded":
        print(f"- SAÚDE DEGRADADA: {health}")
    return health_gate_exit_code(health)


if __name__ == "__main__":
    raise SystemExit(main())
