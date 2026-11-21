import json
import re
import subprocess
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


def test_jornal_mg_tls_context_adds_pinned_intermediate_without_weakening_tls():
    import ssl

    sources = json.loads(collect.CONFIG.read_text(encoding="utf-8"))
    source = next(item for item in sources if item["name"] == "Diário Oficial de Minas Gerais")

    context = collect.ssl_context_for_source(source)

    assert source["type"] == "jornal_mg"
    assert source["tlsIntermediateCert"] == "collector/certs/sectigo-public-server-authentication-ca-dv-r36.pem"
    assert source["tlsIntermediateSha256"] == "8c54c334b66ba4e426772af4a3f9136c19a1aec729fdb28c535c07a5a4ef22e0"
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True

    with pytest.raises(ValueError, match="SHA-256"):
        collect.ssl_context_for_source({**source, "tlsIntermediateSha256": "00" * 32})


def test_jornal_mg_parser_uses_official_date_page_and_canonical_portal_link():
    from urllib.parse import parse_qs, urlparse

    source = {
        **SOURCE,
        "name": "Diário Oficial de Minas Gerais",
        "url": "https://www.jornalminasgerais.mg.gov.br/",
        "type": "jornal_mg",
        "minScore": 0,
    }
    raw = json.dumps({
        "dados": [{
            "idJornal": 331353,
            "dataPublicacao": "2026-11-20T00:00:00",
            "tipoCaderno": "Diário do Executivo",
            "textoResultado": "A Secretaria de Fazenda fixa índices definitivos da parcela do ICMS.",
            "pagina": 7,
        }],
        "paginaAtual": 1,
        "totalDePaginas": 1,
        "totalDeRegistros": 1,
        "erros": [],
    }).encode()

    items = collect.parse_jornal_mg_results(
        raw,
        source,
        "ICMS",
        collected_at=datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc),
    )

    assert len(items) == 1
    item = items[0]
    assert item["publishedAt"] == "2026-11-20T00:00:00+00:00"
    assert item["publicationDateEvidence"] == "jornal-mg-api:dataPublicacao"
    assert item["sourceRecord"] == {"idJornal": 331353, "pagina": 7}
    assert "ICMS" in item["title"]
    parsed = urlparse(item["url"])
    assert parsed.path == "/edicao-do-dia"
    navigation = json.loads(parse_qs(parsed.query)["dados"][0])
    assert navigation == {
        "dataPublicacaoSelecionada": "2026-11-20T00:00:00",
        "idCadernoEdicaoSelecionado": 331353,
        "paginaSelecionada": 7,
        "textoPesquisa": "ICMS",
    }


def test_jornal_mg_collection_uses_anonymous_auth_then_bearer_search(monkeypatch):
    source = {
        **SOURCE,
        "name": "Diário Oficial de Minas Gerais",
        "url": "https://www.jornalminasgerais.mg.gov.br/",
        "type": "jornal_mg",
        "minScore": 0,
        "searchTerms": ["ICMS"],
        "lookbackDays": 30,
        "pageSize": 50,
        "maxPagesPerTerm": 1,
        "htmlLimit": 20,
    }
    search_payload = json.dumps({
        "dados": [{
            "idJornal": 331353,
            "dataPublicacao": "2026-11-20T00:00:00",
            "tipoCaderno": "Diário do Executivo",
            "textoResultado": "Publicação sobre ICMS.",
            "pagina": 7,
        }],
        "paginaAtual": 1,
        "totalDePaginas": 1,
        "totalDeRegistros": 1,
        "erros": [],
    }).encode()
    responses = [json.dumps({"dados": "token-anonimo"}).encode(), search_payload]
    calls = []
    strict_context = object()

    class FakeResponse:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, _limit=None):
            return self.body

    def fake_urlopen(request, timeout, context):
        calls.append((request, timeout, context))
        return FakeResponse(responses.pop(0))

    monkeypatch.setattr(collect, "ssl_context_for_source", lambda _source: strict_context)
    monkeypatch.setattr(collect, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        collect,
        "_open_bearer_request",
        lambda request, *, timeout, context: fake_urlopen(request, timeout, context),
    )

    items = collect.collect_jornal_mg(
        source,
        now=datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc),
    )

    assert len(items) == 1
    assert len(calls) == 2
    auth_request, _, auth_context = calls[0]
    search_request, _, search_context = calls[1]
    assert auth_request.get_method() == "POST"
    assert auth_request.data == b"{}"
    assert auth_request.full_url.endswith("/api/v1/Autenticacao/Autenticar")
    assert auth_request.get_header("Authorization") is None
    assert dict(auth_request.header_items())["User-agent"].encode("ascii")
    assert search_request.get_method() == "GET"
    assert "/api/v1/Pesquisa/PesquisarJornaisPaginados?" in search_request.full_url
    assert search_request.get_header("Authorization") == "Bearer token-anonimo"
    assert auth_context is strict_context
    assert search_context is strict_context


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


def test_confaz_detail_adapter_skips_one_failed_detail_and_keeps_valid_items():
    index = b'''<table>
      <tr><td><a href="/legislacao/convenios/2026/CV127_26">127</a></td></tr>
      <tr><td><a href="/legislacao/convenios/2026/CV131_26">131</a></td></tr>
    </table>'''
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

    def fake_fetch(url):
        if url.endswith("CV127_26"):
            raise TimeoutError("detalhe indisponível")
        return b'''<p class="TituloAcordo">CONVENIO ICMS 131, DE 27 DE OUTUBRO DE 2026</p>
          <p>Publicado no DOU de 29.10.26</p>'''

    items = collect.parse_html_detail_links(
        index,
        source,
        datetime(2026, 11, 21, tzinfo=timezone.utc),
        detail_fetcher=fake_fetch,
    )

    assert [item["url"].rsplit("/", 1)[-1] for item in items] == ["CV131_26"]


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


def test_jornal_mg_evidence_is_allowed_for_verified_history():
    item = {
        "title": "Diário Oficial de Minas Gerais — ICMS",
        "url": "https://www.jornalminasgerais.mg.gov.br/edicao-do-dia?dados=teste",
        "summary": "Publicação oficial sobre ICMS",
        "publishedAt": "2026-11-20T00:00:00+00:00",
        "collectedAt": "2026-11-21T12:00:00+00:00",
        "publicationDateEvidence": "jornal-mg-api:dataPublicacao",
    }

    assert collect.keep_existing(item)


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
    assert "state.collectorDegraded = !feedValidated;" in index
    assert "state.feedHistorico = feedHistorico;" in index
    assert "Última coleta saudável preservada" in index
    assert "Histórico possivelmente desatualizado" in index
    assert "'feed:updated'" not in index
    assert "!item.publicationDateEvidence || !item.collectedAt" in index
    assert "'html:detail:configured-regex'" in index
    assert "'dou:pubDate'" in index
    assert "'jornal-mg-api:dataPublicacao'" in index
    assert "const staleUrgency = rawImpacts.includes('prazo') && !deadline;" in index
    assert "state.online = respondeu && !state.collectorDegraded && !state.feedHistorico;" in index


def test_phase_three_preview_has_app_shell_theme_and_mobile_navigation_contract():
    index = (collect.ROOT / "index.html").read_text(encoding="utf-8")

    assert 'class="app-shell"' in index
    assert 'class="product-nav"' in index
    assert 'id="themeToggle"' in index
    assert 'id="navToggle"' in index
    assert 'data-theme="dark"' in index
    assert "radar-fiscal-theme" in index
    assert "prefers-color-scheme: dark" in index
    assert "@media(max-width:900px)" in index


def _run_frontend_validator(items):
    index = (collect.ROOT / "index.html").read_text(encoding="utf-8")
    functions = []
    for name in ("urlExternaSegura", "validarItemFeed"):
        match = re.search(rf"function {name}\([^\n]+\)\{{[\s\S]*?\n\}}", index)
        assert match, f"função {name} ausente"
        functions.append(match.group(0))
    script = "\n".join(functions) + f"\nconsole.log(JSON.stringify({json.dumps(items)}.map(validarItemFeed)));"
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )
    return json.loads(completed.stdout)


def test_frontend_rejects_xss_urls_markup_and_invalid_feed_item_dates():
    valid = {
        "title": "Ato fiscal válido",
        "url": "https://example.gov.br/ato",
        "source": "Fonte oficial",
        "category": "icms",
        "summary": "Resumo válido",
        "publishedAt": "2026-11-20T10:00:00Z",
        "collectedAt": "2026-11-21T10:00:00Z",
        "publicationDateEvidence": "feed:pubdate",
        "impactTags": ["risco"],
    }
    candidates = [
        valid,
        {**valid, "title": '<img src=x onerror="globalThis.pwned=1">'},
        {**valid, "url": "javascript:alert(1)"},
        {**valid, "url": "data:text/html,<script>alert(1)</script>"},
        {**valid, "publishedAt": "inválida"},
        {**valid, "collectedAt": "inválida"},
        {**valid, "publishedAt": "2999-01-01T00:00:00Z", "collectedAt": "2999-01-02T00:00:00Z"},
        {**valid, "publishedAt": "2026-11-22T00:00:00Z"},
        {**valid, "collectedAt": "2999-01-02T00:00:00Z"},
        {**valid, "title": 123},
        {**valid, "impactTags": "risco"},
    ]

    validated = _run_frontend_validator(candidates)

    assert validated[0]["url"] == valid["url"]
    # Markup é texto legítimo e deve permanecer inerte; os demais itens são inválidos.
    assert validated[1]["title"] == candidates[1]["title"]
    assert validated[2:] == [None] * (len(candidates) - 2)


def test_frontend_has_no_innerhtml_sink_for_external_data():
    index = (collect.ROOT / "index.html").read_text(encoding="utf-8")

    assert ".innerHTML" not in index
    assert "e.textContent = String(text)" in index
    assert "a.href = i.url" in index
    assert "urlExternaSegura(item.url)" in index
    assert "if(urlExternaSegura(i.url))" in index
    assert "const payloadPartial = payload.health?.status === 'partial';" in index
    assert "state.collectorPartial = payloadPartial && feedComDataValida;" in index
    assert "Feed parcial" in index


def test_bearer_redirects_are_blocked_before_authorization_can_be_forwarded():
    from urllib.error import HTTPError
    from urllib.request import Request

    request = Request(
        "https://www.jornalminasgerais.mg.gov.br/api/v1/Pesquisa/teste",
        headers={"Authorization": "Bearer segredo"},
    )
    handler = collect.NoBearerRedirectHandler()

    with pytest.raises(HTTPError, match="redirect bloqueado"):
        handler.redirect_request(
            request, None, 302, "Found", {}, "https://attacker.example/roubo"
        )


def test_confaz_partial_detail_failures_are_structured_sanitized_and_publishable():
    index = b'''<table>
      <tr><td><a href="/legislacao/convenios/2026/CV127_26?token=segredo">127</a></td></tr>
      <tr><td><a href="/legislacao/convenios/2026/CV131_26">131</a></td></tr>
    </table>'''
    source = {
        **SOURCE,
        "name": "CONFAZ — Convênios ICMS", "critical": True,
        "url": "https://www.confaz.fazenda.gov.br/legislacao/convenios/2026/2026",
        "type": "html", "htmlContainerTag": "tr", "htmlFollowDetail": True,
        "htmlDetailTitleRegex": r'<p class="TituloAcordo">([\s\S]*?)</p>',
        "htmlPublishedDateRegex": r"Publicado no DOU de ([0-3]?\d\.[01]?\d\.\d{2})",
        "urlAllowRegex": r"confaz\.fazenda\.gov\.br/legislacao/convenios/\d{4}/CV\d{3}_\d{2}",
    }
    failures = []

    def fake_fetch(url):
        if "CV127_26" in url:
            raise TimeoutError("token=segredo não deve vazar")
        return b'''<p class="TituloAcordo">CONVENIO ICMS 131, DE 27 DE OUTUBRO DE 2026</p>
          <p>Publicado no DOU de 29.10.26</p>'''

    items = collect.parse_source_document(
        index, source, datetime(2026, 11, 21, tzinfo=timezone.utc),
        detail_fetcher=fake_fetch, detail_failures=failures,
    )
    status = {
        "source": source["name"], "critical": True, "ok": True,
        "parserRecognized": True, "items": len(items),
        "detailFailureCount": len(failures), "detailFailures": failures,
    }
    health = collect.source_health([status])

    assert len(items) == 1
    assert failures == [{
        "url": "https://www.confaz.fazenda.gov.br/legislacao/convenios/2026/CV127_26",
        "error": "TimeoutError",
    }]
    assert health["status"] == "partial"
    assert health["criticalSourcesPartial"] == [source["name"]]
    assert collect.health_gate_exit_code(health) == 0
