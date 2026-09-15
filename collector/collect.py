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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

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
}

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
    wanted = set(names)
    for child in list(node):
        local = child.tag.rsplit("}", 1)[-1].lower()
        if local in wanted and child.text:
            return clean(child.text)
    return ""


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


def parse_date(value: str) -> str:
    raw = clean(value)
    if not raw:
        return datetime.now(timezone.utc).isoformat()
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%d/%m/%Y %H:%M"):
        try:
            dt = datetime.strptime(raw[: len(datetime.now().strftime(fmt))], fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except Exception:
            continue
    return datetime.now(timezone.utc).isoformat()


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



def strategic_analysis(title: str, summary: str, category: str, score: int) -> dict:
    blob = norm(f"{title} {summary} {category}")
    tags: list[str] = []
    for key, terms in IMPACT_RULES.items():
        if any(term_matches(blob, term) if term.strip() == term else term in blob for term in terms):
            tags.append(key)
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

    return {
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

def iter_entries(root: ET.Element) -> list[ET.Element]:
    entries = []
    for node in root.iter():
        local = node.tag.rsplit("}", 1)[-1].lower()
        if local in {"item", "entry"}:
            entries.append(node)
    return entries


def parse_feed(raw: bytes, source: dict) -> list[dict]:
    root = ET.fromstring(raw)
    result = []
    min_score = int(source.get("minScore", 5))
    for node in iter_entries(root)[:PER_SOURCE_LIMIT]:
        title = child_text(node, ("title",))
        link = child_text(node, ("link",)) or child_attr(node, "link", "href", "alternate") or child_attr(node, "link", "href")
        summary = child_text(node, ("description", "summary", "content", "encoded"))
        published = child_text(node, ("pubdate", "published", "updated", "date", "dc:date"))
        if not title or not link:
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
            "publishedAt": parse_date(published),
            "score": score,
        }
        item.update(strategic_analysis(title, item_summary, category, score))
        result.append(item)
    return result


def load_old() -> dict:
    if not OUTPUT.exists():
        return {"items": []}
    try:
        return json.loads(OUTPUT.read_text(encoding="utf-8"))
    except Exception:
        return {"items": []}


def keep_existing(item: dict) -> bool:
    """Evita carregar ruído histórico de coletas antigas com feeds genéricos."""
    if not item.get("title") or not item.get("url"):
        return False
    pseudo_source = {"baseScore": 0, "includeKeywords": [], "excludeKeywords": [], "minScore": 5}
    score = relevance_score(item.get("title", ""), item.get("summary", ""), pseudo_source)
    if score >= 5:
        item["score"] = score
        if item.get("publishedAt"):
            item["publishedAt"] = parse_date(item["publishedAt"])
        return True
    return False


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

    for source in sources:
        source_status = {"source": source["name"], "url": source["url"], "ok": False, "items": 0}
        try:
            req = Request(
                source["url"],
                headers={
                    "User-Agent": "RadarFiscal/2.0 (RSS fiscal; contato: painel local)",
                    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
                },
            )
            with urlopen(req, timeout=int(source.get("timeout", TIMEOUT))) as response:
                raw = response.read()
            found = parse_feed(raw, source)
            for item in found:
                by_id[item["id"]] = item
            ok += 1
            source_status.update({"ok": True, "items": len(found)})
        except Exception as exc:
            error = str(exc)[:180]
            failed.append({"source": source["name"], "url": source["url"], "error": error})
            source_status["error"] = error
        statuses.append(source_status)

    for item in by_id.values():
        if not item.get("impactTags"):
            item.update(strategic_analysis(item.get("title", ""), item.get("summary", ""), item.get("category", "receita"), int(item.get("score", 0))))
    items = sorted(by_id.values(), key=lambda x: x.get("publishedAt", ""), reverse=True)[:MAX_ITEMS]
    payload = {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "items": items,
        "sourcesOk": ok,
        "sourcesFailed": failed,
        "sources": statuses,
        "meta": {
            "collector": "collector/collect.py",
            "policy": "RSS/Atom com filtro fiscal/tributário, deduplicação por URL canônica e fallback do histórico local.",
            "maxItems": MAX_ITEMS,
        },
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Coleta concluída: {len(items)} itens; {ok} fontes OK; {len(failed)} falhas.")
    if failed:
        for item in failed:
            print(f"- FALHA {item['source']}: {item['error']}")
    for status in statuses:
        if status["ok"]:
            print(f"- OK {status['source']}: {status['items']} itens relevantes")


if __name__ == "__main__":
    main()
