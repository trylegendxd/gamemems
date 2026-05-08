# Vinted + Facebook Marketplace — Setup Guide

## Vinted

**Não é preciso configurar nada.** A sessão é obtida automaticamente
visitando a homepage do Vinted antes do primeiro pedido à API.

Variáveis opcionais (no `.env`):

```bash
VINTED_MAX_PAGES=3      # nº máximo de páginas por pesquisa (default: 3)
VINTED_PER_PAGE=20      # items por página (default: 20)
```

URLs aceites:
- `https://www.vinted.pt/catalog?search_text=...` ← recomendado
- `https://www.vinted.fr/catalog?search_text=...` (e outros TLDs)
- `https://www.vinted.pt/api/v2/catalog/items?search_text=...`

---

## Facebook Marketplace

⚠️ **Atenção legal e técnica:**
- Scraping do Facebook **viola os Termos de Serviço** da Meta.
- A tua conta pode ser **temporariamente bloqueada** se o FB detectar
  o padrão de pedidos.
- O markup do FB **muda regularmente** — o adaptador é best-effort.
- As cookies expiram (~30 dias) e têm de ser **refrescadas manualmente**.

### Como obter as cookies

1. Abre o Chrome em modo **anónimo** e faz login no Facebook.
2. F12 → tab **Application** → **Cookies** → `https://www.facebook.com`.
3. Copia os valores destas três cookies:
   - `c_user`  (ID numérico do utilizador, ex: `100012345678901`)
   - `xs`      (token de sessão, ex: `42:abc123def...`)
   - `datr`    (impressão digital do browser; opcional mas recomendado)

### Configurar no .env

```bash
FB_C_USER=100012345678901
FB_XS=42%3AabcDEFghi123:2:1234567890:-1:5678
FB_DATR=AbCdEf-gHiJkLmNoP

# Limites opcionais
FB_MAX_LISTINGS=60          # máx de items extraídos por pesquisa
```

### Quando refrescar

Se vires nos logs:

```
[FB] resposta parece página de login — cookies expiradas. Refresca FB_C_USER e FB_XS.
```

Repete os passos acima para obter cookies novas.

### Recomendação de segurança

- **Usa uma conta secundária** do Facebook só para isto.
- **Não** uses a tua conta principal — risco de bloqueio.
- Em **Render/Heroku**, define as variáveis como secrets, **não** as
  comites no `.env`.

---

## URLs de pesquisa do FB Marketplace

Formato base:
```
https://www.facebook.com/marketplace/<cidade>/search?query=<termo>
```

Exemplos:
```
https://www.facebook.com/marketplace/lisbon/search?query=iphone+15
https://www.facebook.com/marketplace/porto/search?query=ps5
https://www.facebook.com/marketplace/braga/search?query=rtx+3070
```

Filtros úteis (adicionar como query params):
- `&minPrice=100` / `&maxPrice=500`
- `&daysSinceListed=7`  (últimos 7 dias)
- `&exact=true`         (match exacto da query)

---

## Como aparece no dashboard / Telegram

Não é preciso fazer nada — o campo `source` flui automaticamente:

- **Dashboard**: cada deal mostra o badge da fonte (`OLX`, `CustoJusto`,
  `Vinted`, `Facebook`).
- **Telegram**: a mensagem inclui `Fonte: <nome>`.
- **CSV export**: coluna `source` está populada.
- **Filtros** na API `/api/deals` aceitam `?source=Vinted` (já existia
  para OLX/CJ).
