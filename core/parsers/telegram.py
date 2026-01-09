import re
from datetime import datetime
from typing import ClassVar

from aiohttp import ClientError
from bs4 import BeautifulSoup, Tag

from astrbot.core.config.astrbot_config import AstrBotConfig

from ..data import ParseResult, Platform
from ..download import Downloader
from ..exception import ParseException
from .base import BaseParser, handle

_STYLE_URL_RE = re.compile(r"url\(['\"]?([^'\")]+)['\"]?\)")


class TelegramParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="telegram", display_name="Telegram")

    def __init__(self, config: AstrBotConfig, downloader: Downloader):
        super().__init__(config, downloader)

    @handle(
        "t.me/c",
        r"https?://t\.me/c/(?P<channel_id>\d+)/(?P<msg_id>\d+)(?:[/?#].*)?",
    )
    async def _parse_private(self, searched: re.Match[str]) -> ParseResult:
        url = searched.group(0)
        channel_id = searched.group("channel_id")
        msg_id = searched.group("msg_id")
        fetch_url = f"https://t.me/c/{channel_id}/{msg_id}"
        return await self._parse_message(fetch_url, url, msg_id)

    @handle(
        "t.me",
        r"https?://t\.me/(?:s/)?(?P<peer>[^/?#]+)/(?P<msg_id>\d+)(?:[/?#].*)?",
    )
    @handle(
        "telegram.me",
        r"https?://telegram\.me/(?:s/)?(?P<peer>[^/?#]+)/(?P<msg_id>\d+)(?:[/?#].*)?",
    )
    async def _parse(self, searched: re.Match[str]) -> ParseResult:
        url = searched.group(0)
        peer = searched.group("peer")
        msg_id = searched.group("msg_id")
        fetch_url = url if "/s/" in url else f"https://t.me/s/{peer}/{msg_id}"
        return await self._parse_message(fetch_url, url, msg_id)

    async def _parse_message(
        self,
        fetch_url: str,
        source_url: str,
        msg_id: str,
    ) -> ParseResult:
        html = await self._fetch_html(fetch_url)
        soup = BeautifulSoup(html, "html.parser")

        message = self._find_message(soup, msg_id)
        text = self._extract_message_text(message)
        author_name = self._extract_author_name(message, soup)
        timestamp = self._extract_timestamp(message)

        video_url, cover_url = self._extract_video(message)
        image_urls = self._extract_images(message)

        meta_images = self._get_meta_values(
            soup, {"og:image", "twitter:image", "twitter:image:src"}
        )
        meta_video = self._get_meta_values(
            soup, {"og:video", "og:video:url", "og:video:secure_url"}
        )
        meta_desc = self._get_meta_values(soup, {"og:description", "description"})

        if not text and meta_desc:
            text = meta_desc[0]
        if not image_urls and meta_images:
            image_urls = meta_images
        if not video_url and meta_video:
            video_url = meta_video[0]

        image_urls = self._dedupe_urls(image_urls)

        if not any([text, video_url, image_urls]):
            raise ParseException("无法解析 Telegram 内容")

        contents = []
        if video_url:
            contents.append(self.create_video_content(video_url, cover_url))
        elif image_urls:
            contents.extend(self.create_image_contents(image_urls))

        author = self.create_author(author_name) if author_name else None

        return self.result(
            url=source_url,
            author=author,
            text=text,
            timestamp=timestamp,
            contents=contents,
        )

    async def _fetch_html(self, url: str) -> str:
        async with self.client.get(
            url, headers=self.headers, allow_redirects=True, proxy=self.proxy
        ) as resp:
            if resp.status >= 400:
                raise ClientError(f"telegram {resp.status} {resp.reason}")
            return await resp.text()

    @staticmethod
    def _find_message(soup: BeautifulSoup, msg_id: str) -> Tag | None:
        candidates = soup.select("div.tgme_widget_message")
        for msg in candidates:
            data_post = msg.get("data-post")
            if isinstance(data_post, str) and data_post.endswith(f"/{msg_id}"):
                return msg
        return candidates[0] if candidates else None

    @staticmethod
    def _extract_message_text(message: Tag | None) -> str | None:
        if not message:
            return None
        text_tag = message.find("div", class_="tgme_widget_message_text")
        if not isinstance(text_tag, Tag):
            return None
        text = text_tag.get_text("\n", strip=True)
        return text or None

    @staticmethod
    def _extract_author_name(message: Tag | None, soup: BeautifulSoup) -> str | None:
        if message:
            owner = message.find("a", class_="tgme_widget_message_owner_name")
            if isinstance(owner, Tag):
                name = owner.get_text(strip=True)
                if name:
                    return name
        header = soup.find("div", class_="tgme_channel_info_header_title")
        if isinstance(header, Tag):
            name = header.get_text(strip=True)
            if name:
                return name
        page_title = soup.find("div", class_="tgme_page_title")
        if isinstance(page_title, Tag):
            name = page_title.get_text(strip=True)
            if name:
                return name
        return None

    @staticmethod
    def _extract_timestamp(message: Tag | None) -> int | None:
        if not message:
            return None
        time_tag = message.find("time")
        if not isinstance(time_tag, Tag):
            return None
        dt_value = time_tag.get("datetime")
        if not dt_value:
            return None
        if isinstance(dt_value, str):
            try:
                dt_value = dt_value.replace("Z", "+00:00")
                return int(datetime.fromisoformat(dt_value).timestamp())
            except ValueError:
                return None
        return None

    @classmethod
    def _extract_images(cls, message: Tag | None) -> list[str]:
        if not message:
            return []
        urls: list[str] = []
        for tag in message.find_all("a", class_="tgme_widget_message_photo_wrap"):
            style = tag.get("style")
            if isinstance(style, str):
                if url := cls._extract_style_url(style):
                    urls.append(url)
        for img in message.find_all("img"):
            src = img.get("src")
            if isinstance(src, str) and src.startswith("http"):
                urls.append(src)
        return urls

    @classmethod
    def _extract_video(cls, message: Tag | None) -> tuple[str | None, str | None]:
        if not message:
            return None, None
        cover_url = None
        video_wrap = message.find("a", class_="tgme_widget_message_video_wrap")
        if isinstance(video_wrap, Tag):
            data_video = video_wrap.get("data-video")
            if isinstance(data_video, str):
                cover_url = cls._extract_style_url(video_wrap.get("style"))
                return data_video, cover_url
            href = video_wrap.get("href")
            if isinstance(href, str) and ".mp4" in href:
                cover_url = cls._extract_style_url(video_wrap.get("style"))
                return href, cover_url

        video_tag = message.find("video")
        if isinstance(video_tag, Tag):
            src = video_tag.get("src")
            if not src:
                source = video_tag.find("source")
                if isinstance(source, Tag):
                    src = source.get("src")
            if isinstance(src, str):
                cover_url = video_tag.get("poster")
                return src, cover_url
        return None, None

    @staticmethod
    def _extract_style_url(style: str | None) -> str | None:
        if not style:
            return None
        match = _STYLE_URL_RE.search(style)
        if not match:
            return None
        return match.group(1)

    @staticmethod
    def _get_meta_values(soup: BeautifulSoup, keys: set[str]) -> list[str]:
        values: list[str] = []
        for meta in soup.find_all("meta"):
            if not isinstance(meta, Tag):
                continue
            key = meta.get("property") or meta.get("name")
            if key not in keys:
                continue
            content = meta.get("content")
            if isinstance(content, str) and content:
                values.append(content)
        return values

    @staticmethod
    def _dedupe_urls(urls: list[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for url in urls:
            if url in seen:
                continue
            seen.add(url)
            ordered.append(url)
        return ordered
