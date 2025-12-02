# Skolinspektionen DATA

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![MCP](https://img.shields.io/badge/MCP-Compatible-green.svg)](https://modelcontextprotocol.io/)

Open data from **Skolinspektionen** (Swedish Schools Inspectorate) via the Model Context Protocol (MCP).

## Overview

This project provides structured, AI-accessible data from [Skolinspektionen](https://www.skolinspektionen.se), enabling language models to search, analyze, and reason about Swedish school inspection data.

### Key Features

- **Intelligent Search** — BM25 relevance ranking with fuzzy matching for typo tolerance
- **Skolenkäten** — Survey results from students, parents, and teachers across all Swedish schools
- **Tillståndsbeslut** — School permit decisions and application outcomes
- **Tillsyn Statistics** — Inspection statistics including fines (viten), targeted supervision (TUI), and planned inspections
- **Kolada Integration** — Municipality-level education KPIs from the Swedish municipality database
- **Publication Archive** — Quality reviews, reports, decisions, and press releases
- **Smart Caching** — Two-tier memory + disk cache for fast repeated queries
- **Data Refresh** — CLI tools for automated data updates with delta tracking

## Installation

```bash
pip install skolinspektionen-data
```

### From Source

```bash
git clone https://github.com/isakskogstad/skolinspektionen-data
cd skolinspektionen-data
pip install -e ".[dev]"
```

## Quick Start

### MCP Integration (Claude Desktop)

Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "skolinspektionen": {
      "command": "si-mcp"
    }
  }
}
```

Or with `uvx` (no installation required):

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

### CLI Tools

```bash
# Refresh all data sources
si-refresh

# Refresh specific sources
si-refresh --sources skolenkaten tillstand

# Check data status
si-refresh --status

# Run the MCP server manually
si-mcp
```

## Available MCP Tools

### Publication Search

| Tool | Description |
|------|-------------|
| `search_publications` | Search publications with BM25 ranking and filters |
| `search_press_releases` | Search press releases by query and year |
| `get_publication_content` | Fetch full publication content as Markdown |
| `get_publication_metadata` | Get publication metadata (faster than full content) |

### Skolenkäten (School Survey)

| Tool | Description |
|------|-------------|
| `search_skolenkaten` | Search survey results by school, municipality, or principal |
| `get_skolenkaten_summary` | Get aggregated statistics for a school unit |
| `list_skolenkaten_files` | List available survey data files |
| `list_skolenkaten_respondent_types` | List respondent categories (students, parents, teachers) |
| `list_skolenkaten_indices` | List survey index definitions |

### Tillståndsbeslut (Permit Decisions)

| Tool | Description |
|------|-------------|
| `search_tillstand` | Search permit decisions by school, municipality, or type |
| `get_tillstand_summary` | Get statistics on permit decisions |
| `list_tillstand_files` | List available permit decision files |

### Tillsyn Statistics (Inspection Data)

| Tool | Description |
|------|-------------|
| `get_viten_statistik` | Fine statistics from school inspections |
| `get_tui_statistik` | Targeted individual supervision statistics |
| `get_planerad_tillsyn_statistik` | Planned inspection statistics |
| `get_tillsyn_summary` | Combined inspection statistics overview |

### Kolada Integration (Municipality Data)

| Tool | Description |
|------|-------------|
| `search_kolada_municipalities` | Search Swedish municipalities |
| `get_kolada_education_stats` | Get education KPIs for a municipality |
| `compare_kolada_municipalities` | Compare education data between municipalities |
| `list_kolada_education_kpis` | List available education indicators |

### Reference Data

| Tool | Description |
|------|-------------|
| `list_publication_types` | All publication types |
| `list_themes` | Inspection themes |
| `list_skolformer` | School types |
| `list_subjects` | School subjects |
| `list_decision_types` | Decision categories |
| `list_regions` | Geographic regions |

### Administration

| Tool | Description |
|------|-------------|
| `refresh_data` | Trigger data refresh |
| `get_refresh_status` | Check refresh status and history |
| `get_cache_stats` | Cache performance statistics |
| `health_check` | Service health and data freshness |

## MCP Resources

| Resource URI | Description |
|--------------|-------------|
| `skolinspektionen://publication-types` | Publication types as JSON |
| `skolinspektionen://themes` | Inspection themes as JSON |
| `skolinspektionen://recent` | 20 most recent publications |

## MCP Prompts

| Prompt | Description |
|--------|-------------|
| `analyze_school` | Comprehensive analysis of a school |
| `compare_schools` | Compare multiple schools |
| `summarize_publication` | Summarize a publication from URL |
| `find_school_decisions` | Find inspection decisions for a school |

## Data Sources

| Source | Description | Update Frequency |
|--------|-------------|------------------|
| **Publications** | Quality reviews, government reports, statistics | Continuous |
| **Skolenkäten** | Survey responses from ~500,000 respondents/year | Biannual (spring/fall) |
| **Tillståndsbeslut** | School permit applications and decisions | Continuous |
| **Tillsyn** | Inspection fines, TUI, planned inspections | Annual |
| **Kolada** | Municipality education KPIs via Kolada API | Annual |

## Project Architecture

```
src/
├── cli/
│   └── refresh.py          # Data refresh CLI
├── config.py               # Configuration (pydantic-settings)
├── mcp/
│   ├── server.py           # MCP server with 30+ tools
│   └── validation.py       # Input validation and sanitization
├── search/
│   └── ranker.py           # BM25 + fuzzy search
└── services/
    ├── browser.py          # Headless browser for JS pages
    ├── cache.py            # Two-tier LRU + disk cache
    ├── fetcher.py          # Secure file downloader
    ├── kolada.py           # Kolada API client
    ├── models.py           # Pydantic data models
    ├── ombedomning.py      # Re-inspection data parser
    ├── parser.py           # HTML → Markdown conversion
    ├── rate_limiter.py     # Token bucket rate limiting
    ├── refresher.py        # Data refresh orchestration
    ├── retry.py            # Exponential backoff + circuit breaker
    ├── scraper.py          # Publication scraper
    ├── skolenkaten.py      # Survey data parser
    ├── tillstand.py        # Permit decision parser
    └── tillsyn_statistik.py # Inspection statistics parser
```

## Configuration

Environment variables (or `.env` file):

| Variable | Default | Description |
|----------|---------|-------------|
| `SI_BASE_URL` | `https://www.skolinspektionen.se` | Base URL |
| `SI_DATA_DIR` | `~/.skolinspektionen-data` | Data storage directory |
| `SI_CACHE_TTL_HOURS` | `24` | Cache time-to-live |
| `SI_RATE_LIMIT` | `2.0` | Requests per second |
| `SI_LOG_LEVEL` | `INFO` | Logging verbosity |

## Security

This project implements multiple security measures:

- **SSRF Protection** — URL validation with domain whitelisting
- **Input Validation** — All MCP tool inputs sanitized and validated
- **Path Traversal Prevention** — Filename sanitization for downloads
- **Content-Type Validation** — Only allowed file types accepted
- **Private IP Blocking** — RFC 1918 and link-local addresses blocked
- **Rate Limiting** — Respectful scraping with configurable limits

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests (268 tests)
pytest

# Run tests with coverage
pytest --cov=src --cov-report=term-missing

# Type checking
mypy src/

# Linting
ruff check src/
```

## License

**AGPL-3.0** — See [LICENSE](LICENSE) for details.

This license ensures that:
- The software remains free and open source
- Modifications must be shared under the same license
- Network use (e.g., as a web service) triggers copyleft provisions

## Contributing

This is a **Civic Tech Sweden** project. Contributions are welcome!

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Make your changes with tests
4. Ensure all tests pass (`pytest`)
5. Submit a pull request

## Related Projects

- [SCB MCP](https://github.com/civictechsweden/scb-mcp) — Statistics Sweden data via MCP
- [Kolada MCP](https://github.com/civictechsweden/kolada-mcp) — Swedish municipality KPIs via MCP
- [g0vse](https://github.com/civictechsweden/g0vse) — Swedish government documents via MCP

## Acknowledgments

- **Skolinspektionen** for publishing open data
- **Kolada** for the municipality statistics API
- **Anthropic** for the Model Context Protocol specification

---

*Built with care for Swedish education transparency.*
