#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
=============================================================
                 📺 IPTV EPG GENERATOR
=============================================================

Gera:

    output/epg.xml
    output/epg.xml.gz
    output/playlist.m3u
    output/epg_aliases.json
    output/epg-report.json

ORDEM DE PRIORIDADE:

    1. mi.tv
    2. Guia de TV
    3. ALEPI

FALLBACK:

    O fallback é realizado por CANAL e por DIA.

    Exemplo:

        Canal X / Dia 1
            ↓
        mi.tv
            ↓ falhou
        Guia de TV
            ↓ falhou
        ALEPI

        Canal X / Dia 2
            ↓
        mi.tv
            ↓ falhou
        Guia de TV
            ↓
        resultado encontrado

FONTES:

    mi.tv:
        https://mi.tv/br/programacao
        https://mi.tv/br/canais/{slug}

    Guia de TV:
        https://www.guiadetv.com/busca
        https://www.guiadetv.com
        https://www.guiadetv.com/canais/{slug}

    ALEPI:
        https://www.al.pi.leg.br/
        comunicacao/tv-assembleia/programacao

EPG:

    https://raw.githubusercontent.com/
    josieljefferson/EPG-M3U/
    refs/heads/main/output/epg.xml.gz
=============================================================
"""

from __future__ import annotations

import asyncio
import gzip
import html
import json
import logging
import os
import re
import sys
import traceback
import unicodedata

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from xml.etree import ElementTree as ET


# ============================================================
# CAMINHOS
# ============================================================

ROOT = Path(__file__).resolve().parent.parent

CONFIG_FILE = ROOT / "config" / "channels.json"

OUTPUT_DIR = ROOT / "output"

OUTPUT_FILE = OUTPUT_DIR / "epg.xml"
OUTPUT_GZ_FILE = OUTPUT_DIR / "epg.xml.gz"
PLAYLIST_FILE = OUTPUT_DIR / "playlist.m3u"
ALIASES_FILE = OUTPUT_DIR / "epg_aliases.json"
REPORT_FILE = OUTPUT_DIR / "epg-report.json"

DEBUG_DIR = ROOT / "debug_epg"


# ============================================================
# URL DO EPG
# ============================================================

EPG_URL = (
    "https://raw.githubusercontent.com/"
    "josieljefferson/EPG-M3U/"
    "refs/heads/main/output/epg.xml.gz"
)


# ============================================================
# CONFIGURAÇÕES PADRÃO
# ============================================================

BR_TZ = timezone(timedelta(hours=-3))

DEFAULT_DAYS = 2
DEFAULT_LANGUAGE = "pt-BR"
DEFAULT_TIMEZONE_ID = "America/Sao_Paulo"
DEFAULT_TIMEZONE = "-0300"

MITV_DAYS_AHEAD = int(
    os.getenv("MITV_DAYS_AHEAD", str(DEFAULT_DAYS))
)

MITV_MIN_PROGRAMS = int(
    os.getenv("MITV_MIN_PROGRAMS", "1")
)

MITV_PAGE_TIMEOUT = int(
    os.getenv("MITV_PAGE_TIMEOUT", "60000")
)

MITV_WAIT = int(
    os.getenv("MITV_WAIT", "5000")
)

MITV_RETRIES = int(
    os.getenv("MITV_RETRIES", "3")
)

EPG_MIN_CHANNEL_COVERAGE = float(
    os.getenv("EPG_MIN_CHANNEL_COVERAGE", "0")
)

EPG_MIN_PROGRAM_RATIO = float(
    os.getenv("EPG_MIN_PROGRAM_RATIO", "0")
)

EPG_REQUIRE_ALL_CHANNELS = (
    os.getenv("EPG_REQUIRE_ALL_CHANNELS", "false")
    .strip()
    .lower()
    in {"1", "true", "yes", "sim"}
)


# ============================================================
# REGEX
# ============================================================

TIME_RE = re.compile(
    r"\b([01]?\d|2[0-3])[:h]([0-5]\d)\b",
    re.IGNORECASE,
)

DATE_TIME_RE = re.compile(
    r"""
    (?P<date>\d{1,2}[/-]\d{1,2}[/-]\d{2,4})
    \s*
    (?:-|às|as)?
    \s*
    (?P<time>\d{1,2}[:h]\d{2})
    """,
    re.IGNORECASE | re.VERBOSE,
)


# ============================================================
# LOG
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

LOGGER = logging.getLogger("epg-generator")


# ============================================================
# DATACLASSES
# ============================================================

@dataclass
class Channel:
    id: str
    name: str
    tvg_name: str = ""
    group: str = "Brasil"
    url: str = ""
    logo: str = ""
    aliases: list[str] = field(default_factory=list)
    slug: str = ""
    sources: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.tvg_name:
            self.tvg_name = self.name

        if not self.slug:
            self.slug = slugify(self.name)


@dataclass
class Program:
    channel_id: str
    title: str
    start: datetime
    stop: datetime
    description: str = ""
    category: str = ""
    source: str = ""

    @property
    def duration(self) -> int:
        return max(
            0,
            int((self.stop - self.start).total_seconds()),
        )


@dataclass
class EPGSource:
    id: str
    name: str
    priority: int
    url: str = ""
    urls: list[str] = field(default_factory=list)
    channel_url: str = ""
    categories: list[str] = field(default_factory=list)
    only_channels: list[str] = field(default_factory=list)
    enabled: bool = True


@dataclass
class FetchResult:
    source_id: str
    channel_id: str
    day: date
    programs: list[Program] = field(default_factory=list)
    success: bool = False
    error: str = ""
    url: str = ""

    @property
    def count(self) -> int:
        return len(self.programs)


# ============================================================
# TEXTO
# ============================================================

def clean_text(value: Any) -> str:
    """
    Limpa HTML, espaços duplicados e entidades.
    """

    if value is None:
        return ""

    text = str(value)

    text = html.unescape(text)

    text = re.sub(
        r"<br\s*/?>",
        " ",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r"<[^>]+>",
        " ",
        text,
    )

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


def normalize(value: Any) -> str:
    """
    Normalização usada para comparação de nomes.
    """

    text = clean_text(value)

    text = unicodedata.normalize(
        "NFKD",
        text,
    )

    text = "".join(
        char
        for char in text
        if not unicodedata.combining(char)
    )

    text = text.lower()

    text = re.sub(
        r"[^a-z0-9]+",
        " ",
        text,
    )

    return re.sub(
        r"\s+",
        " ",
        text,
    ).strip()


def slugify(value: Any) -> str:
    text = normalize(value)

    return re.sub(
        r"[^a-z0-9]+",
        "-",
        text,
    ).strip("-")


def xml_escape(value: Any) -> str:
    return html.escape(
        clean_text(value),
        quote=True,
    )


def m3u_attr(value: Any) -> str:
    text = clean_text(value)

    return (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", " ")
        .replace("\n", " ")
    )


# ============================================================
# DATAS
# ============================================================

def local_now() -> datetime:
    return datetime.now(BR_TZ)


def combine_br(
    day: date,
    hour: int,
    minute: int,
) -> datetime:
    return datetime.combine(
        day,
        time(hour, minute),
        tzinfo=BR_TZ,
    )


def parse_time_value(
    value: Any,
) -> Optional[time]:

    if value is None:
        return None

    text = clean_text(value)

    match = TIME_RE.search(text)

    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2))

    if hour > 23 or minute > 59:
        return None

    return time(
        hour,
        minute,
    )


def parse_date_value(
    value: Any,
) -> Optional[date]:

    if value is None:
        return None

    text = clean_text(value)

    formats = (
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%d/%m/%y",
        "%d-%m-%y",
    )

    for fmt in formats:
        try:
            return datetime.strptime(
                text,
                fmt,
            ).date()
        except ValueError:
            continue

    return None


def parse_datetime_value(
    value: Any,
    fallback_day: Optional[date] = None,
) -> Optional[datetime]:

    if value is None:
        return None

    if isinstance(value, datetime):

        if value.tzinfo is None:
            return value.replace(tzinfo=BR_TZ)

        return value.astimezone(BR_TZ)

    text = clean_text(value)

    match = DATE_TIME_RE.search(text)

    if match:

        parsed_date = parse_date_value(
            match.group("date")
        )

        parsed_time = parse_time_value(
            match.group("time")
        )

        if parsed_date and parsed_time:

            return datetime.combine(
                parsed_date,
                parsed_time,
                tzinfo=BR_TZ,
            )

    parsed_time = parse_time_value(text)

    if parsed_time and fallback_day:

        return datetime.combine(
            fallback_day,
            parsed_time,
            tzinfo=BR_TZ,
        )

    formats = (
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%d-%m-%Y %H:%M",
    )

    for fmt in formats:

        try:

            parsed = datetime.strptime(
                text,
                fmt,
            )

            return parsed.replace(
                tzinfo=BR_TZ
            )

        except ValueError:
            continue

    return None


# ============================================================
# FONTES
# ============================================================

def default_sources() -> list[EPGSource]:
    """
    Ordem oficial das fontes:

        1. mi.tv
        2. Guia de TV
        3. ALEPI

    TV Map não faz parte do projeto.
    """

    return [
        EPGSource(
            id="mitv",
            name="mi.tv",
            priority=1,
            url="https://mi.tv/br/programacao",
            channel_url=(
                "https://mi.tv/br/canais/{slug}"
            ),
        ),

        EPGSource(
            id="guiadetv",
            name="Guia de TV",
            priority=2,
            url="https://www.guiadetv.com/busca",
            urls=[
                "https://www.guiadetv.com/busca",
                "https://www.guiadetv.com",
            ],
            channel_url=(
                "https://www.guiadetv.com/canais/{slug}"
            ),
        ),

        EPGSource(
            id="alepi",
            name="ALEPI",
            priority=3,
            url=(
                "https://www.al.pi.leg.br/"
                "comunicacao/tv-assembleia/programacao"
            ),
            only_channels=[
                "tv.assembleia.piaui.br"
            ],
        ),
    ]


# ============================================================
# CONFIGURAÇÃO
# ============================================================

def load_json_file(
    path: Path,
) -> dict[str, Any]:

    if not path.exists():

        LOGGER.warning(
            "Configuração não encontrada: %s",
            path,
        )

        return {}

    try:

        with path.open(
            "r",
            encoding="utf-8",
        ) as file:

            data = json.load(file)

            if isinstance(data, dict):
                return data

    except Exception as exc:

        LOGGER.error(
            "Erro lendo %s: %s",
            path,
            exc,
        )

    return {}


def build_source(
    item: dict[str, Any],
    fallback: EPGSource,
) -> EPGSource:

    source_id = clean_text(
        item.get("id")
        or fallback.id
    ).lower()

    return EPGSource(
        id=source_id,
        name=clean_text(
            item.get("name")
            or fallback.name
        ),
        priority=int(
            item.get(
                "priority",
                fallback.priority,
            )
        ),
        url=clean_text(
            item.get("url")
            or fallback.url
        ),
        urls=[
            clean_text(url)
            for url in item.get(
                "urls",
                fallback.urls,
            )
            if clean_text(url)
        ],
        channel_url=clean_text(
            item.get("channel_url")
            or fallback.channel_url
        ),
        categories=[
            clean_text(value)
            for value in item.get(
                "categories",
                fallback.categories,
            )
        ],
        only_channels=[
            clean_text(value)
            for value in item.get(
                "only_channels",
                fallback.only_channels,
            )
        ],
        enabled=bool(
            item.get(
                "enabled",
                fallback.enabled,
            )
        ),
    )


def load_config() -> tuple[
    dict[str, Any],
    list[Channel],
    list[EPGSource],
]:
    """
    Carrega channels.json.

    As prioridades oficiais são sempre:

        mitv = 1
        guiadetv = 2
        alepi = 3

    Qualquer configuração antiga de TV Map é ignorada.
    """

    data = load_json_file(
        CONFIG_FILE
    )

    settings = data.get(
        "settings",
        {},
    )

    if not isinstance(
        settings,
        dict,
    ):
        settings = {}

    # --------------------------------------------------------
    # Configurações
    # --------------------------------------------------------

    days = int(
        settings.get(
            "days",
            DEFAULT_DAYS,
        )
    )

    language = clean_text(
        settings.get(
            "language",
            DEFAULT_LANGUAGE,
        )
    )

    timezone_id = clean_text(
        settings.get(
            "timezone_id",
            DEFAULT_TIMEZONE_ID,
        )
    )

    timezone_value = clean_text(
        settings.get(
            "timezone",
            DEFAULT_TIMEZONE,
        )
    )

    config = {
        "days": max(1, days),
        "language": language,
        "timezone_id": timezone_id,
        "timezone": timezone_value,
    }

    # --------------------------------------------------------
    # Fontes
    # --------------------------------------------------------

    defaults = {
        source.id: source
        for source in default_sources()
    }

    configured_sources = settings.get(
        "sources",
        [],
    )

    sources: list[EPGSource] = []

    if isinstance(
        configured_sources,
        list,
    ) and configured_sources:

        for item in configured_sources:

            if not isinstance(
                item,
                dict,
            ):
                continue

            source_id = clean_text(
                item.get("id")
            ).lower()

            # TV Map removido
            if source_id == "tvmap":
                LOGGER.warning(
                    "Fonte TV Map encontrada no "
                    "channels.json e ignorada."
                )
                continue

            fallback = defaults.get(
                source_id
            )

            if fallback is None:
                continue

            source = build_source(
                item,
                fallback,
            )

            sources.append(source)

    else:

        sources = list(
            default_sources()
        )

    # --------------------------------------------------------
    # Garantir fontes oficiais
    # --------------------------------------------------------

    existing_ids = {
        source.id
        for source in sources
    }

    for source in default_sources():

        if source.id not in existing_ids:
            sources.append(source)

    # --------------------------------------------------------
    # Prioridades oficiais
    # --------------------------------------------------------

    official_priorities = {
        "mitv": 1,
        "guiadetv": 2,
        "alepi": 3,
    }

    cleaned_sources: list[EPGSource] = []

    for source in sources:

        if source.id == "tvmap":
            continue

        if source.id not in official_priorities:
            continue

        source.priority = official_priorities[
            source.id
        ]

        cleaned_sources.append(source)

    sources = sorted(
        cleaned_sources,
        key=lambda item: (
            item.priority,
            item.id,
        ),
    )

    # --------------------------------------------------------
    # Canais
    # --------------------------------------------------------

    raw_channels = data.get(
        "channels",
        [],
    )

    if isinstance(
        raw_channels,
        dict,
    ):
        raw_channels = list(
            raw_channels.values()
        )

    channels: list[Channel] = []

    if isinstance(
        raw_channels,
        list,
    ):

        for item in raw_channels:

            if not isinstance(
                item,
                dict,
            ):
                continue

            channel_id = clean_text(
                item.get("tvg_id")
                or item.get("id")
                or item.get("channel_id")
            )

            name = clean_text(
                item.get("name")
                or item.get("tvg_name")
                or channel_id
            )

            if not channel_id:
                channel_id = slugify(name)

            aliases_value = item.get(
                "aliases",
                [],
            )

            aliases: list[str] = []

            if isinstance(
                aliases_value,
                dict,
            ):
                aliases.extend(
                    str(key)
                    for key in aliases_value.keys()
                )
                aliases.extend(
                    str(value)
                    for value in aliases_value.values()
                )

            elif isinstance(
                aliases_value,
                list,
            ):
                aliases.extend(
                    str(value)
                    for value in aliases_value
                )

            elif aliases_value:
                aliases.append(
                    str(aliases_value)
                )

            # Nome também é um alias
            aliases.append(name)

            channel = Channel(
                id=channel_id,
                name=name,
                tvg_name=clean_text(
                    item.get(
                        "tvg_name",
                        name,
                    )
                ),
                group=clean_text(
                    item.get(
                        "group",
                        item.get(
                            "group-title",
                            "Brasil",
                        ),
                    )
                ),
                url=clean_text(
                    item.get("url")
                    or item.get("stream")
                    or item.get("stream_url")
                    or item.get("stream-url")
                    or ""
                ),
                logo=clean_text(
                    item.get(
                        "logo",
                        item.get(
                            "tvg_logo",
                            "",
                        ),
                    )
                ),
                aliases=list(
                    dict.fromkeys(
                        aliases
                    )
                ),
                slug=clean_text(
                    item.get(
                        "slug",
                        "",
                    )
                ),
                sources=[
                    clean_text(value)
                    for value in item.get(
                        "sources",
                        [],
                    )
                    if clean_text(value)
                ],
            )

            channels.append(channel)

    return (
        config,
        channels,
        sources,
    )


# ============================================================
# ALIASES / MATCHING
# ============================================================

def text_contains_alias(
    text: str,
    aliases: Iterable[str],
) -> bool:

    normalized_text = normalize(text)

    if not normalized_text:
        return False

    for alias in aliases:

        normalized_alias = normalize(
            alias
        )

        if not normalized_alias:
            continue

        if normalized_alias in normalized_text:
            return True

    return False


def alias_exact(
    text: str,
    aliases: Iterable[str],
) -> bool:

    normalized_text = normalize(text)

    return any(
        normalized_text == normalize(alias)
        for alias in aliases
        if normalize(alias)
    )


def channel_aliases(
    channel: Channel,
) -> list[str]:

    values = [
        channel.id,
        channel.name,
        channel.tvg_name,
        channel.slug,
        *channel.aliases,
    ]

    return list(
        dict.fromkeys(
            clean_text(value)
            for value in values
            if clean_text(value)
        )
    )


def find_channel_block(
    text: str,
    channel: Channel,
) -> bool:

    aliases = channel_aliases(
        channel
    )

    return (
        alias_exact(
            text,
            aliases,
        )
        or text_contains_alias(
            text,
            aliases,
        )
    )


# ============================================================
# NORMALIZAÇÃO DOS PROGRAMAS
# ============================================================

def normalize_programs(
    programs: list[Program],
    channel_id: str,
    source_id: str,
    target_day: date,
) -> list[Program]:

    result: list[Program] = []

    for program in programs:

        if not program.title:
            continue

        start = program.start
        stop = program.stop

        if start.tzinfo is None:
            start = start.replace(
                tzinfo=BR_TZ
            )

        if stop.tzinfo is None:
            stop = stop.replace(
                tzinfo=BR_TZ
            )

        start = start.astimezone(
            BR_TZ
        )

        stop = stop.astimezone(
            BR_TZ
        )

        # Corrige programas que atravessam meia-noite
        if stop <= start:

            candidate = stop + timedelta(
                days=1
            )

            if candidate > start:
                stop = candidate

        if stop <= start:
            continue

        # Programa deve ter relação com o dia solicitado
        if (
            start.date() != target_day
            and stop.date() != target_day
        ):
            continue

        result.append(
            Program(
                channel_id=channel_id,
                title=clean_text(
                    program.title
                ),
                start=start,
                stop=stop,
                description=clean_text(
                    program.description
                ),
                category=clean_text(
                    program.category
                ),
                source=source_id,
            )
        )

    # Ordenação
    result.sort(
        key=lambda item: (
            item.start,
            item.stop,
            normalize(item.title),
        )
    )

    # Remove duplicados
    unique: list[Program] = []
    seen: set[tuple] = set()

    for program in result:

        key = (
            program.start,
            program.stop,
            normalize(program.title),
        )

        if key in seen:
            continue

        seen.add(key)
        unique.append(program)

    return unique


# ============================================================
# PARSER GENÉRICO DE BLOCOS
# ============================================================

def parse_program_block(
    block: dict[str, Any],
    channel_id: str,
    source_id: str,
    target_day: date,
) -> Optional[Program]:

    title = clean_text(
        block.get(
            "title"
            or block.get("name")
        )
    )

    if not title:

        title = clean_text(
            block.get(
                "program"
            )
        )

    description = clean_text(
        block.get(
            "description",
            block.get(
                "desc",
                "",
            ),
        )
    )

    category = clean_text(
        block.get(
            "category",
            block.get(
                "genre",
                "",
            ),
        )
    )

    start = parse_datetime_value(
        block.get(
            "start",
            block.get(
                "start_time",
                block.get(
                    "begin",
                    block.get(
                        "date_start"
                    ),
                ),
            ),
        ),
        target_day,
    )

    stop = parse_datetime_value(
        block.get(
            "stop",
            block.get(
                "end",
                block.get(
                    "end_time",
                    block.get(
                        "date_end"
                    ),
                ),
            ),
        ),
        target_day,
    )

    if start is None:
        return None

    if stop is None:

        duration = block.get(
            "duration",
            60,
        )

        try:
            duration = int(duration)
        except Exception:
            duration = 60

        stop = start + timedelta(
            minutes=max(
                1,
                duration,
            )
        )

    return Program(
        channel_id=channel_id,
        title=title,
        start=start,
        stop=stop,
        description=description,
        category=category,
        source=source_id,
    )


# ============================================================
# PARSER JSON
# ============================================================

def parse_json_programs(
    data: Any,
    channel_id: str,
    source_id: str,
    target_day: date,
) -> list[Program]:

    if isinstance(data, dict):

        for key in (
            "programs",
            "programas",
            "events",
            "schedule",
            "programacao",
            "items",
            "data",
        ):

            if key in data:

                nested = parse_json_programs(
                    data[key],
                    channel_id,
                    source_id,
                    target_day,
                )

                if nested:
                    return nested

        program = parse_program_block(
            data,
            channel_id,
            source_id,
            target_day,
        )

        return (
            [program]
            if program
            else []
        )

    if isinstance(data, list):

        result: list[Program] = []

        for item in data:

            if not isinstance(
                item,
                dict,
            ):
                continue

            program = parse_program_block(
                item,
                channel_id,
                source_id,
                target_day,
            )

            if program:
                result.append(program)

        return normalize_programs(
            result,
            channel_id,
            source_id,
            target_day,
        )

    return []


# ============================================================
# PLAYWRIGHT
# ============================================================

async def create_browser():

    try:

        from playwright.async_api import (
            async_playwright,
        )

    except ImportError as exc:

        raise RuntimeError(
            "Playwright não está instalado. "
            "Instale com: pip install playwright "
            "e execute: playwright install chromium"
        ) from exc

    playwright = await async_playwright().start()

    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ],
    )

    return playwright, browser


async def load_page(
    browser,
    url: str,
    timeout: int = MITV_PAGE_TIMEOUT,
    wait_ms: int = MITV_WAIT,
) -> str:

    page = await browser.new_page(
        locale="pt-BR",
        timezone_id=DEFAULT_TIMEZONE_ID,
    )

    try:

        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=timeout,
        )

        if wait_ms:
            await page.wait_for_timeout(
                wait_ms
            )

        content = await page.content()

        return content

    finally:

        await page.close()


# ============================================================
# PARSER HTML GENÉRICO
# ============================================================

def extract_json_ld(
    html_text: str,
) -> list[Any]:

    result: list[Any] = []

    scripts = re.findall(
        r"""
        <script[^>]*type=["']application/ld\+json["']
        [^>]*>(.*?)</script>
        """,
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    for script in scripts:

        try:

            data = json.loads(
                html.unescape(
                    script
                )
            )

            result.append(data)

        except Exception:
            continue

    return result


def parse_html_time_entries(
    html_text: str,
    channel_id: str,
    source_id: str,
    target_day: date,
) -> list[Program]:

    programs: list[Program] = []

    # --------------------------------------------------------
    # Tenta JSON-LD
    # --------------------------------------------------------

    for data in extract_json_ld(
        html_text
    ):

        parsed = parse_json_programs(
            data,
            channel_id,
            source_id,
            target_day,
        )

        programs.extend(parsed)

    # --------------------------------------------------------
    # Microdata / elementos HTML
    # --------------------------------------------------------

    try:

        from html.parser import HTMLParser

        class ProgramParser(
            HTMLParser
        ):

            def __init__(self):
                super().__init__(
                    convert_charrefs=True
                )

                self.current: dict[str, Any] = {}
                self.stack: list[str] = []
                self.text_parts: list[str] = []
                self.results: list[dict[str, Any]] = []

            def handle_starttag(
                self,
                tag,
                attrs,
            ):

                attrs_dict = dict(attrs)

                self.stack.append(
                    tag
                )

                classes = attrs_dict.get(
                    "class",
                    "",
                )

                class_text = (
                    classes.lower()
                    if classes
                    else ""
                )

                if any(
                    marker in class_text
                    for marker in (
                        "program",
                        "programa",
                        "schedule",
                        "evento",
                    )
                ):

                    self.current = {
                        "start": attrs_dict.get(
                            "data-start"
                        )
                        or attrs_dict.get(
                            "datetime"
                        )
                        or attrs_dict.get(
                            "data-time"
                        ),
                        "stop": attrs_dict.get(
                            "data-stop"
                        )
                        or attrs_dict.get(
                            "data-end"
                        ),
                        "title": attrs_dict.get(
                            "data-title"
                        ),
                        "description": attrs_dict.get(
                            "data-description"
                        ),
                    }

                    self.text_parts = []

            def handle_data(
                self,
                data,
            ):

                if self.current:
                    self.text_parts.append(
                        data
                    )

            def handle_endtag(
                self,
                tag,
            ):

                if (
                    self.current
                    and tag in {
                        "div",
                        "article",
                        "li",
                        "section",
                    }
                ):

                    if not self.current.get(
                        "title"
                    ):

                        text = clean_text(
                            " ".join(
                                self.text_parts
                            )
                        )

                        if text:
                            self.current[
                                "title"
                            ] = text

                    self.results.append(
                        self.current
                    )

                    self.current = {}
                    self.text_parts = []

                if self.stack:
                    self.stack.pop()

        parser = ProgramParser()

        parser.feed(html_text)

        for item in parser.results:

            program = parse_program_block(
                item,
                channel_id,
                source_id,
                target_day,
            )

            if program:
                programs.append(program)

    except Exception:
        pass

    # --------------------------------------------------------
    # Regex de horários
    # --------------------------------------------------------

    if not programs:

        plain = clean_text(
            html_text
        )

        matches = list(
            TIME_RE.finditer(
                plain
            )
        )

        for index, match in enumerate(
            matches
        ):

            start_time = parse_time_value(
                match.group(0)
            )

            if start_time is None:
                continue

            start = datetime.combine(
                target_day,
                start_time,
                tzinfo=BR_TZ,
            )

            if index + 1 < len(matches):

                next_time = parse_time_value(
                    matches[index + 1].group(0)
                )

                if next_time:

                    stop = datetime.combine(
                        target_day,
                        next_time,
                        tzinfo=BR_TZ,
                    )

                    if stop <= start:
                        stop += timedelta(
                            days=1
                        )

                else:
                    stop = start + timedelta(
                        hours=1
                    )

            else:

                stop = start + timedelta(
                    hours=1
                )

            # Tenta pegar texto próximo ao horário
            after = plain[
                match.end():
            ]

            title = clean_text(
                after[:150]
            )

            title = re.split(
                r"\b(?:\d{1,2}[:h]\d{2})\b",
                title,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0]

            if not title:
                title = "Programação"

            programs.append(
                Program(
                    channel_id=channel_id,
                    title=title,
                    start=start,
                    stop=stop,
                    source=source_id,
                )
            )

    return normalize_programs(
        programs,
        channel_id,
        source_id,
        target_day,
    )


# ============================================================
# MI.TV
# ============================================================

def make_mitv_url(
    channel: Channel,
    target_day: date,
    source: EPGSource,
) -> str:

    slug = channel.slug or slugify(
        channel.name
    )

    if source.channel_url:

        return source.channel_url.format(
            slug=slug,
            channel=slug,
            channel_id=channel.id,
        )

    return (
        "https://mi.tv/br/canais/"
        f"{slug}"
    )


async def fetch_mitv(
    browser,
    channel: Channel,
    target_day: date,
    source: EPGSource,
) -> FetchResult:

    result = FetchResult(
        source_id=source.id,
        channel_id=channel.id,
        day=target_day,
    )

    url = make_mitv_url(
        channel,
        target_day,
        source,
    )

    result.url = url

    last_error = ""

    for attempt in range(
        1,
        MITV_RETRIES + 1,
    ):

        try:

            LOGGER.info(
                "[mi.tv] %s | %s | tentativa %s",
                channel.name,
                target_day,
                attempt,
            )

            page_html = await load_page(
                browser,
                url,
            )

            programs = parse_html_time_entries(
                page_html,
                channel.id,
                source.id,
                target_day,
            )

            programs = normalize_programs(
                programs,
                channel.id,
                source.id,
                target_day,
            )

            if len(programs) >= MITV_MIN_PROGRAMS:

                result.programs = programs
                result.success = True

                return result

            last_error = (
                "Nenhuma programação válida "
                f"encontrada ({len(programs)})."
            )

        except Exception as exc:

            last_error = str(exc)

            LOGGER.warning(
                "[mi.tv] erro %s/%s: %s",
                attempt,
                MITV_RETRIES,
                exc,
            )

        if attempt < MITV_RETRIES:

            await asyncio.sleep(
                min(
                    attempt * 2,
                    10,
                )
            )

    result.error = last_error

    return result


# ============================================================
# GUIA DE TV
# ============================================================

def make_guiadetv_urls(
    channel: Channel,
    source: EPGSource,
) -> list[str]:

    urls: list[str] = []

    # --------------------------------------------------------
    # Primeiro tenta página individual
    # --------------------------------------------------------

    slug = channel.slug or slugify(
        channel.name
    )

    if source.channel_url:

        urls.append(
            source.channel_url.format(
                slug=slug,
                channel=slug,
                channel_id=channel.id,
            )
        )

    # --------------------------------------------------------
    # Depois /busca e página principal
    # --------------------------------------------------------

    if source.url:
        urls.append(
            source.url
        )

    for url in source.urls:

        if url not in urls:
            urls.append(url)

    return list(
        dict.fromkeys(
            urls
        )
    )


def parse_guiadetv_page(
    html_text: str,
    channel: Channel,
    source: EPGSource,
    target_day: date,
) -> list[Program]:

    programs = parse_html_time_entries(
        html_text,
        channel.id,
        source.id,
        target_day,
    )

    return normalize_programs(
        programs,
        channel.id,
        source.id,
        target_day,
    )


async def fetch_guiadetv(
    browser,
    channel: Channel,
    target_day: date,
    source: EPGSource,
) -> FetchResult:

    result = FetchResult(
        source_id=source.id,
        channel_id=channel.id,
        day=target_day,
    )

    urls = make_guiadetv_urls(
        channel,
        source,
    )

    errors: list[str] = []

    for url in urls:

        result.url = url

        try:

            LOGGER.info(
                "[Guia de TV] %s | %s | %s",
                channel.name,
                target_day,
                url,
            )

            page_html = await load_page(
                browser,
                url,
                timeout=MITV_PAGE_TIMEOUT,
                wait_ms=MITV_WAIT,
            )

            programs = parse_guiadetv_page(
                page_html,
                channel,
                source,
                target_day,
            )

            if programs:

                result.programs = programs
                result.success = True

                return result

            errors.append(
                f"{url}: nenhum programa"
            )

        except Exception as exc:

            errors.append(
                f"{url}: {exc}"
            )

    result.error = "; ".join(
        errors
    )

    return result


# ============================================================
# ALEPI
# ============================================================

def source_applies(
    source: EPGSource,
    channel: Channel,
) -> bool:

    if not source.enabled:
        return False

    if source.only_channels:

        channel_values = {
            normalize(channel.id),
            normalize(channel.name),
            normalize(channel.tvg_name),
            normalize(channel.slug),
        }

        allowed = {
            normalize(value)
            for value in source.only_channels
        }

        if not channel_values.intersection(
            allowed
        ):
            return False

    if channel.sources:

        allowed_sources = {
            value.lower()
            for value in channel.sources
        }

        if source.id not in allowed_sources:
            return False

    return True


async def fetch_alepi(
    browser,
    channel: Channel,
    target_day: date,
    source: EPGSource,
) -> FetchResult:

    result = FetchResult(
        source_id=source.id,
        channel_id=channel.id,
        day=target_day,
    )

    if not source_applies(
        source,
        channel,
    ):

        result.error = (
            "Fonte não se aplica ao canal."
        )

        return result

    url = source.url

    result.url = url

    try:

        LOGGER.info(
            "[ALEPI] %s | %s",
            channel.name,
            target_day,
        )

        page_html = await load_page(
            browser,
            url,
            timeout=MITV_PAGE_TIMEOUT,
            wait_ms=MITV_WAIT,
        )

        # ----------------------------------------------------
        # Confirma que a página corresponde ao canal
        # ----------------------------------------------------

        if not find_channel_block(
            page_html,
            channel,
        ):

            # ALEPI é específica para TV Assembleia.
            # Quando o source foi explicitamente autorizado
            # pelo only_channels, continua a análise.
            if source.only_channels:
                pass
            else:

                result.error = (
                    "Canal não encontrado na página ALEPI."
                )

                return result

        programs = parse_html_time_entries(
            page_html,
            channel.id,
            source.id,
            target_day,
        )

        programs = normalize_programs(
            programs,
            channel.id,
            source.id,
            target_day,
        )

        if programs:

            result.programs = programs
            result.success = True

            return result

        result.error = (
            "Nenhuma programação ALEPI encontrada."
        )

    except Exception as exc:

        result.error = str(exc)

    return result


# ============================================================
# DISPATCH DAS FONTES
# ============================================================

async def fetch_source(
    browser,
    channel: Channel,
    target_day: date,
    source: EPGSource,
) -> FetchResult:

    if not source_applies(
        source,
        channel,
    ):

        return FetchResult(
            source_id=source.id,
            channel_id=channel.id,
            day=target_day,
            success=False,
            error="Fonte não aplicável.",
        )

    if source.id == "mitv":

        return await fetch_mitv(
            browser,
            channel,
            target_day,
            source,
        )

    if source.id == "guiadetv":

        return await fetch_guiadetv(
            browser,
            channel,
            target_day,
            source,
        )

    if source.id == "alepi":

        return await fetch_alepi(
            browser,
            channel,
            target_day,
            source,
        )

    return FetchResult(
        source_id=source.id,
        channel_id=channel.id,
        day=target_day,
        success=False,
        error=(
            f"Fonte desconhecida: {source.id}"
        ),
    )


# ============================================================
# FETCH DE UM CANAL
# ============================================================

async def fetch_channel(
    browser,
    channel: Channel,
    days: int,
    sources: list[EPGSource],
) -> tuple[
    list[Program],
    list[dict[str, Any]],
]:

    all_programs: list[Program] = []
    attempts: list[dict[str, Any]] = []

    ordered_sources = sorted(
        sources,
        key=lambda item: (
            item.priority,
            item.id,
        ),
    )

    for day_offset in range(
        days
    ):

        target_day = (
            local_now().date()
            + timedelta(
                days=day_offset
            )
        )

        day_success = False

        # ----------------------------------------------------
        # FALLBACK POR DIA
        # ----------------------------------------------------

        for source in ordered_sources:

            if not source_applies(
                source,
                channel,
            ):
                continue

            result = await fetch_source(
                browser,
                channel,
                target_day,
                source,
            )

            attempts.append(
                {
                    "channel_id": channel.id,
                    "channel_name": channel.name,
                    "date": target_day.isoformat(),
                    "source": source.id,
                    "priority": source.priority,
                    "success": result.success,
                    "programs": result.count,
                    "url": result.url,
                    "error": result.error,
                }
            )

            if result.success:

                all_programs.extend(
                    result.programs
                )

                day_success = True

                LOGGER.info(
                    "✅ %s | %s | fonte=%s | "
                    "programas=%s",
                    channel.name,
                    target_day,
                    source.id,
                    result.count,
                )

                # ------------------------------------------------
                # IMPORTANTE:
                # para este canal/dia, para no próximo source.
                # ------------------------------------------------

                break

            LOGGER.info(
                "↪️ %s | %s | %s falhou. "
                "Tentando próxima fonte...",
                channel.name,
                target_day,
                source.id,
            )

        if not day_success:

            LOGGER.warning(
                "❌ Nenhuma fonte encontrou "
                "programação para %s em %s",
                channel.name,
                target_day,
            )

    all_programs = normalize_programs(
        all_programs,
        channel.id,
        "fallback",
        local_now().date(),
    )

    return (
        all_programs,
        attempts,
    )


# ============================================================
# LOGOS
# ============================================================

def update_channel_logo(
    channel: Channel,
    html_text: str,
) -> None:

    if channel.logo:
        return

    patterns = [
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image',
        r'<img[^>]+src=["\']([^"\']+)["\'][^>]+alt=["\'][^"\']*',
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            html_text,
            flags=re.IGNORECASE,
        )

        if match:

            logo = clean_text(
                match.group(1)
            )

            if logo.startswith(
                "http://"
            ) or logo.startswith(
                "https://"
            ):

                channel.logo = logo
                return


# ============================================================
# XMLTV
# ============================================================

def xmltv_datetime(
    value: datetime,
) -> str:

    if value.tzinfo is None:

        value = value.replace(
            tzinfo=BR_TZ
        )

    value = value.astimezone(
        BR_TZ
    )

    offset = value.utcoffset()

    if offset is None:
        offset = timedelta(
            hours=-3
        )

    total_minutes = int(
        offset.total_seconds()
        // 60
    )

    sign = (
        "+"
        if total_minutes >= 0
        else "-"
    )

    total_minutes = abs(
        total_minutes
    )

    hours = total_minutes // 60
    minutes = total_minutes % 60

    return (
        value.strftime(
            "%Y%m%d%H%M%S"
        )
        + f" {sign}{hours:02d}{minutes:02d}"
    )


def generate_xml(
    channels: list[Channel],
    programs: list[Program],
    language: str = DEFAULT_LANGUAGE,
) -> bytes:

    root = ET.Element(
        "tv",
        {
            "generator-info-name":
                "IPTV EPG Generator",
            "generator-info-url":
                EPG_URL,
        },
    )

    # --------------------------------------------------------
    # Canais
    # --------------------------------------------------------

    for channel in channels:

        element = ET.SubElement(
            root,
            "channel",
            {
                "id": channel.id,
            },
        )

        display_name = ET.SubElement(
            element,
            "display-name",
            {
                "lang": language,
            },
        )

        display_name.text = channel.tvg_name

        if channel.logo:

            ET.SubElement(
                element,
                "icon",
                {
                    "src": channel.logo,
                },
            )

    # --------------------------------------------------------
    # Programas
    # --------------------------------------------------------

    ordered_programs = sorted(
        programs,
        key=lambda item: (
            item.start,
            item.stop,
            item.channel_id,
        ),
    )

    for program in ordered_programs:

        element = ET.SubElement(
            root,
            "programme",
            {
                "channel": program.channel_id,
                "start": xmltv_datetime(
                    program.start
                ),
                "stop": xmltv_datetime(
                    program.stop
                ),
            },
        )

        title = ET.SubElement(
            element,
            "title",
            {
                "lang": language,
            },
        )

        title.text = program.title

        if program.description:

            desc = ET.SubElement(
                element,
                "desc",
                {
                    "lang": language,
                },
            )

            desc.text = (
                program.description
            )

        if program.category:

            category = ET.SubElement(
                element,
                "category",
                {
                    "lang": language,
                },
            )

            category.text = (
                program.category
            )

    ET.indent(
        root,
        space="  ",
    )

    xml_data = ET.tostring(
        root,
        encoding="utf-8",
        xml_declaration=True,
    )

    return xml_data


# ============================================================
# GZIP
# ============================================================

def gzip_xml(
    xml_data: bytes,
) -> bytes:

    return gzip.compress(
        xml_data,
        compresslevel=9,
        mtime=0,
    )


# ============================================================
# M3U
# ============================================================

def generate_m3u(
    channels: list[Channel],
) -> str:

    lines = [
        f'#EXTM3U url-tvg="{EPG_URL}"'
    ]

    playable = 0

    for channel in channels:

        url = clean_text(channel.url)

        if not url:
            continue

        if not re.match(r"^https?://\S+$", url, re.IGNORECASE):
            LOGGER.warning(
                "Canal ignorado por URL inválida: %s -> %s",
                channel.name,
                url,
            )
            continue

        attributes = [
            f'tvg-id="{m3u_attr(channel.id)}"',
            f'tvg-name="{m3u_attr(channel.tvg_name)}"',
        ]

        if channel.logo:

            attributes.append(
                f'tvg-logo="{m3u_attr(channel.logo)}"'
            )

        if channel.group:

            attributes.append(
                f'group-title="{m3u_attr(channel.group)}"'
            )

        lines.append(
            "#EXTINF:-1 "
            + " ".join(attributes)
            + ","
            + m3u_attr(channel.name)
        )

        lines.append(
            url
        )
        playable += 1

    if playable < 1:
        raise RuntimeError(
            "Nenhum canal com URL de streaming válida foi encontrado para a playlist M3U."
        )

    LOGGER.info(
        "📺 Playlist M3U: %d canal(is) com stream válido.",
        playable,
    )

    return (
        "\n".join(lines)
        + "\n"
    )


# ============================================================
# ALIASES
# ============================================================

def generate_aliases(
    channels: list[Channel],
) -> dict[str, Any]:

    aliases: dict[str, Any] = {}

    for channel in channels:

        values = channel_aliases(
            channel
        )

        aliases[channel.id] = {
            "name": channel.name,
            "tvg_name": channel.tvg_name,
            "aliases": values,
        }

    return aliases


# ============================================================
# ESCRITA ATÔMICA
# ============================================================

def atomic_write(
    path: Path,
    data: bytes,
) -> None:

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary = path.with_suffix(
        path.suffix + ".tmp"
    )

    temporary.write_bytes(
        data
    )

    temporary.replace(
        path
    )


def atomic_write_text(
    path: Path,
    text: str,
) -> None:

    atomic_write(
        path,
        text.encode("utf-8"),
    )


def write_m3u_atomic(
    content: str,
) -> None:

    expected_header = (
        f'#EXTM3U url-tvg="{EPG_URL}"'
    )

    if not content.startswith(
        expected_header
    ):

        raise RuntimeError(
            "A playlist M3U não possui "
            "o cabeçalho EPG esperado."
        )

    entries = sum(
        1
        for line in content.splitlines()
        if line.startswith("#EXTINF:")
    )

    if entries < 1:
        raise RuntimeError(
            "A playlist M3U não possui nenhuma entrada #EXTINF."
        )

    atomic_write_text(
        PLAYLIST_FILE,
        content,
    )

    if not PLAYLIST_FILE.is_file() or PLAYLIST_FILE.stat().st_size == 0:
        raise RuntimeError(
            "playlist.m3u não foi gravada corretamente."
        )


# ============================================================
# RELATÓRIO
# ============================================================

def create_report(
    config: dict[str, Any],
    channels: list[Channel],
    programs: list[Program],
    attempts: list[dict[str, Any]],
    sources: list[EPGSource],
) -> dict[str, Any]:

    by_source: dict[str, int] = {}

    for program in programs:

        by_source[
            program.source
        ] = (
            by_source.get(
                program.source,
                0,
            )
            + 1
        )

    channel_program_count: dict[
        str,
        int,
    ] = {}

    for program in programs:

        channel_program_count[
            program.channel_id
        ] = (
            channel_program_count.get(
                program.channel_id,
                0,
            )
            + 1
        )

    successful_attempts = sum(
        1
        for item in attempts
        if item.get("success")
    )

    report = {
        "generated_at": local_now().isoformat(),
        "epg_url": EPG_URL,
        "settings": config,
        "sources": [
            {
                "id": source.id,
                "name": source.name,
                "priority": source.priority,
                "url": source.url,
                "urls": source.urls,
                "channel_url": source.channel_url,
                "enabled": source.enabled,
            }
            for source in sources
        ],
        "source_priority": [
            source.id
            for source in sorted(
                sources,
                key=lambda item: item.priority,
            )
        ],
        "channels": {
            "configured": len(channels),
            "with_programs": sum(
                1
                for channel in channels
                if channel_program_count.get(
                    channel.id,
                    0,
                )
                > 0
            ),
        },
        "programs": {
            "total": len(programs),
            "by_source": by_source,
            "by_channel": channel_program_count,
        },
        "attempts": {
            "total": len(attempts),
            "successful": successful_attempts,
            "failed": (
                len(attempts)
                - successful_attempts
            ),
        },
        "files": {
            "epg": str(
                OUTPUT_FILE.relative_to(ROOT)
            ),
            "epg_gz": str(
                OUTPUT_GZ_FILE.relative_to(ROOT)
            ),
            "playlist": str(
                PLAYLIST_FILE.relative_to(ROOT)
            ),
            "aliases": str(
                ALIASES_FILE.relative_to(ROOT)
            ),
            "report": str(
                REPORT_FILE.relative_to(ROOT)
            ),
        },
        "attempt_details": attempts,
    }

    return report


def write_report(
    report: dict[str, Any],
) -> None:

    atomic_write_text(
        REPORT_FILE,
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
        ),
    )


# ============================================================
# VALIDAÇÃO
# ============================================================

def validate_xml(
    xml_data: bytes,
) -> tuple[int, int]:

    root = ET.fromstring(
        xml_data
    )

    if root.tag != "tv":
        raise RuntimeError(
            "XMLTV inválido: raiz não é <tv>."
        )

    channels = root.findall(
        "channel"
    )

    programmes = root.findall(
        "programme"
    )

    if not channels:
        raise RuntimeError(
            "XMLTV inválido: nenhum canal."
        )

    if not programmes:
        raise RuntimeError(
            "XMLTV inválido: nenhum programa."
        )

    return (
        len(channels),
        len(programmes),
    )


def validate_outputs(
    channels: list[Channel],
    programs: list[Program],
) -> None:

    if not OUTPUT_FILE.exists():
        raise RuntimeError(
            "epg.xml não foi gerado."
        )

    if not OUTPUT_GZ_FILE.exists():
        raise RuntimeError(
            "epg.xml.gz não foi gerado."
        )

    if not PLAYLIST_FILE.exists():
        raise RuntimeError(
            "playlist.m3u não foi gerada."
        )

    if not ALIASES_FILE.exists():
        raise RuntimeError(
            "epg_aliases.json não foi gerado."
        )

    if not REPORT_FILE.exists():
        raise RuntimeError(
            "epg-report.json não foi gerado."
        )

    if not channels:
        raise RuntimeError(
            "Nenhum canal foi carregado."
        )

    if PLAYLIST_FILE.stat().st_size == 0:
        raise RuntimeError(
            "playlist.m3u está vazia."
        )

    playlist_entries = sum(
        1
        for line in PLAYLIST_FILE.read_text(encoding="utf-8").splitlines()
        if line.startswith("#EXTINF:")
    )

    if playlist_entries < 1:
        raise RuntimeError(
            "playlist.m3u não contém nenhum canal (#EXTINF)."
        )

    if not programs:
        raise RuntimeError(
            "Nenhum programa foi encontrado."
        )

    playlist = PLAYLIST_FILE.read_text(
        encoding="utf-8"
    )

    expected_header = (
        f'#EXTM3U url-tvg="{EPG_URL}"'
    )

    if not playlist.startswith(
        expected_header
    ):

        raise RuntimeError(
            "Cabeçalho da playlist inválido."
        )

    with gzip.open(
        OUTPUT_GZ_FILE,
        "rb",
    ) as file:

        decompressed = file.read()

    ET.fromstring(
        decompressed
    )


# ============================================================
# DEBUG
# ============================================================

def save_debug(
    name: str,
    content: str,
) -> None:

    try:

        DEBUG_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

        safe_name = re.sub(
            r"[^a-zA-Z0-9._-]+",
            "_",
            name,
        )

        path = (
            DEBUG_DIR
            / safe_name
        )

        path.write_text(
            content,
            encoding="utf-8",
        )

    except Exception:
        pass


# ============================================================
# EXECUÇÃO PRINCIPAL
# ============================================================

async def async_main() -> int:

    LOGGER.info(
        "===================================================="
    )

    LOGGER.info(
        "📺 IPTV EPG GENERATOR"
    )

    LOGGER.info(
        "===================================================="
    )

    # --------------------------------------------------------
    # Configuração
    # --------------------------------------------------------

    config, channels, sources = load_config()

    days = int(
        config.get(
            "days",
            DEFAULT_DAYS,
        )
    )

    LOGGER.info(
        "Dias configurados: %s",
        days,
    )

    LOGGER.info(
        "Canais carregados: %s",
        len(channels),
    )

    LOGGER.info(
        "Fontes:"
    )

    for source in sources:

        LOGGER.info(
            "  %s. %s (%s)",
            source.priority,
            source.name,
            source.id,
        )

    # --------------------------------------------------------
    # Verificação de prioridade
    # --------------------------------------------------------

    expected_order = [
        "mitv",
        "guiadetv",
        "alepi",
    ]

    actual_order = [
        source.id
        for source in sources
    ]

    if actual_order != expected_order:

        raise RuntimeError(
            "Ordem de fontes inválida. "
            f"Esperado: {expected_order}; "
            f"obtido: {actual_order}"
        )

    # --------------------------------------------------------
    # Diretórios
    # --------------------------------------------------------

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    # --------------------------------------------------------
    # Browser
    # --------------------------------------------------------

    playwright = None
    browser = None

    all_programs: list[Program] = []
    all_attempts: list[dict[str, Any]] = []

    try:

        playwright, browser = (
            await create_browser()
        )

        # ----------------------------------------------------
        # Processamento sequencial
        # ----------------------------------------------------

        for index, channel in enumerate(
            channels,
            start=1,
        ):

            LOGGER.info(
                ""
            )

            LOGGER.info(
                "📡 [%s/%s] %s",
                index,
                len(channels),
                channel.name,
            )

            try:

                programs, attempts = (
                    await fetch_channel(
                        browser,
                        channel,
                        days,
                        sources,
                    )
                )

                all_programs.extend(
                    programs
                )

                all_attempts.extend(
                    attempts
                )

                LOGGER.info(
                    "📊 %s: %s programas",
                    channel.name,
                    len(programs),
                )

            except Exception as exc:

                LOGGER.error(
                    "Erro processando %s: %s",
                    channel.name,
                    exc,
                )

                LOGGER.debug(
                    traceback.format_exc()
                )

    finally:

        if browser is not None:

            try:
                await browser.close()
            except Exception:
                pass

        if playwright is not None:

            try:
                await playwright.stop()
            except Exception:
                pass

    # --------------------------------------------------------
    # Remoção de duplicados globais
    # --------------------------------------------------------

    unique_programs: list[Program] = []

    seen_programs: set[
        tuple
    ] = set()

    for program in sorted(
        all_programs,
        key=lambda item: (
            item.channel_id,
            item.start,
            item.stop,
            normalize(item.title),
        ),
    ):

        key = (
            program.channel_id,
            program.start,
            program.stop,
            normalize(program.title),
        )

        if key in seen_programs:
            continue

        seen_programs.add(
            key
        )

        unique_programs.append(
            program
        )

    all_programs = unique_programs

    # --------------------------------------------------------
    # Validação mínima
    # --------------------------------------------------------

    configured_channels = len(
        channels
    )

    channels_with_programs = len(
        {
            program.channel_id
            for program in all_programs
        }
    )

    coverage = (
        (
            channels_with_programs
            / configured_channels
        )
        if configured_channels
        else 0
    )

    LOGGER.info(
        "Cobertura EPG: %.2f%%",
        coverage * 100,
    )

    if (
        EPG_REQUIRE_ALL_CHANNELS
        and channels_with_programs
        < configured_channels
    ):

        raise RuntimeError(
            "Nem todos os canais possuem EPG."
        )

    if coverage < EPG_MIN_CHANNEL_COVERAGE:

        raise RuntimeError(
            "Cobertura abaixo do mínimo: "
            f"{coverage:.2%} < "
            f"{EPG_MIN_CHANNEL_COVERAGE:.2%}"
        )

    # --------------------------------------------------------
    # XML
    # --------------------------------------------------------

    LOGGER.info(
        "📝 Gerando XMLTV..."
    )

    xml_data = generate_xml(
        channels,
        all_programs,
        config.get(
            "language",
            DEFAULT_LANGUAGE,
        ),
    )

    xml_channels, xml_programmes = (
        validate_xml(
            xml_data
        )
    )

    LOGGER.info(
        "XMLTV: %s canais / %s programas",
        xml_channels,
        xml_programmes,
    )

    atomic_write(
        OUTPUT_FILE,
        xml_data,
    )

    # --------------------------------------------------------
    # GZIP
    # --------------------------------------------------------

    LOGGER.info(
        "🗜️ Gerando epg.xml.gz..."
    )

    gz_data = gzip_xml(
        xml_data
    )

    atomic_write(
        OUTPUT_GZ_FILE,
        gz_data,
    )

    # --------------------------------------------------------
    # Playlist
    # --------------------------------------------------------

    LOGGER.info(
        "📺 Gerando playlist.m3u..."
    )

    playlist = generate_m3u(
        channels
    )

    write_m3u_atomic(
        playlist
    )

    # --------------------------------------------------------
    # Aliases
    # --------------------------------------------------------

    LOGGER.info(
        "🔗 Gerando epg_aliases.json..."
    )

    aliases = generate_aliases(
        channels
    )

    atomic_write_text(
        ALIASES_FILE,
        json.dumps(
            aliases,
            ensure_ascii=False,
            indent=2,
        ),
    )

    # --------------------------------------------------------
    # Relatório
    # --------------------------------------------------------

    LOGGER.info(
        "📊 Gerando epg-report.json..."
    )

    report = create_report(
        config,
        channels,
        all_programs,
        all_attempts,
        sources,
    )

    write_report(
        report
    )

    # --------------------------------------------------------
    # Validação final
    # --------------------------------------------------------

    validate_outputs(
        channels,
        all_programs,
    )

    # --------------------------------------------------------
    # Resumo
    # --------------------------------------------------------

    LOGGER.info(
        ""
    )

    LOGGER.info(
        "===================================================="
    )

    LOGGER.info(
        "✅ EPG GERADO COM SUCESSO"
    )

    LOGGER.info(
        "===================================================="
    )

    LOGGER.info(
        "Canais:       %s",
        len(channels),
    )

    LOGGER.info(
        "Com EPG:      %s",
        channels_with_programs,
    )

    LOGGER.info(
        "Programas:    %s",
        len(all_programs),
    )

    LOGGER.info(
        "EPG XML:      %s",
        OUTPUT_FILE,
    )

    LOGGER.info(
        "EPG GZIP:     %s",
        OUTPUT_GZ_FILE,
    )

    LOGGER.info(
        "Playlist:     %s",
        PLAYLIST_FILE,
    )

    LOGGER.info(
        "Aliases:      %s",
        ALIASES_FILE,
    )

    LOGGER.info(
        "Relatório:    %s",
        REPORT_FILE,
    )

    LOGGER.info(
        "URL EPG:      %s",
        EPG_URL,
    )

    LOGGER.info(
        "Prioridade:   mi.tv → Guia de TV → ALEPI"
    )

    LOGGER.info(
        "===================================================="
    )

    return 0


# ============================================================
# MAIN
# ============================================================

def main() -> int:

    try:

        return asyncio.run(
            async_main()
        )

    except KeyboardInterrupt:

        LOGGER.warning(
            "Execução interrompida."
        )

        return 130

    except Exception as exc:

        LOGGER.error(
            "❌ ERRO FATAL: %s",
            exc,
        )

        LOGGER.debug(
            traceback.format_exc()
        )

        return 1


if __name__ == "__main__":
    sys.exit(
        main()
    )
