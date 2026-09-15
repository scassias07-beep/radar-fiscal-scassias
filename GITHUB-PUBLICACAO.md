# Publicação do Radar Fiscal no GitHub

Este projeto foi preparado para funcionar como painel estático alimentado por coleta automática.

## Estrutura

- `index.html`: painel visual.
- `collector/collect.py`: coletor RSS/Atom fiscal/tributário em Python.
- `collector/sources.json`: fontes e filtros de relevância.
- `data/news.json`: dados consumidos pelo painel.
- `.github/workflows/atualizar-noticias.yml`: rotina horária do GitHub Actions.
- `.nojekyll`: evita processamento Jekyll no GitHub Pages.

## Como publicar

1. Crie um repositório no GitHub, por exemplo `radar-fiscal`.
2. Envie todos os arquivos deste pacote para a raiz do repositório.
3. No GitHub, acesse **Settings → Pages**.
4. Em **Build and deployment**, selecione:
   - Source: `Deploy from a branch`
   - Branch: `main`
   - Folder: `/root`
5. Em **Settings → Actions → General**, confira:
   - Workflow permissions: `Read and write permissions`
6. Rode manualmente o workflow em **Actions → Atualizar notícias fiscais → Run workflow**.
7. Abra a URL do GitHub Pages e confira se o painel carrega `data/news.json`.

## Validação local

```bash
python3 collector/collect.py
python3 -m py_compile collector/collect.py
python3 -m json.tool collector/sources.json >/tmp/radar_sources_check.json
python3 -m json.tool data/news.json >/tmp/radar_news_check.json
python3 -m http.server 8000
```

Depois acesse `http://localhost:8000`.

## Observações

- A atualização pública via GitHub Actions é horária.
- Para atualização em poucos minutos, alertas por WhatsApp/e-mail, login, favoritos ou histórico completo, evoluir para backend com banco e worker dedicado.
- O painel mostra manchetes/resumos e aponta para a fonte original; não copia matérias protegidas integralmente.
