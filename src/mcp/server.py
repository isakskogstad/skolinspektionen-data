"""MCP server for Skolinspektionen data.

Provides tools for searching and fetching publications, decisions,
and statistics from Skolinspektionen (Swedish Schools Inspectorate).

Enhanced with:
- BM25 + fuzzy search for better relevance
- Caching for faster repeated queries
- Health monitoring and cache statistics
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    Tool,
    TextContent,
    GetPromptResult,
    Prompt,
    PromptMessage,
    PromptArgument,
    Resource,
)

from ..config import get_settings
from ..search.ranker import search_publications, search_press_releases, SearchResult
from ..services.cache import get_content_cache
from ..services.models import (
    Index,
    Publication,
    PUBLICATION_TYPES,
    THEMES,
    SKOLFORMER,
    SUBJECTS,
    DECISION_TYPES,
    REGIONS,
    TERMINER,
    YEAR_RANGE,
    SKOLENKATEN_RESPONDENT_TYPES,
    SKOLENKATEN_INDEX,
    TILLSTAND_BESLUT_TYPES,
    TILLSTAND_ANSOKNINGSTYPER,
    TILLSTAND_SKOLFORMER,
)
from ..services.parser import ContentParser
from ..services.scraper import PublicationScraper
from ..services.skolenkaten import (
    parse_skolenkaten_excel,
    create_summary,
    discover_skolenkaten_files,
    search_schools_in_results,
)
from ..services.kolada import (
    search_municipalities,
    get_municipality,
    get_education_stats,
    compare_municipalities,
    list_education_kpis,
    EDUCATION_KPIS,
)
from ..services.tillstand import (
    parse_tillstand_excel,
    create_summary as create_tillstand_summary,
    discover_tillstand_files,
    search_tillstand,
)
from ..services.tillsyn_statistik import (
    parse_viten_excel,
    parse_tui_excel,
    parse_planerad_tillsyn_excel,
    load_all_tillsyn_statistik,
    discover_tillsyn_files,
)
from ..services.ombedomning import (
    get_all_reports as get_ombedomning_reports,
    get_report_by_year as get_ombedomning_by_year,
    get_latest_report as get_latest_ombedomning,
    get_summary as get_ombedomning_summary,
)
from ..services.refresher import DataRefresher, run_refresh
from ..services.models import (
    TILLSYN_CATEGORIES,
    TUI_ASSESSMENT_AREAS,
)
from .validation import (
    validate_string,
    validate_int,
    validate_limit,
    validate_year,
    validate_url,
    validate_enum,
    validate_bool,
)

# Initialize server
server = Server("skolinspektionen-data")

# Global state
_index: Optional[Index] = None
_parser: Optional[ContentParser] = None
_skolenkaten_cache: dict = {}  # Cache for parsed Skolenkäten data by file path
_tillstand_cache: dict = {}  # Cache for parsed Tillståndsbeslut data by file path
_tillsyn_cache: dict = {}  # Cache for parsed Tillsyn statistics data


def get_data_dir() -> Path:
    """Get the data directory path."""
    settings = get_settings()
    return settings.data_dir


async def load_index() -> Index:
    """Load or create the publication index."""
    global _index

    if _index is not None:
        return _index

    settings = get_settings()
    index_path = settings.index_path

    if index_path.exists():
        with open(index_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            _index = Index(**data)
    else:
        # Create a minimal index if none exists
        _index = Index(last_updated=datetime.now().isoformat())

    return _index


async def get_parser() -> ContentParser:
    """Get or create the content parser."""
    global _parser

    if _parser is None:
        _parser = ContentParser()
        await _parser.__aenter__()

    return _parser


def _format_search_results(results: list[SearchResult]) -> list[dict]:
    """Format search results for JSON output."""
    return [
        {
            "title": getattr(r.item, "title", ""),
            "url": getattr(r.item, "url", ""),
            "type": getattr(r.item, "type", None),
            "type_name": PUBLICATION_TYPES.get(getattr(r.item, "type", ""), ""),
            "published": (
                r.item.published.isoformat()
                if hasattr(r.item, "published") and r.item.published
                else None
            ),
            "summary": getattr(r.item, "summary", None),
            "relevance": {
                "score": round(r.score, 3),
                "match_type": r.match_type,
                "label": r.relevance_label,
            },
            "highlight": r.highlight,
        }
        for r in results
    ]


# === TOOLS ===


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    return [
        Tool(
            name="search_publications",
            description="Search for publications from Skolinspektionen using intelligent relevance ranking. Supports fuzzy matching for typo tolerance. Filter by school form, subject, theme, type, and year.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (matches title and summary with BM25 ranking and fuzzy matching)",
                    },
                    "type": {
                        "type": "string",
                        "description": f"Filter by publication type. Options: {', '.join(PUBLICATION_TYPES.keys())}",
                        "enum": list(PUBLICATION_TYPES.keys()),
                    },
                    "skolform": {
                        "type": "string",
                        "description": f"Filter by school form (skolform). Options: {', '.join(list(SKOLFORMER.keys())[:8])}...",
                        "enum": list(SKOLFORMER.keys()),
                    },
                    "theme": {
                        "type": "string",
                        "description": f"Filter by inspection theme. Options: {', '.join(list(THEMES.keys())[:6])}...",
                        "enum": list(THEMES.keys()),
                    },
                    "subject": {
                        "type": "string",
                        "description": f"Filter by school subject (ämne). Options: {', '.join(list(SUBJECTS.keys())[:8])}...",
                        "enum": list(SUBJECTS.keys()),
                    },
                    "year": {
                        "type": "integer",
                        "description": f"Filter by publication year ({min(YEAR_RANGE)}-{max(YEAR_RANGE)})",
                        "minimum": min(YEAR_RANGE),
                        "maximum": max(YEAR_RANGE),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results (default: 20)",
                        "default": 20,
                    },
                },
            },
        ),
        Tool(
            name="search_press_releases",
            description="Search press releases from Skolinspektionen.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query",
                    },
                    "year": {
                        "type": "integer",
                        "description": "Filter by year",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results (default: 20)",
                        "default": 20,
                    },
                },
            },
        ),
        Tool(
            name="get_publication_content",
            description="Get the full content of a publication as Markdown. Use this to read the actual text of a report or decision.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL or path of the publication (e.g., /beslut-rapporter/publikationer/kvalitetsgranskning/2024/...)",
                    },
                },
                "required": ["url"],
            },
        ),
        Tool(
            name="get_publication_metadata",
            description="Get metadata about a publication without fetching full content. Faster than get_publication_content.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL or path of the publication",
                    },
                },
                "required": ["url"],
            },
        ),
        Tool(
            name="list_publication_types",
            description="List all available publication types from Skolinspektionen.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="list_themes",
            description="List all inspection themes used by Skolinspektionen.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="list_skolformer",
            description="List all school forms (skolformer) in the Swedish education system.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="list_subjects",
            description="List all school subjects (ämnen) covered in Skolinspektionen reports.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="list_decision_types",
            description="List all decision/inspection types (besluts- och granskningstyper) from Skolinspektionen.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="list_regions",
            description="List all inspection regions used by Skolinspektionen.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="get_statistics_files",
            description="Get a list of available statistics files (Excel, PDF) with download URLs.",
            inputSchema={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Filter by category (tillstand, tillsyn, kvalitetsgranskning, skolenkaten, arsrapport)",
                    },
                    "year": {
                        "type": "integer",
                        "description": "Filter by year",
                    },
                },
            },
        ),
        Tool(
            name="refresh_index",
            description="Refresh the publication index by scraping the Skolinspektionen website. Use sparingly - only when you need the latest data.",
            inputSchema={
                "type": "object",
                "properties": {
                    "max_pages": {
                        "type": "integer",
                        "description": "Maximum number of pages to scrape (default: 10)",
                        "default": 10,
                    },
                },
            },
        ),
        Tool(
            name="get_cache_stats",
            description="Get cache statistics including memory and disk usage. Useful for monitoring performance.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="health_check",
            description="Check the health and status of the Skolinspektionen data service.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        # === SKOLENKÄTEN TOOLS ===
        Tool(
            name="search_skolenkaten",
            description="Search Skolenkäten (school survey) results by school name, municipality, or operator. Returns survey index scores (1-10 scale) for areas like trygghet (safety), studiero (study environment), stimulans (motivation), etc.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query - matches school name",
                    },
                    "kommun": {
                        "type": "string",
                        "description": "Filter by municipality name",
                    },
                    "huvudman": {
                        "type": "string",
                        "description": "Filter by operator (huvudman) - school owner/organization",
                    },
                    "respondent_type": {
                        "type": "string",
                        "description": f"Filter by respondent type. Options: {', '.join(list(SKOLENKATEN_RESPONDENT_TYPES.keys())[:5])}...",
                        "enum": list(SKOLENKATEN_RESPONDENT_TYPES.keys()),
                    },
                    "year": {
                        "type": "integer",
                        "description": "Filter by survey year (2015-2025)",
                        "minimum": 2015,
                        "maximum": 2025,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results (default: 20)",
                        "default": 20,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_skolenkaten_summary",
            description="Get summary statistics from Skolenkäten surveys including national averages for all index areas. Useful for understanding typical survey results to compare against individual schools.",
            inputSchema={
                "type": "object",
                "properties": {
                    "respondent_type": {
                        "type": "string",
                        "description": f"Respondent type. Options: {', '.join(list(SKOLENKATEN_RESPONDENT_TYPES.keys())[:5])}...",
                        "enum": list(SKOLENKATEN_RESPONDENT_TYPES.keys()),
                    },
                    "year": {
                        "type": "integer",
                        "description": "Survey year (default: latest available)",
                    },
                },
            },
        ),
        Tool(
            name="list_skolenkaten_respondent_types",
            description="List all respondent types in Skolenkäten surveys (students, parents, teachers by school form).",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="list_skolenkaten_indices",
            description="List all index categories measured in Skolenkäten (trygghet, studiero, stimulans, etc.) with descriptions.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="list_skolenkaten_files",
            description="List available Skolenkäten Excel files by year and respondent type.",
            inputSchema={
                "type": "object",
                "properties": {
                    "year": {
                        "type": "integer",
                        "description": "Filter by year",
                    },
                },
            },
        ),
        # === KOLADA TOOLS ===
        Tool(
            name="search_kolada_municipalities",
            description="Search for Swedish municipalities by name. Returns municipality IDs for use with other Kolada tools.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Municipality name to search for (e.g., 'Stockholm', 'Malmö')",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum results (default: 10)",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_kolada_education_stats",
            description="Get education statistics for a municipality from Kolada. Includes costs, student outcomes, teacher qualifications for grundskola/gymnasieskola/förskola.",
            inputSchema={
                "type": "object",
                "properties": {
                    "municipality_id": {
                        "type": "string",
                        "description": "Municipality ID (e.g., '0180' for Stockholm). Use search_kolada_municipalities to find IDs.",
                    },
                    "year": {
                        "type": "integer",
                        "description": "Year for statistics (default: latest available)",
                    },
                },
                "required": ["municipality_id"],
            },
        ),
        Tool(
            name="compare_kolada_municipalities",
            description="Compare a specific education KPI across multiple municipalities.",
            inputSchema={
                "type": "object",
                "properties": {
                    "municipality_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of municipality IDs to compare",
                    },
                    "kpi_id": {
                        "type": "string",
                        "description": f"KPI ID to compare. Options include: {', '.join(list(EDUCATION_KPIS.keys())[:5])}...",
                    },
                    "year": {
                        "type": "integer",
                        "description": "Year for comparison",
                    },
                },
                "required": ["municipality_ids", "kpi_id"],
            },
        ),
        Tool(
            name="list_kolada_education_kpis",
            description="List available education KPIs from Kolada with descriptions.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        # === TILLSTÅNDSBESLUT TOOLS ===
        Tool(
            name="search_tillstand",
            description="Search permit decisions (tillståndsbeslut) for starting or expanding independent schools (fristående skolor). Returns decisions with school name, municipality, decision type, and grade-level approvals.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search by school name or applicant",
                    },
                    "kommun": {
                        "type": "string",
                        "description": "Filter by municipality",
                    },
                    "skolform": {
                        "type": "string",
                        "description": f"Filter by school form. Options: {', '.join(TILLSTAND_SKOLFORMER.keys())}",
                    },
                    "beslutstyp": {
                        "type": "string",
                        "description": f"Filter by decision type. Options: {', '.join(TILLSTAND_BESLUT_TYPES.keys())}",
                    },
                    "ansokningstyp": {
                        "type": "string",
                        "description": f"Filter by application type. Options: {', '.join(TILLSTAND_ANSOKNINGSTYPER.keys())}",
                    },
                    "year": {
                        "type": "integer",
                        "description": "Filter by decision year (2018-2025)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results (default: 20)",
                        "default": 20,
                    },
                },
            },
        ),
        Tool(
            name="get_tillstand_summary",
            description="Get summary statistics for permit decisions by year. Shows totals, approval rates, and breakdowns by school form and application type.",
            inputSchema={
                "type": "object",
                "properties": {
                    "year": {
                        "type": "integer",
                        "description": "Decision year (default: latest available)",
                    },
                },
            },
        ),
        Tool(
            name="list_tillstand_beslut_types",
            description="List all decision types, application types, and school forms for tillståndsbeslut.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="list_tillstand_files",
            description="List available Tillståndsbeslut Excel files by year.",
            inputSchema={
                "type": "object",
                "properties": {
                    "year": {
                        "type": "integer",
                        "description": "Filter by year",
                    },
                },
            },
        ),
        # === TILLSYN STATISTICS TOOLS (BEO/TUI) ===
        Tool(
            name="get_viten_statistik",
            description="Get statistics on viten (fines) imposed by Skolinspektionen. Shows yearly data on number of fine decisions and court applications for both private and public schools.",
            inputSchema={
                "type": "object",
                "properties": {
                    "year": {
                        "type": "integer",
                        "description": "Filter by specific year (2011-2024 available)",
                    },
                },
            },
        ),
        Tool(
            name="get_tui_statistik",
            description="Get TUI (Tillsyn Utifrån Individärenden) / BEO statistics - decisions from individual complaints about schools. Includes breakdown by gender, school form, and type of deficiency (kränkande behandling, stöd, etc.).",
            inputSchema={
                "type": "object",
                "properties": {
                    "year": {
                        "type": "integer",
                        "description": "Filter by year",
                    },
                },
            },
        ),
        Tool(
            name="get_planerad_tillsyn_statistik",
            description="Get statistics on planerad tillsyn (planned supervision) - regular scheduled inspections of schools. Shows decisions, deficiency rates, and breakdown by school form.",
            inputSchema={
                "type": "object",
                "properties": {
                    "year": {
                        "type": "integer",
                        "description": "Filter by year",
                    },
                },
            },
        ),
        Tool(
            name="get_tillsyn_summary",
            description="Get a combined summary of all Tillsyn (supervision) statistics including viten, TUI/BEO, and planerad tillsyn across all available years.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="list_tillsyn_categories",
            description="List all supervision categories and TUI assessment areas used by Skolinspektionen.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        # === OMBEDÖMNING NATIONELLA PROV TOOLS ===
        Tool(
            name="get_ombedomning_reports",
            description="Get all available reports on ombedömning (re-evaluation) of national tests. "
                        "These reports analyze consistency in grading across schools. "
                        "Note: Data is only available as PDF reports, not structured data.",
            inputSchema={
                "type": "object",
                "properties": {
                    "year": {
                        "type": "integer",
                        "description": "Filter by publication year (optional)",
                    },
                },
            },
        ),
        Tool(
            name="get_ombedomning_summary",
            description="Get a summary of all available ombedömning nationella prov reports, "
                        "including years covered, subjects tested, and the latest report.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        # === DATA REFRESH TOOLS ===
        Tool(
            name="refresh_data",
            description="Refresh data from all or specific sources. Downloads new files from "
                        "Skolinspektionen, fetches API data from Kolada, and updates the local cache. "
                        "Use this to ensure data is up-to-date.",
            inputSchema={
                "type": "object",
                "properties": {
                    "sources": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["publications", "skolenkaten", "tillstand", "tillsyn", "kolada"],
                        },
                        "description": "List of sources to refresh. Leave empty for all sources.",
                    },
                    "force": {
                        "type": "boolean",
                        "description": "Force re-download of files even if they haven't changed",
                        "default": False,
                    },
                },
            },
        ),
        Tool(
            name="get_refresh_status",
            description="Get the current status of data refresh operations, including last refresh "
                        "times for each source and recent refresh history.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="list_data_sources",
            description="List all available data sources and their current status.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Handle tool calls."""

    if name == "search_publications":
        return await _search_publications(arguments)

    elif name == "search_press_releases":
        return await _search_press_releases(arguments)

    elif name == "get_publication_content":
        return await _get_publication_content(arguments)

    elif name == "get_publication_metadata":
        return await _get_publication_metadata(arguments)

    elif name == "list_publication_types":
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "publication_types": [
                            {"key": k, "name": v} for k, v in PUBLICATION_TYPES.items()
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]

    elif name == "list_themes":
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "themes": [
                            {"key": k, "name": v} for k, v in THEMES.items()
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]

    elif name == "list_skolformer":
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "skolformer": [
                            {"key": k, "name": v} for k, v in SKOLFORMER.items()
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]

    elif name == "list_subjects":
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "subjects": [
                            {"key": k, "name": v} for k, v in SUBJECTS.items()
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]

    elif name == "list_decision_types":
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "decision_types": [
                            {"key": k, "name": v} for k, v in DECISION_TYPES.items()
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]

    elif name == "list_regions":
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "regions": [
                            {"key": k, "name": v} for k, v in REGIONS.items()
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]

    elif name == "get_statistics_files":
        return await _get_statistics_files(arguments)

    elif name == "refresh_index":
        return await _refresh_index(arguments)

    elif name == "get_cache_stats":
        return await _get_cache_stats()

    elif name == "health_check":
        return await _health_check()

    # === SKOLENKÄTEN HANDLERS ===
    elif name == "search_skolenkaten":
        return await _search_skolenkaten(arguments)

    elif name == "get_skolenkaten_summary":
        return await _get_skolenkaten_summary(arguments)

    elif name == "list_skolenkaten_respondent_types":
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "respondent_types": [
                            {"key": k, "name": v}
                            for k, v in SKOLENKATEN_RESPONDENT_TYPES.items()
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]

    elif name == "list_skolenkaten_indices":
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "indices": [
                            {"key": k, "name": v, "scale": "1-10"}
                            for k, v in SKOLENKATEN_INDEX.items()
                        ],
                        "description": "Index scores range from 1-10, where higher is better. Each index aggregates multiple survey questions.",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]

    elif name == "list_skolenkaten_files":
        return await _list_skolenkaten_files(arguments)

    # === KOLADA HANDLERS ===
    elif name == "search_kolada_municipalities":
        return await _search_kolada_municipalities(arguments)

    elif name == "get_kolada_education_stats":
        return await _get_kolada_education_stats(arguments)

    elif name == "compare_kolada_municipalities":
        return await _compare_kolada_municipalities(arguments)

    elif name == "list_kolada_education_kpis":
        kpis = list_education_kpis()
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "education_kpis": [
                            {"id": k, "description": v} for k, v in kpis.items()
                        ],
                        "usage": "Use these KPI IDs with get_kolada_education_stats or compare_kolada_municipalities",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]

    # === TILLSTÅNDSBESLUT HANDLERS ===
    elif name == "search_tillstand":
        return await _search_tillstand(arguments)

    elif name == "get_tillstand_summary":
        return await _get_tillstand_summary(arguments)

    elif name == "list_tillstand_beslut_types":
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "beslut_types": [
                            {"key": k, "name": v}
                            for k, v in TILLSTAND_BESLUT_TYPES.items()
                        ],
                        "ansokningstyper": [
                            {"key": k, "name": v}
                            for k, v in TILLSTAND_ANSOKNINGSTYPER.items()
                        ],
                        "skolformer": [
                            {"key": k, "name": v}
                            for k, v in TILLSTAND_SKOLFORMER.items()
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]

    elif name == "list_tillstand_files":
        return await _list_tillstand_files(arguments)

    # === TILLSYN STATISTICS HANDLERS (BEO/TUI) ===
    elif name == "get_viten_statistik":
        return await _get_viten_statistik(arguments)

    elif name == "get_tui_statistik":
        return await _get_tui_statistik(arguments)

    elif name == "get_planerad_tillsyn_statistik":
        return await _get_planerad_tillsyn_statistik(arguments)

    elif name == "get_tillsyn_summary":
        return await _get_tillsyn_summary(arguments)

    elif name == "list_tillsyn_categories":
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "tillsyn_categories": [
                            {"key": k, "name": v}
                            for k, v in TILLSYN_CATEGORIES.items()
                        ],
                        "tui_assessment_areas": [
                            {"key": k, "name": v}
                            for k, v in TUI_ASSESSMENT_AREAS.items()
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]

    # === OMBEDÖMNING HANDLERS ===
    elif name == "get_ombedomning_reports":
        return _handle_ombedomning_reports(arguments)

    elif name == "get_ombedomning_summary":
        return _handle_ombedomning_summary()

    # === DATA REFRESH HANDLERS ===
    elif name == "refresh_data":
        return await _handle_refresh_data(arguments)

    elif name == "get_refresh_status":
        return _handle_refresh_status()

    elif name == "list_data_sources":
        return _handle_list_data_sources()

    else:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def _search_publications(args: dict) -> list[TextContent]:
    """Search publications with improved relevance ranking."""
    index = await load_index()

    # Validate inputs
    query = validate_string(args.get("query"), max_length=500, default="")
    type_filter = validate_enum(args.get("type"), set(PUBLICATION_TYPES.keys()))
    theme_filter = validate_enum(args.get("theme"), set(THEMES.keys()))
    skolform_filter = validate_enum(args.get("skolform"), set(SKOLFORMER.keys()))
    subject_filter = validate_enum(args.get("subject"), set(SUBJECTS.keys()))
    year_filter = validate_year(args.get("year"))
    limit = validate_limit(args.get("limit"), default=20)

    # Apply pre-filters
    publications = index.publications

    if theme_filter:
        publications = [p for p in publications if theme_filter in p.themes]

    if skolform_filter:
        publications = [p for p in publications if skolform_filter in p.skolformer]

    if subject_filter:
        publications = [p for p in publications if subject_filter in p.subjects]

    # Use enhanced search if query provided
    if query:
        results = search_publications(
            publications,
            query=query,
            max_results=limit,
            publication_type=type_filter,
            year=year_filter,
        )
        formatted = _format_search_results(results)
    else:
        # No query - just filter and return
        filtered = publications
        if type_filter:
            filtered = [p for p in filtered if p.type == type_filter]
        if year_filter:
            filtered = [
                p for p in filtered if p.published and p.published.year == year_filter
            ]

        formatted = [
            {
                "title": p.title,
                "url": p.url,
                "type": p.type,
                "type_name": PUBLICATION_TYPES.get(p.type, p.type),
                "published": p.published.isoformat() if p.published else None,
                "summary": p.summary,
                "themes": p.themes,
                "skolformer": p.skolformer,
                "subjects": p.subjects,
            }
            for p in filtered[:limit]
        ]

    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "count": len(formatted),
                    "query": query or None,
                    "filters": {
                        "type": type_filter,
                        "theme": theme_filter,
                        "skolform": skolform_filter,
                        "subject": subject_filter,
                        "year": year_filter,
                    },
                    "publications": formatted,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    ]


async def _search_press_releases(args: dict) -> list[TextContent]:
    """Search press releases."""
    index = await load_index()

    # Validate inputs
    query = validate_string(args.get("query"), max_length=500, default="")
    year_filter = validate_year(args.get("year"))
    limit = validate_limit(args.get("limit"), default=20)

    if query:
        results = search_press_releases(
            index.press_releases,
            query=query,
            max_results=limit,
            year=year_filter,
        )
        formatted = [
            {
                "title": getattr(r.item, "title", ""),
                "url": getattr(r.item, "url", ""),
                "published": (
                    r.item.published.isoformat()
                    if hasattr(r.item, "published") and r.item.published
                    else None
                ),
                "relevance": {
                    "score": round(r.score, 3),
                    "match_type": r.match_type,
                },
            }
            for r in results
        ]
    else:
        releases = index.press_releases
        if year_filter:
            releases = [
                r for r in releases if r.published and r.published.year == year_filter
            ]

        formatted = [
            {
                "title": r.title,
                "url": r.url,
                "published": r.published.isoformat() if r.published else None,
            }
            for r in releases[:limit]
        ]

    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "count": len(formatted),
                    "press_releases": formatted,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    ]


async def _get_publication_content(args: dict) -> list[TextContent]:
    """Get full publication content."""
    # Validate URL (SSRF protection - defense in depth)
    url = validate_url(args.get("url"), require_allowed_domain=True)

    if not url:
        return [TextContent(type="text", text="Error: Valid Skolinspektionen URL is required")]

    parser = await get_parser()
    content = await parser.fetch_publication_content(url)

    if not content:
        return [
            TextContent(type="text", text=f"Error: Could not fetch content from {url}")
        ]

    # Format as readable output
    output = f"# {content['title']}\n\n"

    if content.get("metadata", {}).get("published"):
        output += f"**Publicerad:** {content['metadata']['published']}\n\n"

    if content.get("metadata", {}).get("diarienummer"):
        output += f"**Diarienummer:** {content['metadata']['diarienummer']}\n\n"

    output += "---\n\n"
    output += content["markdown"]

    if content.get("attachments"):
        output += "\n\n---\n\n## Bilagor\n\n"
        for att in content["attachments"]:
            output += f"- [{att.name}]({att.url}) ({att.file_type})\n"

    return [TextContent(type="text", text=output)]


async def _get_publication_metadata(args: dict) -> list[TextContent]:
    """Get publication metadata without full content."""
    # Validate URL (SSRF protection - defense in depth)
    url = validate_url(args.get("url"), require_allowed_domain=True)

    if not url:
        return [TextContent(type="text", text="Error: Valid Skolinspektionen URL is required")]

    # Search for the publication in the index
    index = await load_index()

    # Normalize URL for comparison
    url_normalized = url.strip("/")
    if url_normalized.startswith("http"):
        url_normalized = url_normalized.split("skolinspektionen.se")[-1].strip("/")

    for pub in index.publications:
        pub_url = pub.url.strip("/")
        if pub_url == url_normalized or url_normalized in pub_url:
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "title": pub.title,
                            "url": pub.url,
                            "type": pub.type,
                            "type_name": PUBLICATION_TYPES.get(pub.type, pub.type),
                            "published": pub.published.isoformat() if pub.published else None,
                            "summary": pub.summary,
                            "themes": pub.themes,
                            "attachments": [
                                {"name": a.name, "url": a.url, "type": a.file_type}
                                for a in pub.attachments
                            ],
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
            ]

    return [TextContent(type="text", text=f"Publication not found in index: {url}")]


async def _get_statistics_files(args: dict) -> list[TextContent]:
    """Get statistics files."""
    index = await load_index()
    settings = get_settings()

    category_filter = args.get("category")
    year_filter = args.get("year")

    results = []
    for f in index.statistics_files:
        if category_filter and f.category != category_filter:
            continue
        if year_filter and f.year != year_filter:
            continue
        results.append(f)

    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "count": len(results),
                    "files": [
                        {
                            "name": f.name,
                            "url": f"{settings.base_url}{f.url}",
                            "file_type": f.file_type,
                            "category": f.category,
                            "year": f.year,
                            "description": f.description,
                        }
                        for f in results
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    ]


async def _refresh_index(args: dict) -> list[TextContent]:
    """Refresh the publication index."""
    global _index

    max_pages = args.get("max_pages", 10)

    async with PublicationScraper() as scraper:
        _index = await scraper.build_index()

    # Save to file
    settings = get_settings()
    data_dir = settings.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    with open(settings.index_path, "w", encoding="utf-8") as f:
        json.dump(_index.model_dump(mode="json"), f, ensure_ascii=False, indent=2)

    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "status": "success",
                    "total_items": _index.total_items,
                    "publications": len(_index.publications),
                    "press_releases": len(_index.press_releases),
                    "statistics_files": len(_index.statistics_files),
                    "last_updated": _index.last_updated,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    ]


async def _get_cache_stats() -> list[TextContent]:
    """Get cache statistics."""
    cache = get_content_cache()
    stats = await cache.get_stats()

    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "memory_cache": {
                        "size": stats["memory"]["size"],
                        "max_size": stats["memory"]["max_size"],
                        "total_hits": stats["memory"]["total_hits"],
                    },
                    "disk_cache": {
                        "size": stats["disk"]["size"],
                        "total_bytes": stats["disk"]["total_bytes"],
                        "cache_dir": stats["disk"]["cache_dir"],
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    ]


async def _health_check() -> list[TextContent]:
    """Check service health."""
    index = await load_index()
    settings = get_settings()

    # Check data freshness
    last_updated = None
    data_age_hours = None
    if index.last_updated:
        try:
            last_dt = datetime.fromisoformat(index.last_updated.replace("Z", "+00:00"))
            last_updated = index.last_updated
            delta = datetime.now() - last_dt.replace(tzinfo=None)
            data_age_hours = round(delta.total_seconds() / 3600, 1)
        except Exception:
            pass

    # Check if index file exists
    index_exists = settings.index_path.exists()

    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "status": "healthy",
                    "data": {
                        "index_exists": index_exists,
                        "last_updated": last_updated,
                        "data_age_hours": data_age_hours,
                        "is_stale": data_age_hours > 48 if data_age_hours else True,
                        "total_items": index.total_items,
                    },
                    "counts": {
                        "publications": len(index.publications),
                        "press_releases": len(index.press_releases),
                        "statistics_files": len(index.statistics_files),
                    },
                    "config": {
                        "base_url": settings.base_url,
                        "cache_ttl_hours": settings.cache_ttl_hours,
                        "rate_limit_per_second": settings.rate_limit_per_second,
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    ]


# === SKOLENKÄTEN HANDLERS ===


def _get_skolenkaten_data_dir() -> Path:
    """Get the directory containing Skolenkäten Excel files."""
    # First check for local downloaded website data
    local_paths = [
        Path.home() / "Desktop" / "www.skolinspektionen.se" / "globalassets" / "02-beslut-rapporter-stat" / "statistik" / "statistik-skolenkaten",
        Path("/Users/isak/Desktop/www.skolinspektionen.se/globalassets/02-beslut-rapporter-stat/statistik/statistik-skolenkaten"),
    ]
    for p in local_paths:
        if p.exists():
            return p

    # Fall back to configured data directory
    settings = get_settings()
    return settings.data_dir / "skolenkaten"


async def _load_skolenkaten_data(
    respondent_type: Optional[str] = None,
    year: Optional[int] = None,
) -> list:
    """Load Skolenkäten data from Excel files with caching."""
    global _skolenkaten_cache

    data_dir = _get_skolenkaten_data_dir()
    if not data_dir.exists():
        return []

    # Discover files
    files = discover_skolenkaten_files(data_dir)
    if not files:
        return []

    # Filter files by year if specified
    if year:
        from ..services.skolenkaten import parse_year_from_path
        files = [f for f in files if parse_year_from_path(f) == year]

    # Filter by respondent type if specified
    if respondent_type:
        from ..services.skolenkaten import parse_respondent_type
        files = [f for f in files if parse_respondent_type(f.name)[0] == respondent_type]

    results = []
    for file_path in files:
        cache_key = str(file_path)
        if cache_key not in _skolenkaten_cache:
            # Parse and cache
            parsed = parse_skolenkaten_excel(file_path)
            _skolenkaten_cache[cache_key] = parsed
        results.extend(_skolenkaten_cache[cache_key])

    return results


async def _search_skolenkaten(args: dict) -> list[TextContent]:
    """Search Skolenkäten survey results."""
    # Validate inputs
    query = validate_string(args.get("query"), max_length=200, default="")
    kommun = validate_string(args.get("kommun"), max_length=100, default=None) or None
    huvudman = validate_string(args.get("huvudman"), max_length=200, default=None) or None
    respondent_type = validate_enum(args.get("respondent_type"), set(SKOLENKATEN_RESPONDENT_TYPES.keys()))
    year = validate_year(args.get("year"))
    limit = validate_limit(args.get("limit"), default=20)

    if not query:
        return [TextContent(type="text", text="Error: query is required")]

    # Load data
    all_results = await _load_skolenkaten_data(respondent_type, year)

    if not all_results:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": "No Skolenkäten data available",
                        "hint": "Ensure Skolenkäten Excel files are in the data directory",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]

    # Search
    filtered = search_schools_in_results(all_results, query, kommun, huvudman)

    # Format results
    formatted = []
    for r in filtered[:limit]:
        formatted.append({
            "skolenhet": r.skolenhet,
            "skolenhetskod": r.skolenhetskod,
            "huvudman": r.huvudman,
            "kommun": r.kommun,
            "year": r.year,
            "term": r.term,
            "respondent_type": r.respondent_type,
            "respondent_type_name": SKOLENKATEN_RESPONDENT_TYPES.get(r.respondent_type, r.respondent_type),
            "antal_svar": r.antal_svar,
            "svarsfrekvens": r.svarsfrekvens,
            "indices": {
                "information": r.index_information,
                "stimulans": r.index_stimulans,
                "stod": r.index_stod,
                "kritiskt_tankande": r.index_kritiskt_tankande,
                "bemotande_larare": r.index_bemotande_larare,
                "bemotande_elever": r.index_bemotande_elever,
                "inflytande": r.index_inflytande,
                "studiero": r.index_studiero,
                "trygghet": r.index_trygghet,
                "forhindra_krankningar": r.index_forhindra_krankningar,
                "elevhalsa": r.index_elevhalsa,
                "nojdhet": r.index_nojdhet,
            },
        })

    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "count": len(formatted),
                    "total_matches": len(filtered),
                    "query": query,
                    "filters": {
                        "kommun": kommun,
                        "huvudman": huvudman,
                        "respondent_type": respondent_type,
                        "year": year,
                    },
                    "results": formatted,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    ]


async def _get_skolenkaten_summary(args: dict) -> list[TextContent]:
    """Get Skolenkäten summary statistics."""
    respondent_type = args.get("respondent_type")
    year = args.get("year")

    # Load data
    all_results = await _load_skolenkaten_data(respondent_type, year)

    if not all_results:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": "No Skolenkäten data available",
                        "hint": "Ensure Skolenkäten Excel files are in the data directory",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]

    # Create summary
    summary = create_summary(all_results)

    if not summary:
        return [TextContent(type="text", text="Error: Could not create summary")]

    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "year": summary.year,
                    "term": summary.term,
                    "respondent_type": summary.respondent_type,
                    "respondent_type_name": SKOLENKATEN_RESPONDENT_TYPES.get(
                        summary.respondent_type, summary.respondent_type
                    ),
                    "total_schools": summary.total_schools,
                    "total_responses": summary.total_responses,
                    "average_response_rate": round(summary.average_response_rate, 1)
                    if summary.average_response_rate
                    else None,
                    "national_averages": {
                        "information": round(summary.national_index_information, 2)
                        if summary.national_index_information
                        else None,
                        "stimulans": round(summary.national_index_stimulans, 2)
                        if summary.national_index_stimulans
                        else None,
                        "stod": round(summary.national_index_stod, 2)
                        if summary.national_index_stod
                        else None,
                        "kritiskt_tankande": round(summary.national_index_kritiskt_tankande, 2)
                        if summary.national_index_kritiskt_tankande
                        else None,
                        "bemotande_larare": round(summary.national_index_bemotande_larare, 2)
                        if summary.national_index_bemotande_larare
                        else None,
                        "bemotande_elever": round(summary.national_index_bemotande_elever, 2)
                        if summary.national_index_bemotande_elever
                        else None,
                        "inflytande": round(summary.national_index_inflytande, 2)
                        if summary.national_index_inflytande
                        else None,
                        "studiero": round(summary.national_index_studiero, 2)
                        if summary.national_index_studiero
                        else None,
                        "trygghet": round(summary.national_index_trygghet, 2)
                        if summary.national_index_trygghet
                        else None,
                        "forhindra_krankningar": round(
                            summary.national_index_forhindra_krankningar, 2
                        )
                        if summary.national_index_forhindra_krankningar
                        else None,
                        "elevhalsa": round(summary.national_index_elevhalsa, 2)
                        if summary.national_index_elevhalsa
                        else None,
                        "nojdhet": round(summary.national_index_nojdhet, 2)
                        if summary.national_index_nojdhet
                        else None,
                    },
                    "index_descriptions": SKOLENKATEN_INDEX,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    ]


async def _list_skolenkaten_files(args: dict) -> list[TextContent]:
    """List available Skolenkäten Excel files."""
    from ..services.skolenkaten import parse_year_from_path, parse_respondent_type

    year_filter = args.get("year")
    data_dir = _get_skolenkaten_data_dir()

    if not data_dir.exists():
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": "Skolenkäten data directory not found",
                        "searched": str(data_dir),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]

    files = discover_skolenkaten_files(data_dir)

    # Group by year
    by_year: dict = {}
    for f in files:
        year = parse_year_from_path(f)
        if year_filter and year != year_filter:
            continue
        resp_type, skolform = parse_respondent_type(f.name)

        if year not in by_year:
            by_year[year] = []

        by_year[year].append({
            "filename": f.name,
            "respondent_type": resp_type,
            "respondent_type_name": SKOLENKATEN_RESPONDENT_TYPES.get(resp_type, resp_type),
            "skolform": skolform,
        })

    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "total_files": sum(len(v) for v in by_year.values()),
                    "years": sorted(by_year.keys(), reverse=True),
                    "files_by_year": {
                        str(k): v for k, v in sorted(by_year.items(), reverse=True)
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    ]


# === KOLADA HANDLERS ===


async def _search_kolada_municipalities(args: dict) -> list[TextContent]:
    """Search for municipalities."""
    # Validate inputs
    query = validate_string(args.get("query"), max_length=100, default="")
    limit = validate_limit(args.get("limit"), default=10)

    if not query:
        return [TextContent(type="text", text="Error: query is required")]

    try:
        results = await search_municipalities(query, limit)
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "count": len(results),
                        "query": query,
                        "municipalities": results,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]
    except Exception as e:
        return [TextContent(type="text", text=f"Error searching municipalities: {e}")]


async def _get_kolada_education_stats(args: dict) -> list[TextContent]:
    """Get education statistics for a municipality."""
    # Validate inputs
    municipality_id = validate_string(args.get("municipality_id"), max_length=10, default="")
    year = validate_year(args.get("year"))

    if not municipality_id:
        return [TextContent(type="text", text="Error: municipality_id is required")]

    try:
        # Get municipality info
        muni = await get_municipality(municipality_id)
        muni_name = muni.get("title", municipality_id) if muni else municipality_id

        # Get education stats
        stats = await get_education_stats(municipality_id, year)

        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "municipality": {
                            "id": municipality_id,
                            "name": muni_name,
                        },
                        "year": year or "latest",
                        "education_statistics": stats["kpis"],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]
    except Exception as e:
        return [TextContent(type="text", text=f"Error fetching education stats: {e}")]


async def _compare_kolada_municipalities(args: dict) -> list[TextContent]:
    """Compare municipalities on a specific KPI."""
    municipality_ids = args.get("municipality_ids", [])
    kpi_id = args.get("kpi_id", "")
    year = args.get("year")

    if not municipality_ids:
        return [TextContent(type="text", text="Error: municipality_ids is required")]
    if not kpi_id:
        return [TextContent(type="text", text="Error: kpi_id is required")]

    try:
        results = await compare_municipalities(municipality_ids, kpi_id, year)
        kpis = list_education_kpis()

        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "kpi": {
                            "id": kpi_id,
                            "description": kpis.get(kpi_id, kpi_id),
                        },
                        "year": year or "latest",
                        "comparison": results,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]
    except Exception as e:
        return [TextContent(type="text", text=f"Error comparing municipalities: {e}")]


# === TILLSTÅNDSBESLUT HANDLERS ===


def _get_tillstand_data_dir() -> Path:
    """Get the directory containing Tillståndsbeslut Excel files."""
    # First check for local downloaded website data
    local_paths = [
        Path.home() / "Desktop" / "www.skolinspektionen.se" / "globalassets" / "02-beslut-rapporter-stat" / "statistik" / "statistik-tillstand",
        Path("/Users/isak/Desktop/www.skolinspektionen.se/globalassets/02-beslut-rapporter-stat/statistik/statistik-tillstand"),
    ]
    for p in local_paths:
        if p.exists():
            return p

    # Fall back to the configured data directory
    settings = get_settings()
    return settings.data_dir / "tillstand"


async def _load_tillstand_data(year: int | None = None) -> list:
    """Load Tillståndsbeslut data from Excel files.

    Args:
        year: Optional year filter

    Returns:
        List of TillstandBeslut objects
    """
    global _tillstand_cache

    data_dir = _get_tillstand_data_dir()
    if not data_dir.exists():
        return []

    files = discover_tillstand_files(data_dir)

    all_results = []
    for file_path in files:
        # Use cache if available
        cache_key = str(file_path)
        if cache_key not in _tillstand_cache:
            results = parse_tillstand_excel(file_path)
            _tillstand_cache[cache_key] = results

        file_results = _tillstand_cache[cache_key]

        # Apply year filter
        if year:
            file_results = [r for r in file_results if r.year == year]

        all_results.extend(file_results)

    return all_results


async def _search_tillstand(args: dict) -> list[TextContent]:
    """Search Tillståndsbeslut data."""
    # Validate inputs
    query = validate_string(args.get("query"), max_length=200, default=None) or None
    kommun = validate_string(args.get("kommun"), max_length=100, default=None) or None
    skolform = validate_enum(args.get("skolform"), set(TILLSTAND_SKOLFORMER.keys()))
    beslutstyp = validate_enum(args.get("beslutstyp"), set(TILLSTAND_BESLUT_TYPES.keys()))
    ansokningstyp = validate_enum(args.get("ansokningstyp"), set(TILLSTAND_ANSOKNINGSTYPER.keys()))
    year = validate_year(args.get("year"))
    limit = validate_limit(args.get("limit"), default=20)

    # Load data
    all_results = await _load_tillstand_data(year)

    if not all_results:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": "No Tillståndsbeslut data available",
                        "hint": "Ensure Tillståndsbeslut Excel files are in the data directory",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]

    # Search/filter
    filtered = search_tillstand(
        all_results,
        query=query,
        kommun=kommun,
        skolform=skolform,
        beslutstyp=beslutstyp,
        ansokningstyp=ansokningstyp,
    )

    # Limit results
    filtered = filtered[:limit]

    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "count": len(filtered),
                    "query": query,
                    "filters": {
                        "kommun": kommun,
                        "skolform": skolform,
                        "beslutstyp": beslutstyp,
                        "ansokningstyp": ansokningstyp,
                        "year": year,
                    },
                    "results": [
                        {
                            "arendenummer": r.arendenummer,
                            "skola": r.skola,
                            "kommun": r.kommun,
                            "sokande": r.sokande,
                            "skolform": r.skolform,
                            "ansokningstyp": r.ansokningstyp,
                            "beslutstyp": r.beslutstyp,
                            "skolstart_lasar": r.skolstart_lasar,
                            "year": r.year,
                            # Include grade-level decisions if applicable
                            "beslut_per_arskurs": {
                                f"ak{i}": getattr(r, f"beslut_ak{i}")
                                for i in range(1, 10)
                                if getattr(r, f"beslut_ak{i}")
                            } or None,
                            "gymnasie_programs": r.gymnasie_programs,
                        }
                        for r in filtered
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    ]


async def _get_tillstand_summary(args: dict) -> list[TextContent]:
    """Get Tillståndsbeslut summary statistics."""
    year = args.get("year")

    # Load data
    all_results = await _load_tillstand_data(year)

    if not all_results:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": "No Tillståndsbeslut data available",
                        "hint": "Ensure Tillståndsbeslut Excel files are in the data directory",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]

    # If no year specified, get the latest year
    if not year:
        year = max(r.year for r in all_results)
        all_results = [r for r in all_results if r.year == year]

    # Create summary
    summary = create_tillstand_summary(all_results)

    if not summary:
        return [TextContent(type="text", text="Error: Could not create summary")]

    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "year": summary.year,
                    "skolstart_lasar": summary.skolstart_lasar,
                    "total_decisions": summary.total_decisions,
                    "godkannanden": summary.godkannanden,
                    "avslag": summary.avslag,
                    "avskrivningar": summary.avskrivningar,
                    "approval_rate": round(
                        summary.godkannanden / summary.total_decisions * 100, 1
                    ) if summary.total_decisions > 0 else 0,
                    "by_application_type": {
                        "nyetableringar": {
                            "total": summary.nyetableringar_total,
                            "godkanda": summary.nyetableringar_godkanda,
                            "approval_rate": round(
                                summary.nyetableringar_godkanda / summary.nyetableringar_total * 100, 1
                            ) if summary.nyetableringar_total > 0 else 0,
                        },
                        "utokningar": {
                            "total": summary.utokningar_total,
                            "godkanda": summary.utokningar_godkanda,
                            "approval_rate": round(
                                summary.utokningar_godkanda / summary.utokningar_total * 100, 1
                            ) if summary.utokningar_total > 0 else 0,
                        },
                    },
                    "by_skolform": summary.by_skolform,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    ]


async def _list_tillstand_files(args: dict) -> list[TextContent]:
    """List available Tillståndsbeslut Excel files."""
    from ..services.tillstand import parse_year_from_path, parse_skolstart_from_path

    year_filter = args.get("year")
    data_dir = _get_tillstand_data_dir()

    if not data_dir.exists():
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": "Tillståndsbeslut data directory not found",
                        "searched": str(data_dir),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]

    files = discover_tillstand_files(data_dir)

    # Group by year
    by_year: dict = {}
    for f in files:
        year = parse_year_from_path(f)
        if year_filter and year != year_filter:
            continue
        skolstart = parse_skolstart_from_path(f)

        if year not in by_year:
            by_year[year] = []

        by_year[year].append({
            "filename": f.name,
            "skolstart_lasar": skolstart,
        })

    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "total_files": sum(len(v) for v in by_year.values()),
                    "years": sorted(by_year.keys(), reverse=True),
                    "files_by_year": {
                        str(k): v for k, v in sorted(by_year.items(), reverse=True)
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    ]


# === TILLSYN STATISTICS HANDLERS (BEO/TUI) ===


def _get_tillsyn_data_dir() -> Path:
    """Get the directory containing Tillsyn statistics Excel files."""
    # First check for local downloaded website data
    # Files are in /statistik/ not /statistikrapporter/
    local_paths = [
        Path.home() / "Desktop" / "www.skolinspektionen.se" / "globalassets" / "02-beslut-rapporter-stat" / "statistik",
        Path("/Users/isak/Desktop/www.skolinspektionen.se/globalassets/02-beslut-rapporter-stat/statistik"),
    ]
    for p in local_paths:
        if p.exists():
            return p

    # Fall back to the configured data directory
    settings = get_settings()
    return settings.data_dir / "tillsyn-statistik"


async def _load_tillsyn_data():
    """Load all Tillsyn statistics data with caching."""
    global _tillsyn_cache

    if "summary" in _tillsyn_cache:
        return _tillsyn_cache["summary"]

    data_dir = _get_tillsyn_data_dir()
    if not data_dir.exists():
        return None

    summary = load_all_tillsyn_statistik(data_dir)
    _tillsyn_cache["summary"] = summary
    return summary


async def _get_viten_statistik(args: dict) -> list[TextContent]:
    """Get Viten (fines) statistics."""
    year = args.get("year")

    summary = await _load_tillsyn_data()
    if not summary:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": "No Tillsyn statistics data available",
                        "hint": "Ensure Tillsyn statistics Excel files are in the data directory",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]

    viten = summary.viten
    if year:
        viten = [v for v in viten if v.year == year]

    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "description": "Viten (fines) statistics from Skolinspektionen. Shows decisions to impose fines on school operators and applications to courts for enforcement.",
                    "count": len(viten),
                    "years_available": sorted(set(v.year for v in summary.viten), reverse=True),
                    "filter_year": year,
                    "data": [
                        {
                            "year": v.year,
                            "beslut_om_vite": {
                                "totalt": v.beslut_totalt,
                                "enskild_huvudman": v.beslut_enskild,
                                "offentlig_huvudman": v.beslut_offentlig,
                            },
                            "ansokningar_om_utdomande": {
                                "totalt": v.ansokningar_totalt,
                                "enskild_huvudman": v.ansokningar_enskild,
                                "offentlig_huvudman": v.ansokningar_offentlig,
                            },
                        }
                        for v in viten
                    ],
                    "total_beslut_all_years": sum(v.beslut_totalt for v in summary.viten),
                    "total_ansokningar_all_years": sum(v.ansokningar_totalt for v in summary.viten),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    ]


async def _get_tui_statistik(args: dict) -> list[TextContent]:
    """Get TUI (individual case) / BEO statistics."""
    year = args.get("year")

    summary = await _load_tillsyn_data()
    if not summary:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": "No Tillsyn statistics data available",
                        "hint": "Ensure Tillsyn statistics Excel files are in the data directory",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]

    tui = summary.tui
    if year:
        tui = [t for t in tui if t.year == year]

    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "description": "TUI (Tillsyn Utifrån Individärenden) / BEO statistics. Individual complaint investigations, often related to kränkande behandling (offensive treatment) and special needs support.",
                    "count": len(tui),
                    "years_available": sorted(set(t.year for t in summary.tui), reverse=True),
                    "filter_year": year,
                    "data": [
                        {
                            "year": t.year,
                            "beslut": {
                                "totalt": t.beslut_totalt,
                                "med_brist": t.beslut_med_brist,
                                "andel_med_brist": round(t.andel_med_brist, 1) if t.andel_med_brist else None,
                            },
                            "by_huvudman": {
                                "enskild": {
                                    "totalt": t.beslut_enskild,
                                    "med_brist": t.beslut_enskild_med_brist,
                                },
                                "offentlig": {
                                    "totalt": t.beslut_offentlig,
                                    "med_brist": t.beslut_offentlig_med_brist,
                                },
                            },
                            "by_gender": {
                                "flickor": t.beslut_flickor,
                                "pojkar": t.beslut_pojkar,
                                "ovriga": t.beslut_ovriga,
                            },
                            "by_skolform": t.by_skolform,
                            "brister_by_area": {
                                "krankande_behandling": {
                                    "totalt": t.brister_krankande_behandling,
                                    "elev_elev": t.brister_elev_elev,
                                    "personal_elev": t.brister_personal_elev,
                                },
                                "stod_sarskilt_stod": t.brister_stod,
                                "undervisning": t.brister_undervisning,
                                "ovriga": t.brister_ovriga,
                            },
                        }
                        for t in tui
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    ]


async def _get_planerad_tillsyn_statistik(args: dict) -> list[TextContent]:
    """Get Planerad Tillsyn (planned supervision) statistics."""
    year = args.get("year")

    summary = await _load_tillsyn_data()
    if not summary:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": "No Tillsyn statistics data available",
                        "hint": "Ensure Tillsyn statistics Excel files are in the data directory",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]

    pt = summary.planerad_tillsyn
    if year:
        pt = [p for p in pt if p.year == year]

    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "description": "Planerad Tillsyn (planned supervision) statistics. Regular scheduled inspections of schools.",
                    "count": len(pt),
                    "years_available": sorted(set(p.year for p in summary.planerad_tillsyn), reverse=True),
                    "filter_year": year,
                    "data": [
                        {
                            "year": p.year,
                            "beslut": {
                                "totalt": p.beslut_totalt,
                                "med_brist": p.beslut_med_brist,
                                "andel_med_brist": round(p.andel_med_brist, 1) if p.andel_med_brist else None,
                            },
                            "by_huvudman": {
                                "enskild": {
                                    "totalt": p.beslut_enskild,
                                    "med_brist": p.beslut_enskild_med_brist,
                                },
                                "offentlig": {
                                    "totalt": p.beslut_offentlig,
                                    "med_brist": p.beslut_offentlig_med_brist,
                                },
                            },
                            "by_skolform": p.by_skolform,
                        }
                        for p in pt
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    ]


async def _get_tillsyn_summary(args: dict) -> list[TextContent]:
    """Get combined Tillsyn summary across all categories."""
    summary = await _load_tillsyn_data()
    if not summary:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": "No Tillsyn statistics data available",
                        "hint": "Ensure Tillsyn statistics Excel files are in the data directory",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]

    # Calculate totals across all years
    total_viten_beslut = sum(v.beslut_totalt for v in summary.viten)
    total_viten_ansok = sum(v.ansokningar_totalt for v in summary.viten)
    total_tui_beslut = sum(t.beslut_totalt for t in summary.tui)
    total_tui_med_brist = sum(t.beslut_med_brist for t in summary.tui)
    total_pt_beslut = sum(p.beslut_totalt for p in summary.planerad_tillsyn)
    total_pt_med_brist = sum(p.beslut_med_brist for p in summary.planerad_tillsyn)

    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "description": "Summary of all Tillsyn (supervision) statistics from Skolinspektionen",
                    "years_available": summary.years_available,
                    "viten": {
                        "description": "Fines imposed on school operators for non-compliance",
                        "years_with_data": sorted(set(v.year for v in summary.viten), reverse=True),
                        "total_beslut": total_viten_beslut,
                        "total_ansokningar": total_viten_ansok,
                        "latest_year": summary.viten[0].model_dump() if summary.viten else None,
                    },
                    "tui_beo": {
                        "description": "TUI (individual complaint cases) / BEO investigations",
                        "years_with_data": sorted(set(t.year for t in summary.tui), reverse=True),
                        "total_beslut": total_tui_beslut,
                        "total_med_brist": total_tui_med_brist,
                        "average_brist_rate": round(total_tui_med_brist / total_tui_beslut * 100, 1) if total_tui_beslut > 0 else 0,
                        "latest_year": {
                            "year": summary.tui[0].year,
                            "beslut_totalt": summary.tui[0].beslut_totalt,
                            "beslut_med_brist": summary.tui[0].beslut_med_brist,
                            "andel_med_brist": round(summary.tui[0].andel_med_brist, 1) if summary.tui[0].andel_med_brist else None,
                        } if summary.tui else None,
                    },
                    "planerad_tillsyn": {
                        "description": "Planned regular supervision inspections",
                        "years_with_data": sorted(set(p.year for p in summary.planerad_tillsyn), reverse=True),
                        "total_beslut": total_pt_beslut,
                        "total_med_brist": total_pt_med_brist,
                        "average_brist_rate": round(total_pt_med_brist / total_pt_beslut * 100, 1) if total_pt_beslut > 0 else 0,
                        "latest_year": {
                            "year": summary.planerad_tillsyn[0].year,
                            "beslut_totalt": summary.planerad_tillsyn[0].beslut_totalt,
                            "beslut_med_brist": summary.planerad_tillsyn[0].beslut_med_brist,
                            "andel_med_brist": round(summary.planerad_tillsyn[0].andel_med_brist, 1) if summary.planerad_tillsyn[0].andel_med_brist else None,
                        } if summary.planerad_tillsyn else None,
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    ]


# === OMBEDÖMNING HELPER FUNCTIONS ===


def _handle_ombedomning_reports(args: dict) -> list[TextContent]:
    """Get ombedömning reports, optionally filtered by year."""
    year = args.get("year")

    if year:
        report = get_ombedomning_by_year(year)
        if report:
            reports = [report]
        else:
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "error": f"No ombedömning report found for year {year}",
                            "available_years": [r.year for r in get_ombedomning_reports()],
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
            ]
    else:
        reports = get_ombedomning_reports()

    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "description": "Ombedömning nationella prov - reports on re-evaluation of national tests "
                                   "to assess grading consistency across Sweden",
                    "note": "Data is only available as PDF reports, not structured Excel data",
                    "total_reports": len(reports),
                    "reports": [
                        {
                            "title": r.title,
                            "year": r.year,
                            "test_year": r.test_year,
                            "omgang": r.omgang,
                            "filename": r.filename,
                            "url": r.url,
                            "description": r.description,
                            "subjects": r.subjects,
                            "grades": r.grades,
                        }
                        for r in reports
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    ]


def _handle_ombedomning_summary() -> list[TextContent]:
    """Get summary of all ombedömning reports."""
    summary = get_ombedomning_summary()
    latest = summary.latest_report

    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "description": "Summary of Skolinspektionen's ombedömning nationella prov program",
                    "about": "Ombedömning is when Skolinspektionen re-grades a sample of national tests "
                             "to assess consistency in grading across Sweden. Results typically show "
                             "significant variations in how teachers grade compared to external assessors.",
                    "data_format": "PDF reports only (no structured Excel data available)",
                    "total_reports": summary.total_reports,
                    "years_available": summary.years_available,
                    "subjects_covered": summary.subjects_covered,
                    "latest_report": {
                        "title": latest.title,
                        "year": latest.year,
                        "url": latest.url,
                        "description": latest.description,
                    } if latest else None,
                    "key_findings": [
                        "National tests show significant variation in grading between schools",
                        "Teacher grades often differ from external assessor grades",
                        "Variations persist across subjects and grade levels",
                        "Reports have documented these issues consistently since 2011",
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    ]


# === DATA REFRESH HELPER FUNCTIONS ===


async def _handle_refresh_data(args: dict) -> list[TextContent]:
    """Handle data refresh request."""
    sources = args.get("sources")
    force = args.get("force", False)

    try:
        result = await run_refresh(sources=sources, force=force)

        # Format source results
        source_summaries = {}
        for source_name, source_result in result.sources.items():
            source_summaries[source_name] = {
                "status": source_result.status.value,
                "items_fetched": source_result.items_fetched,
                "items_parsed": source_result.items_parsed,
                "duration_seconds": source_result.duration_seconds,
                "errors": source_result.errors[:5] if source_result.errors else [],
            }

        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "success": result.success,
                        "started_at": result.started_at,
                        "completed_at": result.completed_at,
                        "duration_seconds": result.duration_seconds,
                        "total_items": result.total_items,
                        "total_errors": result.total_errors,
                        "sources": source_summaries,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]

    except Exception as e:
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "success": False,
                        "error": str(e),
                        "hint": "Check network connectivity and try again",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]


def _handle_refresh_status() -> list[TextContent]:
    """Get current refresh status."""
    refresher = DataRefresher()
    status = refresher.get_status()

    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "last_full_refresh": status["last_full_refresh"],
                    "last_incremental_refresh": status["last_incremental_refresh"],
                    "sources": status["sources"],
                    "recent_operations": status["recent_history"],
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    ]


def _handle_list_data_sources() -> list[TextContent]:
    """List all data sources and their descriptions."""
    refresher = DataRefresher()
    status = refresher.get_status()

    sources = {
        "publications": {
            "description": "Publication index scraped from skolinspektionen.se",
            "data_type": "Scraped HTML → JSON index",
            "update_frequency": "Recommended: daily",
            "content": "Reports, decisions, press releases",
        },
        "skolenkaten": {
            "description": "Skolenkäten survey results",
            "data_type": "Excel files (xlsx)",
            "update_frequency": "Twice yearly (after each term)",
            "content": "School satisfaction surveys from students, parents, teachers",
        },
        "tillstand": {
            "description": "Tillståndsbeslut (permit decisions) for independent schools",
            "data_type": "Excel files (xlsx)",
            "update_frequency": "Yearly",
            "content": "Decisions on new schools, expansions, approvals/rejections",
        },
        "tillsyn": {
            "description": "Tillsyn statistics (Viten, TUI/BEO, Planerad Tillsyn)",
            "data_type": "Excel files (xlsx)",
            "update_frequency": "Yearly",
            "content": "Supervision statistics, fines, individual complaints",
        },
        "kolada": {
            "description": "Municipal education statistics from Kolada API",
            "data_type": "REST API → JSON",
            "update_frequency": "Yearly (data released with delay)",
            "content": "KPIs: costs, teacher ratios, student results by municipality",
        },
    }

    # Add current status to each source
    for source_name, source_info in sources.items():
        source_state = status["sources"].get(source_name, {})
        source_info["last_refresh"] = source_state.get("last_refresh")
        source_info["last_status"] = source_state.get("status")
        source_info["items_count"] = source_state.get("items")

    return [
        TextContent(
            type="text",
            text=json.dumps(
                {
                    "description": "Available data sources for Skolinspektionen DATA",
                    "sources": sources,
                    "refresh_command": "Use 'refresh_data' tool to update data",
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    ]


# === RESOURCES ===


@server.list_resources()
async def list_resources() -> list[Resource]:
    """List available resources."""
    return [
        Resource(
            uri="skolinspektionen://publication-types",
            name="Publikationstyper",
            description="Lista över alla publikationstyper från Skolinspektionen",
            mimeType="application/json",
        ),
        Resource(
            uri="skolinspektionen://themes",
            name="Teman",
            description="Lista över alla tillsynsteman",
            mimeType="application/json",
        ),
        Resource(
            uri="skolinspektionen://skolformer",
            name="Skolformer",
            description="Lista över alla skolformer i det svenska skolväsendet",
            mimeType="application/json",
        ),
        Resource(
            uri="skolinspektionen://subjects",
            name="Ämnen",
            description="Lista över alla skolämnen som förekommer i rapporter",
            mimeType="application/json",
        ),
        Resource(
            uri="skolinspektionen://decision-types",
            name="Besluts- och granskningstyper",
            description="Lista över alla besluts- och granskningstyper",
            mimeType="application/json",
        ),
        Resource(
            uri="skolinspektionen://regions",
            name="Regioner",
            description="Lista över Skolinspektionens regioner",
            mimeType="application/json",
        ),
        Resource(
            uri="skolinspektionen://recent",
            name="Senaste publikationer",
            description="De 20 senaste publikationerna",
            mimeType="application/json",
        ),
        # Skolenkäten resources
        Resource(
            uri="skolinspektionen://skolenkaten-respondent-types",
            name="Skolenkäten respondenttyper",
            description="Lista över alla respondenttyper i Skolenkäten (elever, vårdnadshavare, lärare)",
            mimeType="application/json",
        ),
        Resource(
            uri="skolinspektionen://skolenkaten-indices",
            name="Skolenkäten index",
            description="Lista över alla indexområden som mäts i Skolenkäten",
            mimeType="application/json",
        ),
    ]


@server.read_resource()
async def read_resource(uri: str) -> str:
    """Read a resource by URI."""
    if uri == "skolinspektionen://publication-types":
        return json.dumps(
            {"publication_types": PUBLICATION_TYPES},
            ensure_ascii=False,
            indent=2,
        )

    elif uri == "skolinspektionen://themes":
        return json.dumps(
            {"themes": THEMES},
            ensure_ascii=False,
            indent=2,
        )

    elif uri == "skolinspektionen://skolformer":
        return json.dumps(
            {"skolformer": SKOLFORMER},
            ensure_ascii=False,
            indent=2,
        )

    elif uri == "skolinspektionen://subjects":
        return json.dumps(
            {"subjects": SUBJECTS},
            ensure_ascii=False,
            indent=2,
        )

    elif uri == "skolinspektionen://decision-types":
        return json.dumps(
            {"decision_types": DECISION_TYPES},
            ensure_ascii=False,
            indent=2,
        )

    elif uri == "skolinspektionen://regions":
        return json.dumps(
            {"regions": REGIONS},
            ensure_ascii=False,
            indent=2,
        )

    elif uri == "skolinspektionen://recent":
        index = await load_index()
        # Sort by date and get recent
        sorted_pubs = sorted(
            [p for p in index.publications if p.published],
            key=lambda p: p.published,
            reverse=True,
        )[:20]

        return json.dumps(
            {
                "count": len(sorted_pubs),
                "publications": [
                    {
                        "title": p.title,
                        "url": p.url,
                        "type": p.type,
                        "published": p.published.isoformat() if p.published else None,
                    }
                    for p in sorted_pubs
                ],
            },
            ensure_ascii=False,
            indent=2,
        )

    elif uri == "skolinspektionen://skolenkaten-respondent-types":
        return json.dumps(
            {
                "respondent_types": [
                    {"key": k, "name": v}
                    for k, v in SKOLENKATEN_RESPONDENT_TYPES.items()
                ]
            },
            ensure_ascii=False,
            indent=2,
        )

    elif uri == "skolinspektionen://skolenkaten-indices":
        return json.dumps(
            {
                "indices": [
                    {"key": k, "name": v, "scale": "1-10"}
                    for k, v in SKOLENKATEN_INDEX.items()
                ],
                "description": "Index scores range from 1-10, where higher is better",
            },
            ensure_ascii=False,
            indent=2,
        )

    raise ValueError(f"Unknown resource: {uri}")


# === PROMPTS ===


@server.list_prompts()
async def list_prompts() -> list[Prompt]:
    """List available prompts."""
    return [
        Prompt(
            name="summarize_publication",
            description="Get a summary of a Skolinspektionen publication",
            arguments=[
                PromptArgument(
                    name="url",
                    description="URL of the publication to summarize",
                    required=True,
                ),
            ],
        ),
        Prompt(
            name="find_school_decisions",
            description="Find inspection decisions for a specific school or municipality",
            arguments=[
                PromptArgument(
                    name="query",
                    description="School name or municipality to search for",
                    required=True,
                ),
            ],
        ),
        Prompt(
            name="compare_inspections",
            description="Compare inspection results across multiple schools or time periods",
            arguments=[
                PromptArgument(
                    name="theme",
                    description="Inspection theme to compare",
                    required=True,
                ),
                PromptArgument(
                    name="year",
                    description="Year to analyze (optional)",
                    required=False,
                ),
            ],
        ),
        Prompt(
            name="analyze_skolenkaten",
            description="Analyze Skolenkäten survey results for a school or municipality",
            arguments=[
                PromptArgument(
                    name="school",
                    description="School name to analyze",
                    required=True,
                ),
                PromptArgument(
                    name="kommun",
                    description="Municipality (optional)",
                    required=False,
                ),
            ],
        ),
    ]


@server.get_prompt()
async def get_prompt(name: str, arguments: Optional[dict] = None) -> GetPromptResult:
    """Get a prompt by name."""

    if name == "summarize_publication":
        url = arguments.get("url", "") if arguments else ""
        return GetPromptResult(
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(
                        type="text",
                        text=f"""Please summarize the following Skolinspektionen publication.

First, use the get_publication_content tool to fetch the content from: {url}

Then provide:
1. A brief summary (2-3 sentences)
2. Key findings or conclusions
3. Any recommendations made
4. Relevance for educators or policymakers""",
                    ),
                )
            ]
        )

    elif name == "find_school_decisions":
        query = arguments.get("query", "") if arguments else ""
        return GetPromptResult(
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(
                        type="text",
                        text=f"""Find inspection decisions related to: {query}

Use the search_publications tool to find relevant reports and decisions.
Then summarize what inspections have been done and what the findings were.""",
                    ),
                )
            ]
        )

    elif name == "compare_inspections":
        theme = arguments.get("theme", "") if arguments else ""
        year = arguments.get("year", "") if arguments else ""
        return GetPromptResult(
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(
                        type="text",
                        text=f"""Analyze and compare inspection results for theme: {theme}
{f"Year: {year}" if year else ""}

1. Use search_publications to find relevant inspections
2. Identify common findings and patterns
3. Compare results across different schools or time periods
4. Highlight any significant trends or concerns""",
                    ),
                )
            ]
        )

    elif name == "analyze_skolenkaten":
        school = arguments.get("school", "") if arguments else ""
        kommun = arguments.get("kommun", "") if arguments else ""
        return GetPromptResult(
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(
                        type="text",
                        text=f"""Analyze Skolenkäten (school survey) results for: {school}
{f"Municipality: {kommun}" if kommun else ""}

1. Use search_skolenkaten to find survey results for this school
2. Get national averages with get_skolenkaten_summary for comparison
3. Analyze how the school performs compared to national averages
4. Identify strengths (high index scores) and areas for improvement (low scores)
5. Consider multiple respondent types if available (students, parents, teachers)

Key index areas to analyze:
- Trygghet (safety) - Students feeling safe at school
- Studiero (study environment) - Calm learning environment
- Stimulans (motivation) - Academic challenge and engagement
- Bemötande (treatment) - By teachers and by other students
- Inflytande (influence) - Student voice and participation
- Information - About education and expectations""",
                    ),
                )
            ]
        )

    raise ValueError(f"Unknown prompt: {name}")


# === MAIN ===


def create_server() -> Server:
    """Create and return the MCP server instance."""
    return server


async def main():
    """Run the MCP server."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
