from datetime import datetime, timezone

import pytest

from collector import collect


SOURCE = {
    "name": "Fonte oficial de teste",
    "url": "https://example.gov.br/noticias",
    "category": "receita",
    "baseScore": 10,
    "minScore": 0,
}


def rss(*items: str) -> bytes:
    return ("<rss><channel>" + "".join(items) + "</channel></rss>").encode()


def entry(title: str, link: str, published: str | None) -> str:
    date = f"<pubDate>{published}</pubDate>" if published is not None else ""
    return f"<item><title>{title}</title><link>{link}</link>{date}</item>"


def test_feed_rejects_unverifiable_publication_dates_and_separates_collection_time():
    collected_at = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
    raw = rss(
        entry("Prazo fiscal sem data", "https://example.gov.br/sem-data", None),
        entry("Prazo fiscal com data inválida", "https://example.gov.br/data-invalida", "ontem"),
        entry("Prazo fiscal verificável", "https://example.gov.br/valida", "Fri, 20 Nov 2026 10:00:00 GMT"),
    )

    items = collect.parse_feed(raw, SOURCE, collected_at=collected_at)

    assert [item["title"] for item in items] == ["Prazo fiscal verificável"]
    assert items[0]["publishedAt"] == "2026-11-20T10:00:00+00:00"
    assert items[0]["collectedAt"] == "2026-11-21T12:00:00+00:00"


@pytest.mark.parametrize(
    ("href", "title"),
    [
        ("javascript:void(0)", "Prazo tributário"),
        ("#conteudo", "Prazo tributário"),
        ("/institucional/quem-somos", "Institucional"),
        ("/contato", "Contato"),
        ("/noticias", "Notícias"),
    ],
)
def test_html_link_validation_rejects_non_content_links(href, title):
    assert not collect.is_valid_content_link(href, title, SOURCE)


def test_html_link_validation_accepts_direct_http_content_link():
    assert collect.is_valid_content_link(
        "/noticias/2026/ato-tributario-123",
        "Ato tributário 123",
        SOURCE,
    )


def test_html_parser_does_not_fabricate_publication_date():
    raw = b'<a href="/noticias/ato-tributario-123">Ato tributario 123</a>'

    assert collect.parse_html_links(raw, SOURCE) == []


def test_historical_item_without_verifiable_publication_date_is_rejected():
    base = {
        "title": "Prazo tributário",
        "url": "https://example.gov.br/noticias/prazo-tributario",
        "summary": "Prazo fiscal",
    }

    assert not collect.keep_existing(dict(base))
    assert not collect.keep_existing({**base, "publishedAt": "data desconhecida"})


def test_tls_context_requires_certificate_validation():
    import ssl

    assert collect.SSL_CONTEXT.verify_mode == ssl.CERT_REQUIRED
    assert collect.SSL_CONTEXT.check_hostname is True


@pytest.mark.parametrize(
    ("deadline", "has_urgency"),
    [
        (None, False),
        ("2026-11-20T12:00:00+00:00", False),
        ("2026-11-22T12:00:00+00:00", True),
    ],
)
def test_urgency_requires_a_real_future_deadline(deadline, has_urgency):
    analysis = collect.strategic_analysis(
        "Prazo fiscal vence amanhã",
        "Contribuinte deve cumprir o prazo.",
        "receita",
        10,
        deadline=deadline,
        now=datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc),
    )

    assert ("prazo" in analysis["impactTags"]) is has_urgency
    if has_urgency:
        assert analysis["priority"] == "alta"
        assert analysis["deadline"] == deadline
    else:
        assert "Prazo urgente" not in analysis["impactLabels"]
        assert "deadline" not in analysis


def test_critical_source_failure_or_empty_result_is_explicitly_degraded():
    health = collect.source_health(
        [
            {"source": "Receita", "critical": True, "ok": False, "items": 0, "error": "timeout"},
            {"source": "CONFAZ", "critical": True, "ok": True, "items": 0},
            {"source": "Fonte auxiliar", "critical": False, "ok": True, "items": 3},
        ]
    )

    assert health == {
        "status": "degraded",
        "criticalSourcesFailed": ["Receita"],
        "criticalSourcesEmpty": ["CONFAZ"],
    }
    assert collect.health_gate_exit_code(health) == 1
    assert collect.health_gate_exit_code(
        {"status": "healthy", "criticalSourcesFailed": [], "criticalSourcesEmpty": []}
    ) == 0


def test_required_mvp_sources_are_marked_critical():
    import json

    sources = json.loads(collect.CONFIG.read_text(encoding="utf-8"))
    critical = {source["name"] for source in sources if source.get("critical")}

    assert {
        "Receita Federal",
        "PGFN Notícias",
        "CONFAZ — Convênios ICMS",
        "CONFAZ — Ajustes SINIEF",
        "CONFAZ — Protocolos ICMS",
        "Diário Oficial da União — busca fiscal",
        "Diário Oficial de Minas Gerais",
        "SEF Minas Gerais — Empresas",
    }.issubset(critical)
