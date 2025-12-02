# Skolinspektionen DATA

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![MCP](https://img.shields.io/badge/MCP-Compatible-green.svg)](https://modelcontextprotocol.io/)

**Samlar in och tillgängliggör data från Skolinspektionen som öppen, strukturerad data.**

## Varför detta projekt?

[Skolinspektionen](https://www.skolinspektionen.se) är en central myndighet vars beslut och granskningar har betydande inverkan på Sveriges skolor, elever och lärare. Trots detta erbjuder myndigheten inte sina data som öppna data i strukturerat format.

Detta verktyg syftar till att:

- **Öka transparensen** — Göra myndighetens arbete mer tillgängligt för allmänheten
- **Möjliggöra granskning** — Underlätta för journalister, forskare och medborgare att analysera beslut och trender
- **Förbättra digital insyn** — Strukturera information som annars är svår att överblicka
- **Rusta AI för analys** — Via MCP-servern kan AI-verktyg effektivt söka, analysera och sammanställa data från myndigheten

> *"Offentlighetsprincipen innebär att allmänheten har rätt till insyn i statens och kommunernas verksamhet. Denna insyn bör även omfatta digital tillgång till strukturerad data."*

## Vad samlas in?

| Datakälla | Beskrivning | Omfattning |
|-----------|-------------|------------|
| **Publikationer** | Kvalitetsgranskningar, regeringsuppdrag, statistikrapporter | ~2000+ dokument |
| **Skolenkäten** | Enkätsvar från elever, vårdnadshavare och personal | ~500 000 svar/år |
| **Tillståndsbeslut** | Beslut om godkännande av fristående skolor | Löpande |
| **Tillsynsbeslut** | Inspektionsresultat och förelägganden | Löpande |
| **Vitesstatistik** | Statistik över utdömda viten | Årlig |
| **Riktad tillsyn** | Individärenden (TUI) och planerad tillsyn | Årlig |

## Installation

```bash
pip install skolinspektionen-data
```

### Från källkod

```bash
git clone https://github.com/isakskogstad/skolinspektionen-data
cd skolinspektionen-data
pip install -e ".[dev]"
```

## Användning

### MCP-integration (Claude Desktop)

Med MCP-servern kan AI-assistenter som Claude direkt söka och analysera Skolinspektionens data. Lägg till i din Claude Desktop-konfiguration:

```json
{
  "mcpServers": {
    "skolinspektionen": {
      "command": "si-mcp"
    }
  }
}
```

Eller med `uvx` (ingen installation krävs):

```json
{
  "mcpServers": {
    "skolinspektionen": {
      "command": "uvx",
      "args": ["skolinspektionen-data"]
    }
  }
}
```

### Kommandoradsverktyg

```bash
# Uppdatera all data
si-refresh

# Uppdatera specifika källor
si-refresh --sources skolenkaten tillstand

# Visa datastatus
si-refresh --status

# Starta MCP-servern manuellt
si-mcp
```

## MCP-verktyg

### Sök och analys

| Verktyg | Beskrivning |
|---------|-------------|
| `search_publications` | Sök publikationer med relevansrankning |
| `search_press_releases` | Sök pressmeddelanden |
| `get_publication_content` | Hämta fullständigt innehåll som Markdown |
| `search_skolenkaten` | Sök enkätresultat per skola |
| `search_tillstand` | Sök tillståndsbeslut |

### Skolenkäten

| Verktyg | Beskrivning |
|---------|-------------|
| `get_skolenkaten_summary` | Sammanställd statistik för en skolenhet |
| `list_skolenkaten_respondent_types` | Lista respondenttyper |
| `list_skolenkaten_indices` | Lista enkätindex och definitioner |

### Tillstånd och tillsyn

| Verktyg | Beskrivning |
|---------|-------------|
| `get_tillstand_summary` | Statistik över tillståndsbeslut |
| `get_viten_statistik` | Vitesstatistik från tillsyn |
| `get_tui_statistik` | Statistik för riktad tillsyn individ |
| `get_tillsyn_summary` | Samlad tillsynsstatistik |

### Kolada-integration

| Verktyg | Beskrivning |
|---------|-------------|
| `search_kolada_municipalities` | Sök kommuner |
| `get_kolada_education_stats` | Utbildnings-KPI:er per kommun |
| `compare_kolada_municipalities` | Jämför kommuner |

### Referensdata

| Verktyg | Beskrivning |
|---------|-------------|
| `list_publication_types` | Publikationstyper |
| `list_themes` | Granskningsteman |
| `list_skolformer` | Skolformer |
| `list_regions` | Geografiska regioner |

## Teknisk arkitektur

```
src/
├── cli/
│   └── refresh.py          # Datauppdatering via kommandorad
├── mcp/
│   ├── server.py           # MCP-server med 30+ verktyg
│   └── validation.py       # Indatavalidering
├── search/
│   └── ranker.py           # BM25 + fuzzy search
└── services/
    ├── cache.py            # Tvålagers-cache (minne + disk)
    ├── fetcher.py          # Säker filnedladdning
    ├── kolada.py           # Kolada API-klient
    ├── parser.py           # HTML → Markdown
    ├── refresher.py        # Datauppdatering
    ├── skolenkaten.py      # Skolenkäten-parser
    ├── tillstand.py        # Tillståndsbeslut-parser
    └── tillsyn_statistik.py # Tillsynsstatistik-parser
```

## Säkerhet

Projektet implementerar flera säkerhetsåtgärder:

- **SSRF-skydd** — URL-validering med domänvitlistning
- **Indatavalidering** — Alla MCP-verktygsparametrar valideras
- **Path traversal-skydd** — Filnamn saneras vid nedladdning
- **Content-type-validering** — Endast tillåtna filtyper accepteras
- **Rate limiting** — Respektfull datainsamling

## Utveckling

```bash
# Installera med utvecklingsberoenden
pip install -e ".[dev]"

# Kör tester (268 tester)
pytest

# Kör med täckningsrapport
pytest --cov=src --cov-report=term-missing
```

## Licens

**AGPL-3.0** — Se [LICENSE](LICENSE) för detaljer.

Denna licens säkerställer att:
- Mjukvaran förblir fri och öppen källkod
- Modifieringar måste delas under samma licens
- Nätverksanvändning (t.ex. som webbtjänst) utlöser copyleft

## Bidra

Detta är ett **Civic Tech**-projekt. Bidrag välkomnas!

1. Forka repot
2. Skapa en feature branch
3. Gör dina ändringar med tester
4. Säkerställ att alla tester passerar
5. Skicka en pull request

## Relaterade projekt

- [g0vse](https://github.com/civictechsweden/g0vse) — Öppen data från regeringen.se
- [SCB MCP](https://github.com/civictechsweden/scb-mcp) — Statistik från SCB via MCP
- [Kolada MCP](https://github.com/civictechsweden/kolada-mcp) — Kommundata via MCP

## Tack till

- **Skolinspektionen** — för att publicera information på sin webbplats
- **Kolada** — för API:et med kommunstatistik
- **Anthropic** — för Model Context Protocol-specifikationen

---

*Ett civic tech-projekt för ökad transparens i svensk skolinspektion.*
