import json
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
        entry("Prazo fiscal com data futura", "https://example.gov.br/data-futura", "Sun, 22 Nov 2026 10:00:00 GMT"),
        entry("Prazo fiscal verificável", "https://example.gov.br/valida", "Fri, 20 Nov 2026 10:00:00 GMT"),
    )

    items = collect.parse_feed(raw, SOURCE, collected_at=collected_at)

    assert [item["title"] for item in items] == ["Prazo fiscal verificável"]
    assert items[0]["publishedAt"] == "2026-11-20T10:00:00+00:00"
    assert items[0]["collectedAt"] == "2026-11-21T12:00:00+00:00"
    assert items[0]["publicationDateEvidence"] == "feed:pubdate"


def test_feed_rejects_updated_as_publication_date():
    raw = b"""<feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>Prazo fiscal apenas atualizado</title>
        <link href="https://example.gov.br/noticias/apenas-atualizado" />
        <updated>2026-11-20T10:00:00Z</updated>
      </entry>
    </feed>"""

    assert collect.parse_feed(
        raw,
        SOURCE,
        collected_at=datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc),
    ) == []


@pytest.mark.parametrize(
    ("href", "title"),
    [
        ("javascript:void(0)", "Prazo tributário"),
        ("#conteudo", "Prazo tributário"),
        ("/institucional/quem-somos", "Institucional"),
        ("/contato", "Contato"),
        ("/noticias", "Notícias"),
        ("/ouvidoria", "Ouvidoria"),
        ("/acesso-a-informacao", "Acesso à informação"),
    ],
)
def test_html_link_validation_rejects_non_content_links(href, title):
    assert not collect.is_valid_content_link(href, title, SOURCE)


def test_html_link_validation_rejects_external_domain():
    assert not collect.is_valid_content_link(
        "https://tracker.example/noticias/ato-tributario",
        "Ato tributário",
        SOURCE,
    )


def test_html_link_validation_accepts_direct_http_content_link():
    assert collect.is_valid_content_link(
        "/noticias/2026/ato-tributario-123",
        "Ato tributário 123",
        SOURCE,
    )


def test_html_parser_does_not_fabricate_publication_date():
    raw = b'<a href="/noticias/ato-tributario-123">Ato tributario 123</a>'

    assert collect.parse_html_links(raw, SOURCE) == []


def test_html_parser_accepts_date_from_same_structured_result():
    raw = b"""
    <article class="resultado">
      <a href="/noticias/ato-tributario-123">Ato tributario 123</a>
      <time datetime="2026-11-20T10:00:00-03:00">20/11/2026</time>
    </article>
    """

    items = collect.parse_html_links(
        raw,
        {**SOURCE, "htmlContainerTag": "article"},
        collected_at=datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc),
    )

    assert len(items) == 1
    assert items[0]["publishedAt"] == "2026-11-20T13:00:00+00:00"
    assert items[0]["publicationDateEvidence"] == "html:time[datetime]"
    assert items[0]["collectedAt"] == "2026-11-21T12:00:00+00:00"


def test_historical_item_without_verifiable_publication_date_is_rejected():
    base = {
        "title": "Prazo tributário",
        "url": "https://example.gov.br/noticias/prazo-tributario",
        "summary": "Prazo fiscal",
        "publishedAt": "2026-11-20T12:00:00+00:00",
        "collectedAt": "2026-11-20T13:00:00+00:00",
    }

    assert not collect.keep_existing({key: base[key] for key in ("title", "url", "summary")})
    assert not collect.keep_existing({**base, "publishedAt": "data desconhecida"})
    assert not collect.keep_existing(dict(base))
    assert not collect.keep_existing({**base, "publicationDateEvidence": "unknown"})
    assert not collect.keep_existing({
        **base,
        "publishedAt": "2026-11-20T14:00:00+00:00",
        "publicationDateEvidence": "feed:pubdate",
    })


def test_html_recognition_requires_link_and_date_in_a_configured_container():
    source = {**SOURCE, "type": "html", "htmlContainerTag": "article"}
    recognized = b'<article><a href="/noticia">Noticia tributaria</a><time datetime="2026-11-20">20/11/2026</time></article>'
    missing_date = b'<article><a href="/noticia">Noticia tributaria</a></article>'
    generic_article = b'<article><p>Pagina institucional</p></article>'

    assert collect.recognizes_source_document(recognized, source)
    assert not collect.recognizes_source_document(missing_date, source)
    assert not collect.recognizes_source_document(generic_article, source)


def test_verified_historical_item_is_sanitized_before_reuse():
    item = {
        "title": "Prazo tributário",
        "url": "https://example.gov.br/noticias/prazo-tributario",
        "summary": "Prazo fiscal sem vencimento verificável",
        "publishedAt": "2026-11-20T12:00:00+00:00",
        "collectedAt": "2026-11-20T13:00:00+00:00",
        "publicationDateEvidence": "feed:pubdate",
        "impactTags": ["prazo", "oportunidade"],
        "impactLabels": ["Prazo urgente", "Oportunidade consultiva"],
        "priority": "alta",
    }

    assert collect.keep_existing(item)
    assert "prazo" not in item["impactTags"]
    assert "Prazo urgente" not in item["impactLabels"]
    assert "deadline" not in item


def test_parse_date_preserves_iso_offset_as_the_correct_utc_instant():
    assert collect.parse_date("2026-11-21T12:00:00-03:00") == "2026-11-21T15:00:00+00:00"


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


def test_feed_does_not_create_urgency_from_words_without_deadline():
    raw = rss(
        entry(
            "Prazo fiscal vence amanhã",
            "https://example.gov.br/noticias/prazo",
            "Fri, 20 Nov 2026 10:00:00 GMT",
        )
    )

    item = collect.parse_feed(raw, SOURCE)[0]

    assert "prazo" not in item["impactTags"]
    assert "Prazo urgente" not in item["impactLabels"]
    assert "deadline" not in item


def test_critical_source_failure_or_empty_result_is_explicitly_degraded():
    health = collect.source_health(
        [
            {"source": "Receita", "critical": True, "ok": False, "items": 0, "error": "timeout"},
            {"source": "CONFAZ", "critical": True, "ok": True, "parserRecognized": True, "items": 0},
            {"source": "Fonte auxiliar", "critical": False, "ok": True, "parserRecognized": True, "items": 3},
        ]
    )

    assert health == {
        "status": "degraded",
        "criticalSourcesFailed": ["Receita"],
        "criticalSourcesUnrecognized": [],
        "criticalSourcesEmpty": ["CONFAZ"],
    }
    assert collect.health_gate_exit_code(health) == 1
    assert collect.health_gate_exit_code(
        {
            "status": "healthy",
            "criticalSourcesFailed": [],
            "criticalSourcesUnrecognized": [],
            "criticalSourcesEmpty": [],
        }
    ) == 0


@pytest.mark.parametrize("health", [
    {},
    {"status": "degraded"},
    {"status": "unknown"},
    {"criticalSourcesFailed": [], "criticalSourcesUnrecognized": [], "criticalSourcesEmpty": []},
])
def test_health_gate_fails_closed_unless_status_is_healthy(health):
    assert collect.health_gate_exit_code(health) == 1


def test_empty_but_recognized_critical_source_is_visible_and_blocks_the_gate():
    health = collect.source_health(
        [{"source": "CONFAZ", "critical": True, "ok": True, "parserRecognized": True, "items": 0}]
    )

    assert health["status"] == "degraded"
    assert health["criticalSourcesEmpty"] == ["CONFAZ"]
    assert collect.health_gate_exit_code(health) == 1


def test_dou_embedded_json_produces_items_with_official_date_evidence():
    payload = {
        "jsonArray": [{
            "title": "PORTARIA MF Nº 100, DE 20 DE NOVEMBRO DE 2026",
            "urlTitle": "portaria-mf-n-100-de-20-de-novembro-de-2026-123",
            "pubDate": "20/11/2026",
            "pubName": "DO1",
            "editionNumber": "219",
            "numberPage": "10",
            "content": "Dispõe sobre obrigação tributária e fiscalização.",
        }]
    }
    raw = (
        '<div id="_br_com_seatecnologia_in_buscadou_BuscaDouPortlet_params">'
        + json.dumps(payload)
        + '</div>'
    ).encode()
    source = {
        "name": "DOU",
        "url": "https://www.in.gov.br/consulta/-/buscar/dou?q=fiscal",
        "category": "legislacao",
        "baseScore": 5,
        "minScore": 5,
        "type": "dou",
    }

    items = collect.parse_dou_html(raw, source, datetime(2026, 11, 21, tzinfo=timezone.utc))

    assert collect.recognizes_source_document(raw, source)
    assert collect.parse_source_document(
        raw, source, datetime(2026, 11, 21, tzinfo=timezone.utc)
    ) == items
    assert len(items) == 1
    assert items[0]["publicationDateEvidence"] == "dou:pubDate"
    assert items[0]["publishedAt"] == "2026-11-20T00:00:00+00:00"
    assert items[0]["url"].startswith("https://www.in.gov.br/web/dou/-/")


def test_configured_html_title_regex_excludes_date_from_title():
    raw = b'''<html><a href="/noticias/2026/item/index.html">
      <div class="title-small">SEF publica orientacao sobre ICMS</div>
      <em>Fri Nov 20 10:00:00 BRT 2026</em>
    </a></html>'''
    source = {
        "name": "SEF/MG Notícias",
        "url": "https://www.fazenda.mg.gov.br/noticias/",
        "type": "html",
        "htmlContainerTag": "a",
        "htmlTitleRegex": r'class=["\'][^"\']*title-small[^"\']*["\'][^>]*>([\s\S]*?)</div>',
        "htmlPublishedDateRegex": r"<em>\s*([^<]+BRT\s+\d{4})\s*</em>",
        "urlAllowRegex": r"fazenda\.mg\.gov\.br/noticias/\d{4}/",
        "baseScore": 5,
        "minScore": 5,
    }

    items = collect.parse_html_links(raw, source, datetime(2026, 11, 21, tzinfo=timezone.utc))

    assert len(items) == 1
    assert items[0]["title"] == "SEF publica orientacao sobre ICMS"
    assert items[0]["publishedAt"] == "2026-11-20T13:00:00+00:00"


def test_confaz_detail_adapter_uses_detail_title_and_dou_publication_date():
    index = b'''<table><tr><td><a href="/legislacao/convenios/2026/CV131_26" title="Convenio ICMS 131/26">131</a></td></tr></table>'''
    detail = b'''<p class="TituloAcordo">CONVENIO ICMS N 131, DE 27 DE OUTUBRO DE 2026</p>
      <p class="DataPublicao">Publicado no DOU de 29.10.26, pelo Despacho 52/26.</p>'''
    source = {
        "name": "CONFAZ — Convênios ICMS",
        "url": "https://www.confaz.fazenda.gov.br/legislacao/convenios/2026/2026",
        "type": "html",
        "htmlContainerTag": "tr",
        "htmlFollowDetail": True,
        "htmlDetailTitleRegex": r'<p\b[^>]*class=["\'][^"\']*TituloAcordo[^"\']*["\'][^>]*>([\s\S]*?)</p>',
        "htmlPublishedDateRegex": r"Publicado\s+no\s+DOU\s+de\s+([0-3]?\d\.[01]?\d\.\d{2})",
        "urlAllowRegex": r"confaz\.fazenda\.gov\.br/legislacao/convenios/\d{4}/CV\d{3}_\d{2}$",
        "baseScore": 5,
        "minScore": 5,
    }

    items = collect.parse_html_detail_links(
        index,
        source,
        datetime(2026, 11, 21, tzinfo=timezone.utc),
        detail_fetcher=lambda url: detail,
    )

    assert len(items) == 1
    assert items[0]["title"].startswith("CONVENIO ICMS N 131")
    assert items[0]["publishedAt"] == "2026-10-29T00:00:00+00:00"
    assert items[0]["publicationDateEvidence"] == "html:detail:configured-regex"


def test_confaz_rejects_cross_year_links_and_semantically_inconsistent_dates():
    source = {
        "name": "CONFAZ — Convênios ICMS",
        "type": "html",
        "url": "https://www.confaz.fazenda.gov.br/legislacao/convenios/2026/2026",
        "category": "icms",
        "baseScore": 5,
        "minScore": 5,
        "includeKeywords": ["convênio icms", "ICMS"],
        "excludeKeywords": [],
        "htmlContainerTag": "tr",
        "htmlFollowDetail": True,
        "htmlDetailTitleRegex": r'<p class="TituloAcordo">([\s\S]*?)</p>',
        "htmlPublishedDateRegex": r"Publicado no DOU de ([0-3]?\d\.[01]?\d\.\d{2})",
        "urlAllowRegex": r"^https://www\.confaz\.fazenda\.gov\.br/legislacao/convenios/\d{4}/CV\d{3}_\d{2}$",
    }
    index = b"""
    <table>
      <tr><td><a href="/legislacao/convenios/2020/CV044_20" title="CONVENIO ICMS 44/20">ato antigo</a></td></tr>
      <tr><td><a href="/legislacao/convenios/2026/CV139_26" title="CONVENIO ICMS 139/26">ato atual</a></td></tr>
    </table>
    """
    fetched = []

    def fake_fetch(url):
        fetched.append(url)
        return b"""
        <p class="TituloAcordo">CONVENIO ICMS 139, DE 17 DE NOVEMBRO DE 2026</p>
        <p>Publicado no DOU de 18.11.20</p>
        """

    items = collect.parse_html_detail_links(
        index,
        source,
        datetime(2026, 11, 21, tzinfo=timezone.utc),
        detail_fetcher=fake_fetch,
    )

    assert items == []
    assert fetched == ["https://www.confaz.fazenda.gov.br/legislacao/convenios/2026/CV139_26"]


@pytest.mark.parametrize(("title_year", "publication", "accepted"), [
    (2025, "02.01.26", False),
    (2026, "02.01.27", True),
    (2026, "29.12.26", False),
    (2026, "02.01.28", False),
])
def test_confaz_requires_listing_year_and_valid_publication_rollover(title_year, publication, accepted):
    source = {
        **SOURCE,
        "name": "CONFAZ — Convênios ICMS",
        "url": "https://www.confaz.fazenda.gov.br/legislacao/convenios/2026/2026",
        "type": "html",
        "htmlContainerTag": "tr",
        "htmlFollowDetail": True,
        "htmlDetailTitleRegex": r'<p class="TituloAcordo">([\s\S]*?)</p>',
        "htmlPublishedDateRegex": r"Publicado no DOU de ([0-3]?\d\.[01]?\d\.\d{2})",
        "urlAllowRegex": r"confaz\.fazenda\.gov\.br/legislacao/convenios/\d{4}/CV\d{3}_\d{2}$",
    }
    index = b'<table><tr><td><a href="/legislacao/convenios/2026/CV200_26">ato</a></td></tr></table>'
    detail = f'''<p class="TituloAcordo">CONVENIO ICMS 200, DE 30 DE DEZEMBRO DE {title_year}</p>
      <p>Publicado no DOU de {publication}</p>'''.encode()

    items = collect.parse_html_detail_links(
        index,
        source,
        datetime(2028, 2, 1, tzinfo=timezone.utc),
        detail_fetcher=lambda url: detail,
    )

    assert bool(items) is accepted


def test_degraded_payload_is_not_written(tmp_path):
    output = tmp_path / "news.json"
    payload = {"health": {"status": "degraded"}, "items": [{"id": "unsafe"}]}

    written = collect.persist_payload(payload, payload["health"], output)

    assert written is False
    assert not output.exists()


def test_healthy_payload_is_written_atomically(tmp_path):
    output = tmp_path / "news.json"
    payload = {"health": {"status": "healthy"}, "items": []}

    written = collect.persist_payload(payload, payload["health"], output)

    assert written is True
    assert json.loads(output.read_text(encoding="utf-8")) == payload
    assert not output.with_suffix(".json.tmp").exists()


def test_unrecognized_critical_parser_blocks_publication_gate():
    health = collect.source_health(
        [{"source": "CONFAZ", "critical": True, "ok": True, "parserRecognized": False, "items": 0}]
    )

    assert health["status"] == "degraded"
    assert health["criticalSourcesUnrecognized"] == ["CONFAZ"]
    assert collect.health_gate_exit_code(health) == 1


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


def test_critical_sources_define_their_verified_adapters():
    sources = {
        source["name"]: source
        for source in json.loads(collect.CONFIG.read_text(encoding="utf-8"))
    }

    pgfn = sources["PGFN Notícias"]
    assert pgfn["htmlContainerTag"] == "li"
    assert pgfn["htmlPublishedDateRegex"]
    assert pgfn["urlAllowRegex"].startswith("^https://")

    for name in (
        "CONFAZ — Convênios ICMS",
        "CONFAZ — Ajustes SINIEF",
        "CONFAZ — Protocolos ICMS",
    ):
        source = sources[name]
        assert source["htmlFollowDetail"] is True
        assert source["htmlContainerTag"] == "tr"
        assert source["htmlDetailTitleRegex"]
        assert "{year}" in source["url"]

    assert sources["Diário Oficial da União — busca fiscal"]["type"] == "dou"

    sef = sources["SEF Minas Gerais — Empresas"]
    assert sef["url"] == "https://www.fazenda.mg.gov.br/noticias/"
    assert sef["htmlContainerTag"] == "a"
    assert sef["htmlTitleRegex"]
    assert sef["htmlPublishedDateRegex"]


def test_source_url_year_placeholder_is_resolved_without_mutating_config():
    source = {"name": "CONFAZ", "url": "https://example.gov.br/{year}/{year}"}

    resolved = collect.resolve_source(source, datetime(2027, 1, 2, tzinfo=timezone.utc))

    assert resolved["url"] == "https://example.gov.br/2027/2027"
    assert source["url"] == "https://example.gov.br/{year}/{year}"


def test_frontend_only_accepts_validated_feed_and_rechecks_urgency_and_health():
    index = (collect.ROOT / "index.html").read_text(encoding="utf-8")

    assert 'if(deadlineFuturo(i.prazo)) tags.push("prazo");' in index
    assert 'if(i.prazo || inclui(blob,["vence","prazo"' not in index
    assert '/(vence|prazo|urgente|' not in index
    assert "const LEGACY_SNAPSHOT_QUARANTINED = [" in index
    assert "const SNAPSHOT = [];" in index
    assert "const LEGACY_PRAZOS_QUARANTINED = [" in index
    assert "const PRAZOS = [];" in index
    assert "collectorDegraded: true" in index
    assert "const payloadHealthy = payload.health?.status === 'healthy';" in index
    assert "const externos = payloadHealthy && feedComDataValida && Array.isArray(payload.items) ? payload.items : [];" in index
    assert "const feedHistorico = payloadHealthy && feedComDataValida && feedObsoleto;" in index
    assert "Histórico possivelmente desatualizado" in index
    assert "'feed:updated'" not in index
    assert "!item.publicationDateEvidence || !item.collectedAt" in index
    assert "'html:detail:configured-regex'" in index
    assert "'dou:pubDate'" in index
    assert "const staleUrgency = rawImpacts.includes('prazo') && !deadline;" in index
    assert "state.online = respondeu && !state.collectorDegraded;" in index
