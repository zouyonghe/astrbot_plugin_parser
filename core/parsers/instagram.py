import asyncio
import re
from typing import Any, ClassVar

import yt_dlp

from astrbot.core.config.astrbot_config import AstrBotConfig

from ..data import ParseResult, Platform
from ..download import Downloader
from ..exception import ParseException
from .base import BaseParser, handle


class InstagramParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="instagram", display_name="Instagram")

    def __init__(self, config: AstrBotConfig, downloader: Downloader):
        super().__init__(config, downloader)

    @handle(
        "instagram.com",
        r"https?://(?:www\.)?instagram\.com/(?P<kind>p|reel|tv)/(?P<shortcode>[^/?#]+)/?(?:[?#].*)?",
    )
    @handle(
        "instagr.am",
        r"https?://(?:www\.)?instagr\.am/(?P<kind>p|reel|tv)/(?P<shortcode>[^/?#]+)/?(?:[?#].*)?",
    )
    async def _parse(self, searched: re.Match[str]) -> ParseResult:
        kind = searched.group("kind")
        shortcode = searched.group("shortcode")
        page_url = f"https://www.instagram.com/{kind}/{shortcode}/"
        result = await self._parse_from_ytdlp(page_url)
        if result is None:
            raise ParseException("instagram content not accessible")
        return result

    async def _parse_from_ytdlp(self, page_url: str) -> ParseResult | None:
        info = await self._fetch_ytdlp_info(page_url)
        if not info:
            return None

        ext_headers = {**self.headers, "Referer": "https://www.instagram.com/"}
        contents = self._extract_ytdlp_contents(info, ext_headers)
        if not contents:
            return None

        author_name = self._first_text(
            info.get("uploader"),
            info.get("uploader_id"),
            info.get("channel"),
        )
        text = self._first_text(info.get("description"), info.get("title"))
        timestamp = info.get("timestamp")
        timestamp = int(timestamp) if isinstance(timestamp, (int, float)) else None

        author = self.create_author(author_name) if author_name else None
        return self.result(
            url=page_url,
            author=author,
            text=text,
            timestamp=timestamp,
            contents=contents,
        )

    async def _fetch_ytdlp_info(self, url: str) -> dict[str, Any] | None:
        opts = {
            "quiet": True,
            "skip_download": True,
            "force_generic_extractor": True,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                raw = await asyncio.to_thread(ydl.extract_info, url, download=False)
        except Exception:
            return None
        return raw if isinstance(raw, dict) else None

    @staticmethod
    def _first_text(*values: Any) -> str | None:
        for value in values:
            if isinstance(value, str) and value:
                return value
        return None

    def _extract_ytdlp_contents(
        self, info: dict[str, Any], ext_headers: dict[str, str]
    ) -> list[Any]:
        contents: list[Any] = []
        entries = info.get("entries")
        if isinstance(entries, list) and entries:
            for entry in entries:
                if isinstance(entry, dict):
                    contents.extend(self._entry_to_contents(entry, ext_headers))
            return contents
        return self._entry_to_contents(info, ext_headers)

    def _entry_to_contents(
        self, entry: dict[str, Any], ext_headers: dict[str, str]
    ) -> list[Any]:
        contents: list[Any] = []
        video_url = self._extract_video_url(entry)
        duration = entry.get("duration")
        duration_value = (
            float(duration) if isinstance(duration, (int, float)) else 0.0
        )
        if video_url:
            contents.append(
                self.create_video_content(
                    video_url,
                    cover_url=entry.get("thumbnail"),
                    duration=duration_value,
                    ext_headers=ext_headers,
                )
            )
            return contents

        image_url = self._extract_image_url(entry)
        if image_url:
            contents.extend(self.create_image_contents([image_url], ext_headers))
        return contents

    @staticmethod
    def _extract_image_url(entry: dict[str, Any]) -> str | None:
        url = entry.get("url")
        ext = entry.get("ext")
        if isinstance(url, str) and url:
            if isinstance(ext, str) and ext.lower() in {"jpg", "jpeg", "png", "webp"}:
                return url
        thumb = entry.get("thumbnail")
        return thumb if isinstance(thumb, str) and thumb else None

    def _extract_video_url(self, entry: dict[str, Any]) -> str | None:
        url = entry.get("url")
        vcodec = entry.get("vcodec")
        if isinstance(url, str) and url and vcodec not in ("none", None):
            return url
        return self._best_video_format_url(entry.get("formats"))

    @staticmethod
    def _best_video_format_url(formats: Any) -> str | None:
        if not isinstance(formats, list):
            return None
        candidates = []
        for fmt in formats:
            if not isinstance(fmt, dict):
                continue
            url = fmt.get("url")
            if not isinstance(url, str) or not url:
                continue
            vcodec = fmt.get("vcodec")
            if vcodec in (None, "none"):
                continue
            candidates.append(fmt)
        if not candidates:
            return None
        prefer = [
            fmt
            for fmt in candidates
            if isinstance(fmt.get("height"), int) and fmt["height"] <= 720
        ]
        target = prefer if prefer else candidates

        def sort_key(fmt: dict[str, Any]) -> int:
            height = fmt.get("height")
            return int(height) if isinstance(height, int) else 0

        best = max(target, key=sort_key)
        return best.get("url")
