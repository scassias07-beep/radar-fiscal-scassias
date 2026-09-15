# Radar Fiscal — pacote para publicação

Este pacote contém o painel visual, um coletor diário/horário de notícias por RSS/Atom e uma rotina do GitHub Actions para atualizar `data/news.json` automaticamente.

## Publicação rápida

1. Crie um repositório no GitHub.
2. Envie todo o conteúdo deste pacote para a raiz do repositório.
3. Ative **Settings → Pages → Deploy from a branch**, escolhendo `main` e `/root`.
4. Acesse o endereço fornecido pelo GitHub Pages.

O painel funciona sem o coletor usando o instantâneo embutido. Quando `data/news.json` tiver dados, ele incorpora as notícias coletadas ao abrir a página.

## Atualização automática

O workflow roda a cada hora e também pode ser executado manualmente em **Actions → Atualizar notícias fiscais → Run workflow**. A primeira execução precisa de permissão para gravar no repositório; se o GitHub solicitar, habilite `Settings → Actions → General → Workflow permissions → Read and write permissions`.

Para testar localmente:

```bash
python3 collector/collect.py
python3 -m http.server 8000
```

Abra `http://localhost:8000` no navegador. O painel deve carregar `data/news.json`.

## Fontes

As fontes ficam em `collector/sources.json`. Dê preferência a RSS/API oficial. Alguns portais podem não oferecer RSS estável, bloquear robôs ou alterar o endereço do feed; nesses casos, substitua a URL pela fonte autorizada e mantenha o link original no painel.

O coletor guarda título, link, fonte, data, resumo curto, categoria e pontuação de relevância. Ele não copia integralmente matérias protegidas, remove duplicidades por URL canônica e registra o status das fontes em `sources`, `sourcesOk` e `sourcesFailed` dentro de `data/news.json`.

### Critérios do coletor

- Usa somente RSS/Atom lidos com biblioteca padrão do Python, sem dependências externas.
- Mantém fallback: se uma fonte falhar, o histórico local relevante continua no JSON e a falha fica registrada.
- Filtra ruídos por palavras-chave fiscais/tributárias/contábeis e por lista negativa (política genérica, eventos, carreira etc.).
- Normaliza datas para ISO-8601 UTC sempre que o feed informa `pubDate`, `published`, `updated` ou `dc:date`.
- Classifica automaticamente em áreas do painel: reforma, receita, judicial, CARF, ICMS, municipal, aduaneiro e legislação.

## Observações importantes

- GitHub Pages é adequado para o painel, mas não executa Python no navegador; por isso a coleta acontece no GitHub Actions.
- A frequência mínima prática é horária. Para coleta a cada poucos minutos ou alertas por WhatsApp/e-mail, será necessário um backend ou serviço de automação separado.
- Sempre confira o conteúdo na publicação oficial antes de tomar decisões fiscais.
