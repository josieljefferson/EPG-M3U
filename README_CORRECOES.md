# EPG IPTV - arquivos corrigidos

## Arquivos

- `script/epg_generator.py` — gerador EPG/XMLTV + playlist M3U.
- `config/channels.json` — fontes e canais.
- `.github/workflows/epg.yml` — GitHub Actions.
- `requirements.txt` — dependências Python.

## Correções principais

1. A playlist usa exclusivamente `output/playlist.m3u` (singular).
2. O gerador aceita `url`, `stream`, `stream_url` e `stream-url` para o endereço do canal.
3. `generate_m3u()` rejeita URLs vazias ou inválidas e falha claramente se não houver nenhum canal reproduzível.
4. `write_m3u_atomic()` valida cabeçalho e quantidade de entradas `#EXTINF`.
5. `validate_outputs()` também verifica se a playlist contém canais.
6. A validação do GitHub Actions usa `awk` para contar `#EXTINF`, evitando o problema de `grep -c` com `set -e`.
7. A configuração mantém somente as fontes desejadas: **mi.tv (1) → Guia de TV (2) → ALEPI (3)**.
8. TV Map e IPTV-org não fazem parte das fontes configuradas.

## Saídas esperadas

- `output/epg.xml`
- `output/epg.xml.gz`
- `output/playlist.m3u`
- `output/epg_aliases.json`
- `output/epg-report.json`
