import json
import re
import subprocess
import socket
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
            self.headers = {}

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
        "open_public_request",
        lambda request, *, source: fake_urlopen(
            request, int(source.get("timeout", collect.TIMEOUT)), strict_context
        ),
    )
    monkeypatch.setattr(
        collect,
        "_open_bearer_request",
        lambda request, *, timeout, context, allowed_hosts, allowed_ports: fake_urlopen(
            request, timeout, context
        ),
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
        "sourcesFailed": ["Receita"],
        "nonCriticalSourcesFailed": [],
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


def test_noncritical_detail_failure_is_explicitly_partial_not_degraded():
    health = collect.source_health(
        [
            {
                "source": "Fonte auxiliar",
                "critical": False,
                "ok": True,
                "parserRecognized": True,
                "items": 0,
                "detailFailureCount": 2,
            }
        ]
    )

    assert health["status"] == "partial"
    assert health["nonCriticalSourcesPartial"] == ["Fonte auxiliar"]
    assert health["criticalSourcesFailed"] == []
    assert collect.health_gate_exit_code(health) == 0


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


def test_receita_federal_config_uses_current_official_html_listing():
    sources = json.loads(collect.CONFIG.read_text(encoding="utf-8"))
    source = next(item for item in sources if item["name"] == "Receita Federal")

    assert source["type"] == "html"
    assert source["url"] == "https://www.gov.br/receitafederal/pt-br/assuntos/noticias"
    assert source["htmlContainerTag"] == "li"
    assert source["htmlPublishedDateRegex"]
    assert source["minTitleLength"] == 12
    assert source["htmlLimit"] == 20
    assert "tribut" in source["includeKeywords"]
    assert "campanha institucional" in source["excludeKeywords"]

    allowed = source["urlAllowRegex"]
    assert re.fullmatch(
        allowed,
        "https://www.gov.br/receitafederal/pt-br/assuntos/noticias/2026/novembro/orientacoes-cbs",
    )
    assert not re.fullmatch(
        allowed,
        "https://www.gov.br/receitafederal/pt-br/assuntos/noticias/orientacoes-cbs",
    )
    assert not re.fullmatch(
        allowed,
        "https://www.gov.br/receitafederal/pt-br/assuntos/noticias/2026/novembro",
    )
    assert re.search(source["urlDenyRegex"], "https://www.gov.br/@@images/foto.png")


@pytest.fixture
def receita_federal_listing_html():
    return b"""
    <html><body>
      <ul class="noticias listagem-noticias-com-foto">
        <li>
          <div class="conteudo">
            <div class="subtitulo-noticia">Tributacao</div>
            <h2 class="titulo"><a href="https://www.gov.br/receitafederal/pt-br/assuntos/noticias/2026/novembro/receita-orienta-sobre-cbs">Receita Federal orienta contribuintes sobre tributacao e CBS</a></h2>
            <span class="descricao"><span class="data"> 20/11/2026 </span></span>
          </div>
        </li>
        <li>
          <div class="conteudo">
            <h2 class="titulo"><a href="/receitafederal/pt-br/assuntos/noticias/2026/novembro/campanha-institucional">Receita promove campanha institucional de boas-vindas</a></h2>
            <span class="descricao"><span class="data"> 19/11/2026 </span></span>
          </div>
        </li>
        <li>
          <div class="conteudo">
            <h2 class="titulo"><a href="/receitafederal/pt-br/assuntos/noticias/2025/dezembro/regras-do-imposto">Receita Federal atualiza regras do imposto e da fiscalizacao</a></h2>
            <span class="descricao"><span class="data"> 01/12/2025 </span></span>
          </div>
        </li>
        <li>
          <a href="/receitafederal/pt-br/assuntos/noticias/2026/novembro/@@images/foto.png">Imagem da noticia tributaria</a>
          <span class="data">20/11/2026</span>
        </li>
      </ul>
    </body></html>
    """


def test_receita_federal_html_parser_and_recency_are_deterministic(
    receita_federal_listing_html,
):
    sources = json.loads(collect.CONFIG.read_text(encoding="utf-8"))
    source = next(item for item in sources if item["name"] == "Receita Federal")
    collected_at = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)

    assert collect.recognizes_source_document(receita_federal_listing_html, source)
    parsed = collect.parse_source_document(
        receita_federal_listing_html, source, collected_at=collected_at
    )

    assert [item["title"] for item in parsed] == [
        "Receita Federal orienta contribuintes sobre tributacao e CBS",
        "Receita Federal atualiza regras do imposto e da fiscalizacao",
    ]
    recent = parsed[0]
    assert recent["publishedAt"] == "2026-11-20T00:00:00+00:00"
    assert recent["publicationDateEvidence"] == "html:configured-regex"
    assert recent["url"] == (
        "https://www.gov.br/receitafederal/pt-br/assuntos/noticias/2026/novembro/"
        "receita-orienta-sobre-cbs"
    )

    active = [
        item
        for item in parsed
        if collect.publication_is_active(item["publishedAt"], collected_at)
    ]
    assert [item["url"] for item in active] == [recent["url"]]


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
    assert "const payloadHealthy = feed.status === 'healthy';" in index
    assert "payload.items.slice(0, MAX_FEED_ITEMS)" in index
    assert "const feedHistorico = feed.historico;" in index
    assert "state.collectorDegraded = !feed.usavel;" in index
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


def _run_frontend_validator(items, now="2026-11-21T12:00:00Z"):
    index = (collect.ROOT / "index.html").read_text(encoding="utf-8")
    functions = []
    for name in ("urlExternaSegura", "validarItemFeed"):
        match = re.search(rf"function {name}\([^\n]+\)\{{[\s\S]*?\n\}}", index)
        assert match, f"função {name} ausente"
        functions.append(match.group(0))
    script = (
        "globalThis.location={href:'https://radar.test/app/'};\n"
        "const ACTIVE_ITEM_MAX_AGE_MS=90*24*60*60*1000;\n"
        + "\n".join(functions)
        + f"\nconst agora=Date.parse({json.dumps(now)});"
        + f"\nconsole.log(JSON.stringify({json.dumps(items)}.map(item=>validarItemFeed(item,agora))));"
    )
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
    assert "const payloadPartial = feed.status === 'partial';" in index
    assert "state.collectorPartial = payloadPartial && feed.aceito;" in index
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


def test_content_links_require_https_and_relative_links_resolve_to_https():
    assert not collect.is_valid_content_link(
        "http://example.gov.br/noticias/ato", "Ato tributário", SOURCE
    )
    assert collect.is_valid_content_link("/noticias/ato", "Ato tributário", SOURCE)
    items = collect.parse_feed(
        rss(entry("Ato tributário", "/noticias/ato", "Fri, 20 Nov 2026 10:00:00 GMT")),
        SOURCE,
        collected_at=datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc),
    )
    assert items[0]["url"] == "https://example.gov.br/noticias/ato"


def test_historical_http_content_url_is_not_reused():
    item = {
        "title": "Prazo tributário",
        "url": "http://example.gov.br/noticias/prazo",
        "summary": "Prazo fiscal",
        "publishedAt": "2026-11-20T12:00:00+00:00",
        "collectedAt": "2026-11-20T13:00:00+00:00",
        "publicationDateEvidence": "feed:pubdate",
    }
    assert not collect.keep_existing(item)


class _ChunkedResponse:
    def __init__(self, body, content_length=None, final_url="https://example.gov.br/feed"):
        self.body = body
        self.headers = {} if content_length is None else {"Content-Length": str(content_length)}
        self._url = final_url

    def read(self, limit=-1):
        if limit < 0:
            return self.body
        result, self.body = self.body[:limit], self.body[limit:]
        return result

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def close(self):
        pass


class _FailingReadResponse(_ChunkedResponse):
    def read(self, limit=-1):
        raise ConnectionResetError(104, "Connection reset by peer")


def test_bounded_network_read_detects_declared_and_streamed_overflow():
    with pytest.raises(collect.ResponseTooLarge, match="limite"):
        collect.read_response_limited(_ChunkedResponse(b"small", content_length=11), 10)
    with pytest.raises(collect.ResponseTooLarge, match="limite"):
        collect.read_response_limited(_ChunkedResponse(b"x" * 11), 10)
    assert collect.read_response_limited(_ChunkedResponse(b"x" * 10), 10) == b"x" * 10


def test_all_collector_network_reads_use_limited_retry_helpers():
    source = (collect.ROOT / "collector" / "collect.py").read_text(encoding="utf-8")
    assert ".read()" not in source
    assert source.count("fetch_public_bytes(") >= 4
    assert source.count("fetch_bearer_bytes(") >= 2
    assert "return read_response_limited(response, max_bytes)" in source


@pytest.mark.parametrize("url", [
    "http://example.gov.br/feed",
    "https://example.gov.br:444/feed",
    "https://other.gov.br/feed",
    "https://127.0.0.1/feed",
    "https://169.254.10.2/feed",
    "https://10.0.0.2/feed",
])
def test_redirect_destination_validation_fails_closed(url, monkeypatch):
    def fake_getaddrinfo(host, port, type=0):
        addresses = {"example.gov.br": "93.184.216.34", "other.gov.br": "93.184.216.35"}
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addresses.get(host, host), port))]
    monkeypatch.setattr(collect.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError):
        collect.validate_network_url(url, allowed_hosts={"example.gov.br"}, allowed_ports={443})


def test_redirect_handler_validates_every_hop_and_final_url(monkeypatch):
    monkeypatch.setattr(
        collect.socket, "getaddrinfo",
        lambda host, port, type=0: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
    )
    handler = collect.SafeRedirectHandler({"example.gov.br"}, {443})
    request = collect.Request("https://example.gov.br/feed")
    with pytest.raises(collect.HTTPError, match="redirect inseguro"):
        handler.redirect_request(request, None, 302, "Found", {}, "http://example.gov.br/next")

    response = _ChunkedResponse(b"ok", final_url="https://other.gov.br/final")
    monkeypatch.setattr(collect, "_open_with_validated_redirects", lambda *args, **kwargs: response)
    with pytest.raises(ValueError, match="host"):
        collect.open_public_request(request, source=SOURCE)


def test_public_download_retries_full_operation_after_body_reset(monkeypatch):
    responses = [
        _FailingReadResponse(b"", final_url=SOURCE["url"]),
        _ChunkedResponse(b"ok", final_url=SOURCE["url"]),
    ]
    attempts = []
    delays = []
    dns_validations = []

    def fake_dns(host, port, type=0):
        dns_validations.append((host, port))
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
        ]

    def flaky_open(*args, **kwargs):
        attempts.append((args, kwargs))
        return responses.pop(0)

    monkeypatch.setattr(collect.socket, "getaddrinfo", fake_dns)
    monkeypatch.setattr(collect, "_open_with_validated_redirects", flaky_open)
    monkeypatch.setattr(collect.time, "sleep", delays.append)

    assert collect.fetch_public_bytes(
        collect.Request(SOURCE["url"]),
        source={**SOURCE, "retryAttempts": 3, "retryDelay": 0.25},
        max_bytes=10,
    ) == b"ok"
    assert len(attempts) == 2
    assert len(dns_validations) == 4  # URL inicial e final em cada tentativa.
    assert delays == [0.25]


def test_public_download_does_not_retry_permanent_http_or_tls_error(monkeypatch):
    import ssl
    from email.message import Message

    failures = [
        collect.HTTPError(SOURCE["url"], 404, "Not Found", Message(), None),
        collect.URLError(ssl.SSLCertVerificationError("certificate rejected")),
    ]
    for failure in failures:
        attempts = []

        def permanent_failure(*args, **kwargs):
            attempts.append((args, kwargs))
            raise failure

        monkeypatch.setattr(collect, "open_public_request", permanent_failure)
        monkeypatch.setattr(
            collect.time,
            "sleep",
            lambda _seconds: pytest.fail("permanent failure must not be retried"),
        )
        with pytest.raises(type(failure)):
            collect.fetch_public_bytes(
                collect.Request(SOURCE["url"]),
                source={**SOURCE, "retryAttempts": 3},
                max_bytes=10,
            )
        assert len(attempts) == 1


def test_bearer_download_retries_body_reset_without_redirecting_token(monkeypatch):
    responses = [
        _FailingReadResponse(b"", final_url=SOURCE["url"]),
        _ChunkedResponse(b"json", final_url=SOURCE["url"]),
    ]
    attempts = []
    monkeypatch.setattr(
        collect,
        "_open_bearer_request",
        lambda *args, **kwargs: (attempts.append(kwargs) or responses.pop(0)),
    )
    monkeypatch.setattr(collect.time, "sleep", lambda _seconds: None)

    request = collect.Request(
        SOURCE["url"], headers={"Authorization": "Bearer secret"}
    )
    assert collect.fetch_bearer_bytes(
        request,
        source={**SOURCE, "retryAttempts": 3, "retryDelay": 0},
        max_bytes=10,
    ) == b"json"
    assert len(attempts) == 2


def test_retry_policy_caps_attempts_and_each_backoff(monkeypatch):
    attempts = []
    delays = []

    def always_reset():
        attempts.append(True)
        raise ConnectionResetError(104, "Connection reset by peer")

    monkeypatch.setattr(collect.time, "sleep", delays.append)
    with pytest.raises(ConnectionResetError):
        collect._download_with_retry(
            always_reset, {"retryAttempts": 99, "retryDelay": 999}
        )

    assert len(attempts) == collect.MAX_RETRY_ATTEMPTS
    assert delays == [collect.MAX_RETRY_DELAY] * (collect.MAX_RETRY_ATTEMPTS - 1)


def test_pages_deploy_retries_artifact_visibility_race():
    workflow = (
        collect.ROOT / ".github" / "workflows" / "atualizar-noticias.yml"
    ).read_text(encoding="utf-8")

    assert workflow.count("uses: actions/deploy-pages@v4") == 2
    assert "continue-on-error: true" in workflow
    assert "steps.deploy_attempt_1.outcome == 'failure'" in workflow
    assert "sleep 5" in workflow


def _frontend_function(name):
    index = (collect.ROOT / "index.html").read_text(encoding="utf-8")
    match = re.search(rf"(?:async )?function {name}\([^\n]*?\)\{{[\s\S]*?\n\}}", index)
    assert match, f"função {name} ausente"
    return match.group(0)


def test_frontend_rejects_http_and_oversized_item_fields():
    valid = {
        "title": "Ato fiscal válido", "url": "https://example.gov.br/ato",
        "source": "Fonte", "publishedAt": "2026-11-20T10:00:00Z",
        "collectedAt": "2026-11-21T10:00:00Z",
        "publicationDateEvidence": "feed:pubdate", "impactTags": [],
    }
    candidates = [valid, {**valid, "url": "/noticia/relativa"},
                  {**valid, "url": "http://example.gov.br/ato"},
                  {**valid, "title": "x" * 221}, {**valid, "summary": "x" * 361},
                  {**valid, "impactTags": ["x" * 65]}]
    validated = _run_frontend_validator(candidates)
    assert validated[0] == valid
    assert validated[1]["url"] == "https://radar.test/noticia/relativa"
    assert validated[2:] == [None] * 4


def test_frontend_caps_payload_bytes_and_item_count_before_use():
    index = (collect.ROOT / "index.html").read_text(encoding="utf-8")
    assert "MAX_FEED_BYTES" in index
    assert "MAX_FEED_ITEMS" in index
    script = "\n".join([
        _frontend_function("lerRespostaLimitada"),
        "const chunk = new TextEncoder().encode('12345678901');",
        "let sent=false; const response = {headers:{get:()=>null}, body:{getReader:()=>({read:async()=>sent?{done:true}:(sent=true,{done:false,value:chunk})})}};",
        "lerRespostaLimitada(response, 10).then(()=>console.log('accepted')).catch(e=>console.log(e.name));",
    ])
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    assert completed.stdout.strip() == "RangeError"
    assert ".slice(0, MAX_FEED_ITEMS)" in index


def test_frontend_freshness_rejects_stale_partial_invalid_and_future_updated_at():
    function = _frontend_function("avaliarPayloadFeed")
    now = int(datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    payloads = [
        {"health": {"status": "healthy"}, "updatedAt": "2026-11-21T11:00:00Z"},
        {"health": {"status": "partial"}, "updatedAt": "2026-11-21T08:00:00Z"},
        {"health": {"status": "partial"}, "updatedAt": "inválida"},
        {"health": {"status": "healthy"}, "updatedAt": "2026-11-21T12:10:01Z"},
    ]
    script = "const FEED_MAX_AGE_MS=2*60*60*1000,FEED_FUTURE_TOLERANCE_MS=5*60*1000;\n" + function + f"\nconsole.log(JSON.stringify({json.dumps(payloads)}.map(p=>avaliarPayloadFeed(p,{now}))));"
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    results = json.loads(completed.stdout)
    assert results[0]["aceito"] is True
    assert results[1]["aceito"] is False and results[1]["historico"] is True
    assert results[2]["aceito"] is False
    assert results[3]["aceito"] is False
    index = (collect.ROOT / "index.html").read_text(encoding="utf-8")
    assert "state.online = feed.aceito;" in index


def test_portal_reforma_tributaria_uses_canonical_https_feed():
    sources = json.loads((collect.ROOT / "collector" / "sources.json").read_text(encoding="utf-8"))
    source = next(item for item in sources if item["name"] == "Portal Reforma Tributária")
    assert source["url"] == "https://www.reformatributaria.com/feed/"


class _BearerResponse:
    def __init__(self, final_url):
        self.final_url = final_url
        self.closed = False

    def geturl(self):
        return self.final_url

    def close(self):
        self.closed = True


def test_bearer_request_validates_initial_and_final_url_immediately_around_open(monkeypatch):
    events = []
    response = _BearerResponse("https://api.example.gov.br/final")

    monkeypatch.setattr(
        collect.socket,
        "getaddrinfo",
        lambda host, port, type=0: events.append(("dns", host, port))
        or [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
    )

    class FakeOpener:
        def open(self, request, timeout):
            events.append(("open", request.full_url))
            return response

    monkeypatch.setattr(collect, "build_opener", lambda *handlers: FakeOpener())
    result = collect._open_bearer_request(
        collect.Request(
            "https://api.example.gov.br/search",
            headers={"Authorization": "Bearer segredo"},
        ),
        timeout=10,
        context=object(),
        allowed_hosts={"api.example.gov.br"},
        allowed_ports={443},
    )

    assert result is response
    assert events == [
        ("dns", "api.example.gov.br", 443),
        ("open", "https://api.example.gov.br/search"),
        ("dns", "api.example.gov.br", 443),
    ]


@pytest.mark.parametrize(
    ("url", "resolved_ip"),
    [
        ("http://api.example.gov.br/search", "93.184.216.34"),
        ("https://api.example.gov.br:444/search", "93.184.216.34"),
        ("https://other.example.gov.br/search", "93.184.216.34"),
        ("https://api.example.gov.br/search", "10.0.0.1"),
        ("https://api.example.gov.br/search", "127.0.0.1"),
        ("https://api.example.gov.br/search", "169.254.1.1"),
    ],
)
def test_bearer_request_rejects_unsafe_initial_destination_before_open(
    url, resolved_ip, monkeypatch
):
    opened = False
    monkeypatch.setattr(
        collect.socket,
        "getaddrinfo",
        lambda host, port, type=0: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (resolved_ip, port))
        ],
    )

    class FakeOpener:
        def open(self, request, timeout):
            nonlocal opened
            opened = True
            return _BearerResponse(request.full_url)

    monkeypatch.setattr(collect, "build_opener", lambda *handlers: FakeOpener())
    with pytest.raises(ValueError):
        collect._open_bearer_request(
            collect.Request(url, headers={"Authorization": "Bearer segredo"}),
            timeout=10,
            context=object(),
            allowed_hosts={"api.example.gov.br"},
            allowed_ports={443},
        )
    assert opened is False


@pytest.mark.parametrize(
    ("final_url", "final_ip"),
    [
        ("http://api.example.gov.br/final", "93.184.216.34"),
        ("https://api.example.gov.br:444/final", "93.184.216.34"),
        ("https://other.example.gov.br/final", "93.184.216.34"),
        ("https://api.example.gov.br/final", "10.0.0.1"),
        ("https://api.example.gov.br/final", "127.0.0.1"),
        ("https://api.example.gov.br/final", "169.254.1.1"),
    ],
)
def test_bearer_request_rejects_unsafe_final_destination_and_closes_response(
    final_url, final_ip, monkeypatch
):
    response = _BearerResponse(final_url)
    resolutions = iter(("93.184.216.34", final_ip))
    monkeypatch.setattr(
        collect.socket,
        "getaddrinfo",
        lambda host, port, type=0: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(resolutions), port))
        ],
    )

    class FakeOpener:
        def open(self, request, timeout):
            return response

    monkeypatch.setattr(collect, "build_opener", lambda *handlers: FakeOpener())
    with pytest.raises(ValueError):
        collect._open_bearer_request(
            collect.Request(
                "https://api.example.gov.br/search",
                headers={"Authorization": "Bearer segredo"},
            ),
            timeout=10,
            context=object(),
            allowed_hosts={"api.example.gov.br"},
            allowed_ports={443},
        )
    assert response.closed is True


def test_complementary_scan_cancels_stream_when_explicit_limit_is_exceeded():
    script = "\n".join([
        "const MAX_SCAN_BYTES=10;",
        "const PROXIES=[u=>u];",
        _frontend_function("tentar"),
        "let cancelled=false; let reads=0;",
        "globalThis.fetch=async()=>({ok:true,body:{getReader:()=>({read:async()=>{reads++; return {done:false,value:new Uint8Array(11)}},cancel:async()=>{cancelled=true}})}});",
        "tentar('https://example.test').then(result=>console.log(JSON.stringify({result,cancelled,reads})));",
    ])
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )
    assert json.loads(completed.stdout) == {
        "result": None,
        "cancelled": True,
        "reads": 1,
    }


def test_stale_partial_fallback_log_counts_partial_items():
    payload = {
        "health": {"status": "partial"},
        "updatedAt": "2020-01-01T00:00:00Z",
        "items": [{"title": "um"}, {"title": "dois"}],
    }
    script = "\n".join([
        "const MAX_FEED_BYTES=2000000,MAX_FEED_ITEMS=300,FEED_MAX_AGE_MS=2*60*60*1000,FEED_FUTURE_TOLERANCE_MS=5*60*1000;",
        "const state={itens:[]}; const CAT={receita:true}; const logs=[];",
        "globalThis.fetch=async()=>({ok:true,headers:{get:()=>null},body:{getReader:()=>{let sent=false;return {read:async()=>sent?{done:true}:(sent=true,{done:false,value:new TextEncoder().encode(" + json.dumps(json.dumps(payload)) + ")})}}}});",
        "const $=()=>({classList:{toggle:()=>{},add:()=>{}},textContent:''});",
        "const log=(message)=>logs.push(message); const validarItemFeed=x=>x;",
        "const atualizarRodapeFeed=()=>{},formatarDataBrasilia=()=>'',norm=x=>x,N=x=>x,deadlineFuturo=()=>false;",
        "const renderKPIs=()=>{},renderExecutive=()=>{},refreshViews=()=>{};",
        _frontend_function("lerRespostaLimitada"),
        _frontend_function("avaliarPayloadFeed"),
        _frontend_function("carregarFeedPublico"),
        "carregarFeedPublico().then(()=>console.log(JSON.stringify(logs)));",
    ])
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )
    assert any("2 itens disponíveis" in message for message in json.loads(completed.stdout))


def test_noncritical_source_failure_makes_health_partial_and_coherent():
    statuses = [
        {"source": "Receita", "critical": True, "ok": True, "parserRecognized": True, "items": 2},
        {"source": "Fonte auxiliar", "critical": False, "ok": False, "parserRecognized": False, "items": 0, "error": "timeout"},
    ]

    health = collect.source_health(statuses)

    assert health["status"] == "partial"
    assert health["sourcesFailed"] == ["Fonte auxiliar"]
    assert health["nonCriticalSourcesFailed"] == ["Fonte auxiliar"]
    assert health["criticalSourcesFailed"] == []
    assert collect.health_gate_exit_code(health) == 0


def test_active_item_policy_uses_publication_date_and_has_inclusive_90_day_limit():
    reference = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)

    assert collect.ACTIVE_ITEM_MAX_AGE.days == 90
    assert collect.publication_is_active("2026-08-23T12:00:00Z", reference)
    assert not collect.publication_is_active("2026-08-23T11:59:59Z", reference)
    assert not collect.publication_is_active("2026-11-21T12:00:01Z", reference)

    old = {
        "title": "Prazo tributário antigo",
        "url": "https://example.gov.br/noticias/antiga",
        "summary": "Prazo fiscal",
        "publishedAt": "2026-08-23T11:59:59Z",
        "collectedAt": "2026-11-21T11:00:00Z",
        "publicationDateEvidence": "feed:pubdate",
    }
    assert not collect.keep_existing(old, reference)


def test_frontend_rejects_old_item_even_when_payload_and_collection_are_recent():
    item = {
        "title": "Conteúdo antigo republicado no payload",
        "url": "https://example.gov.br/antigo",
        "source": "Fonte",
        "publishedAt": "2026-08-23T11:59:59Z",
        "collectedAt": "2026-11-21T11:59:00Z",
        "publicationDateEvidence": "feed:pubdate",
        "impactTags": ["prazo"],
        "priority": "alta",
    }

    assert _run_frontend_validator([item], now="2026-11-21T12:00:00Z") == [None]


def test_frontend_treats_sources_failed_as_partial_and_labels_degraded_state():
    function = _frontend_function("avaliarPayloadFeed")
    payload = {
        "health": {"status": "healthy"},
        "sourcesFailed": [{"source": "Fonte auxiliar", "error": "timeout"}],
        "updatedAt": "2026-11-21T11:00:00Z",
    }
    script = (
        "const FEED_MAX_AGE_MS=2*60*60*1000,FEED_FUTURE_TOLERANCE_MS=5*60*1000;\n"
        + function
        + "\nconsole.log(JSON.stringify(avaliarPayloadFeed("
        + json.dumps(payload)
        + ",Date.parse('2026-11-21T12:00:00Z'))));"
    )
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )
    result = json.loads(completed.stdout)
    assert result["status"] == "partial"
    assert result["aceito"] is True

    index = (collect.ROOT / "index.html").read_text(encoding="utf-8")
    assert "'Feed parcial'" in index
    assert "'Feed degradado'" in index
    assert "statusDeclarado === 'healthy' && temFalhas ? 'partial'" in index


def test_frontend_source_statuses_are_bounded_sanitized_and_rendered_factually():
    payload = {
        "sourceStats": [
            {
                "source": "STJ — Notícias",
                "url": "https://www.stj.jus.br/sites/portalp/Inicio",
                "ok": False,
                "detailFailureCount": 0,
            },
            {
                "source": "Receita Federal",
                "url": "https://www.gov.br/receitafederal/pt-br/assuntos/noticias/RSS",
                "ok": True,
                "detailFailureCount": 0,
            },
            {
                "source": "CONFAZ — Convênios ICMS",
                "url": "https://www.confaz.fazenda.gov.br/legislacao/convenios/2026/2026",
                "ok": True,
                "detailFailureCount": 2,
            },
            {"source": "<img src=x onerror=alert(1)>", "ok": "sim"},
            {"source": "x" * 161, "ok": True},
        ]
        + [{"source": f"extra-{number}", "ok": True} for number in range(70)]
    }
    script = "\n".join([
        "globalThis.location={href:'https://radar.test/'};",
        "const MAX_SOURCE_STATUSES=64;",
        "const FONTES=[",
        "{n:'STJ · Jurisprudência',u:'https://scon.stj.jus.br/SCON/',h:'stj',a:['STJ — Notícias']},",
        "{n:'Receita Federal',u:'https://www.gov.br/receitafederal/pt-br/assuntos/noticias',h:'receita'},",
        "{n:'CONFAZ',u:'https://www.confaz.fazenda.gov.br/legislacao',h:'confaz',a:['CONFAZ — Convênios ICMS','CONFAZ — Ajustes SINIEF','CONFAZ — Protocolos ICMS']},",
        "{n:'Sem coleta',u:'https://sem-coleta.example/',h:'sem-coleta'}];",
        "const box={children:[],replaceChildren(){this.children=[]},appendChild(node){this.children.push(node)}};",
        "const $=()=>box; const el=(tag,cls,text)=>({tag,className:cls||'',textContent:text==null?'':String(text),children:[],appendChild(node){this.children.push(node)}});",
        _frontend_function("urlExternaSegura"),
        _frontend_function("normalizarNomeFonte"),
        _frontend_function("sanitizarStatusFontes"),
        "const state={online:true,varrendo:false,sourceStatuses:sanitizarStatusFontes(" + json.dumps(payload) + ")};",
        _frontend_function("statusDaFonte"),
        _frontend_function("renderFontes"),
        "renderFontes();",
        "console.log(JSON.stringify({statuses:state.sourceStatuses,labels:box.children.map(row=>row.children[1].textContent)}));",
    ])
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )
    result = json.loads(completed.stdout)

    assert len(result["statuses"]) == 62
    assert all(
        status["source"] != "<img src=x onerror=alert(1)>"
        for status in result["statuses"]
    )
    assert result["labels"] == ["falha", "conectada", "parcial", "sem diagnóstico"]


def test_frontend_source_status_fallback_and_partial_copy_are_safe_and_generic():
    index = (collect.ROOT / "index.html").read_text(encoding="utf-8")
    script = "\n".join([
        "globalThis.location={href:'https://radar.test/'}; const MAX_SOURCE_STATUSES=64;",
        _frontend_function("urlExternaSegura"),
        _frontend_function("sanitizarStatusFontes"),
        "const fromSources=sanitizarStatusFontes({sources:[{source:'Fonte segura',url:'https://example.gov.br/feed',ok:true,detailFailureCount:1}]});",
        "const relative=sanitizarStatusFontes({sourceStats:[{source:'Fonte relativa',url:'/relative',ok:false,detailFailureCount:0}]});",
        "console.log(JSON.stringify({fromSources,relative,missing:sanitizarStatusFontes({})}));",
    ])
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )
    result = json.loads(completed.stdout)

    assert result["fromSources"] == [{
        "source": "Fonte segura", "url": "https://example.gov.br/feed",
        "ok": True, "detailFailureCount": 1,
    }]
    assert result["relative"] == [{
        "source": "Fonte relativa", "url": "https://radar.test/relative",
        "ok": False, "detailFailureCount": 0,
    }]
    assert result["missing"] == []
    assert "uma ou mais fontes apresentaram falhas" in index
    assert "falhas parciais de detalhes em fonte crítica" not in index
    assert 'label = "sem diagnóstico"' in index
    assert "else if(state.online)" not in index


def test_frontend_source_status_aggregates_explicit_groups_in_any_order():
    statuses = [
        {
            "source": "CONFAZ — Convênios ICMS",
            "url": "https://www.confaz.fazenda.gov.br/legislacao/convenios/2026/2026",
            "ok": True,
            "detailFailureCount": 3,
        },
        {
            "source": "CONFAZ — Ajustes SINIEF",
            "url": "https://www.confaz.fazenda.gov.br/legislacao/ajustes/2026/2026",
            "ok": False,
            "detailFailureCount": 0,
        },
        {
            "source": "CONFAZ — Protocolos ICMS",
            "url": "https://www.confaz.fazenda.gov.br/legislacao/protocolos/2026/2026",
            "ok": True,
            "detailFailureCount": 0,
        },
    ]
    fonte = {
        "n": "CONFAZ",
        "u": "https://www.confaz.fazenda.gov.br/legislacao",
        "a": [
            "CONFAZ — Convênios ICMS",
            "CONFAZ — Ajustes SINIEF",
            "CONFAZ — Protocolos ICMS",
        ],
    }
    script = "\n".join(
        [
            _frontend_function("normalizarNomeFonte"),
            "const fonte=" + json.dumps(fonte) + ";",
            "const ordens=" + json.dumps([statuses, list(reversed(statuses))]) + ";",
            _frontend_function("statusDaFonte"),
            "console.log(JSON.stringify(ordens.map(sourceStatuses=>{globalThis.state={sourceStatuses};return statusDaFonte(fonte)})));",
        ]
    )

    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )

    assert json.loads(completed.stdout) == [
        {"ok": False, "detailFailureCount": 0},
        {"ok": False, "detailFailureCount": 0},
    ]


def test_frontend_source_status_partial_and_no_diagnostic_are_unambiguous():
    confaz = {
        "n": "CONFAZ",
        "a": ["CONFAZ — Convênios ICMS", "CONFAZ — Ajustes SINIEF"],
    }
    receita = {
        "n": "Receita Federal",
        "u": "https://www.gov.br/receitafederal/pt-br/assuntos/noticias",
    }
    partial_statuses = [
        {"source": "CONFAZ — Ajustes SINIEF", "ok": True, "detailFailureCount": 0},
        {"source": "CONFAZ — Convênios ICMS", "ok": True, "detailFailureCount": 2},
    ]
    shared_hostname_only = [
        {
            "source": "PGFN Notícias",
            "url": "https://www.gov.br/pgfn/pt-br/assuntos/noticias",
            "ok": False,
            "detailFailureCount": 0,
        }
    ]
    script = "\n".join(
        [
            _frontend_function("normalizarNomeFonte"),
            _frontend_function("statusDaFonte"),
            "globalThis.state={sourceStatuses:" + json.dumps(partial_statuses) + "};",
            "const partial=statusDaFonte(" + json.dumps(confaz) + ");",
            "state.sourceStatuses=" + json.dumps(shared_hostname_only) + ";",
            "const semDiagnostico=statusDaFonte(" + json.dumps(receita) + ");",
            "console.log(JSON.stringify({partial,semDiagnostico}));",
        ]
    )

    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )

    assert json.loads(completed.stdout) == {
        "partial": {"ok": True, "detailFailureCount": 2},
        "semDiagnostico": None,
    }


def test_main_persists_partial_payload_with_failure_and_source_stats(monkeypatch, tmp_path):
    config = tmp_path / "sources.json"
    output = tmp_path / "news.json"
    config.write_text(
        json.dumps([
            {"name": "Fonte crítica", "url": "https://example.gov.br/critica", "type": "jornal_mg", "critical": True},
            {"name": "Fonte auxiliar", "url": "https://example.gov.br/auxiliar", "type": "jornal_mg", "critical": False},
        ]),
        encoding="utf-8",
    )

    def fake_collect(source, now):
        if source["name"] == "Fonte auxiliar":
            raise TimeoutError("indisponível")
        published = (now - collect.timedelta(days=1)).isoformat()
        return [{
            "id": "item-recente",
            "title": "Ato tributário recente",
            "url": "https://example.gov.br/ato",
            "source": source["name"],
            "category": "receita",
            "summary": "Ato fiscal",
            "publishedAt": published,
            "collectedAt": now.isoformat(),
            "publicationDateEvidence": "jornal-mg-api:dataPublicacao",
            "score": 10,
            "impactTags": ["oportunidade"],
        }]

    monkeypatch.setattr(collect, "CONFIG", config)
    monkeypatch.setattr(collect, "OUTPUT", output)
    monkeypatch.setattr(collect, "load_old", lambda: {"items": []})
    monkeypatch.setattr(collect, "collect_jornal_mg", fake_collect)

    assert collect.main() == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["health"]["status"] == "partial"
    assert payload["health"]["sourcesFailed"] == ["Fonte auxiliar"]
    assert [failure["source"] for failure in payload["sourcesFailed"]] == ["Fonte auxiliar"]
    assert payload["sourceStats"] == payload["sources"]
    assert next(status for status in payload["sourceStats"] if status["source"] == "Fonte auxiliar")["ok"] is False
    assert payload["meta"]["activeItemMaxAgeDays"] == 90
    assert [item["id"] for item in payload["items"]] == ["item-recente"]


def test_versioned_snapshot_satisfies_the_frontend_feed_contract():
    payload = json.loads((collect.ROOT / "data" / "news.json").read_text(encoding="utf-8"))
    allowed_evidence = {
        "feed:pubdate", "feed:published", "feed:date",
        "html:time[datetime]", "html:time", "html:configured-regex",
        "html:detail:configured-regex", "dou:pubDate",
        "jornal-mg-api:dataPublicacao",
    }

    assert payload.get("health", {}).get("status") in {"healthy", "partial"}
    assert datetime.fromisoformat(payload["updatedAt"])
    assert payload.get("items")
    assert payload.get("sources")
    validated = []
    for start in range(0, len(payload["items"]), 20):
        validated.extend(_run_frontend_validator(
            payload["items"][start:start + 20],
            now=payload["updatedAt"],
        ))
    assert all(item is not None for item in validated)
    for item in payload["items"]:
        assert item["publicationDateEvidence"] in allowed_evidence


def test_frontend_validates_feed_items_without_passing_the_map_index_as_time():
    index = (collect.ROOT / "index.html").read_text(encoding="utf-8")
    assert ".map(item => validarItemFeed(item)).filter(Boolean)" in index
    assert ".map(validarItemFeed).filter(Boolean)" not in index
