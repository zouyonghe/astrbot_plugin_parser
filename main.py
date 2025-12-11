# main.py

import re
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import Image, Record
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .core.clean import CacheCleaner
from .core.download import Downloader
from .core.parsers import BaseParser, BilibiliParser, ParseResult, YouTubeParser
from .core.render import CommonRenderer
from .core.utils import save_cookies_with_netscape


@register("astrbot_plugin_parser", "Zhalslar", "...", "...")
class ParserPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config

        # 插件数据目录
        self.data_dir: Path = StarTools.get_data_dir("astrbot_plugin_parser")
        config["data_dir"] = str(self.data_dir)

        # 缓存目录
        self.cache_dir: Path = self.data_dir / "cache_dir"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        config["cache_dir"] = str(self.cache_dir)

        # ytb_cookies
        if self.config["ytb_ck"]:
            ytb_cookies_file = self.data_dir / "ytb_cookies.txt"
            ytb_cookies_file.parent.mkdir(parents=True, exist_ok=True)
            save_cookies_with_netscape(
                self.config["ytb_ck"],
                ytb_cookies_file,
                "youtube.com",
            )
            config["ytb_cookies_file"] = str(ytb_cookies_file)

        config.save_config()

        # 关键词 -> Parser 映射
        self.parser_map: dict[str, BaseParser] = {}

        # 关键词 -> 正则 列表
        self.key_pattern_list: list[tuple[str, re.Pattern[str]]] = []

        # 渲染器
        self.renderer = CommonRenderer(config)

        # 下载器
        self.downloader = Downloader(config)

        # 缓存清理器
        self.cleaner = CacheCleaner(self.context, self.config)

    async def initialize(self):
        """加载、重载插件时触发"""
        self.register_parser()
        CommonRenderer.load_resources()

    def register_parser(self):
        """注册解析器"""
        # 获取所有解析器
        all_subclass = BaseParser.get_all_subclass()
        # 过滤掉禁用的平台
        enabled_classes = [
            _cls
            for _cls in all_subclass
            if _cls.platform.display_name in self.config["enable_platforms"]
        ]
        # 启用的平台
        platform_names = []
        for _cls in enabled_classes:
            parser = _cls(self.config, self.downloader)
            platform_names.append(parser.platform.display_name)
            for keyword, _ in _cls._key_patterns:
                self.parser_map[keyword] = parser
        logger.info(f"启用平台: {'、'.join(platform_names)}")

        # 关键词-正则对，一次性生成并排序
        patterns: list[tuple[str, re.Pattern[str]]] = [
            (kw, re.compile(pt) if isinstance(pt, str) else pt)
            for cls in enabled_classes
            for kw, pt in cls._key_patterns
        ]
        # 长关键词优先
        patterns.sort(key=lambda x: -len(x[0]))
        logger.debug(f"关键词-正则对已生成：{patterns}")
        self.key_pattern_list = patterns

    def get_parser_by_type(self, parser_type):
        for parser in self.parser_map.values():
            if isinstance(parser, parser_type):
                return parser
        raise ValueError(f"未找到类型为 {parser_type} 的 parser 实例")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def prob_read_feed(self, event: AstrMessageEvent):
        """消息的统一入口"""
        umo = event.unified_msg_origin

        # 禁用会话
        if umo in self.config["disabled_sessions"]:
            return

        text = event.message_str

        # 过滤空文本
        if not text:
            return

        # 匹配 (关键词 + 正则双重判定)
        keyword: str = ""
        searched: re.Match[str] | None = None
        for kw, pat in self.key_pattern_list:
            if kw not in text:
                continue
            if m := pat.search(text):
                keyword, searched = kw, m
                break
        if searched is None:
            return

        logger.debug(f"匹配结果: {keyword}, {searched}")

        # 取解析器
        parser = self.parser_map[keyword]
        # 解析
        parse_res: ParseResult = await parser.parse(keyword, searched)

        # 渲染内容并发送
        async for chain in self.renderer.render_messages(parse_res):
            yield event.chain_result(chain)  # type: ignore

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bm")
    async def bm(self, event: AstrMessageEvent):
        """获取B站的音频"""
        text = event.message_str
        matched = re.search(r"(BV[A-Za-z0-9]{10})(\s\d{1,3})?", text)
        if not matched:
            yield event.plain_result("请发送正确的 BV 号")
            return

        bvid, page_num = matched.group(1), matched.group(2)
        page_idx = int(page_num) if page_num else 0

        parser: BilibiliParser = self.get_parser_by_type(BilibiliParser)  # type: ignore

        _, audio_url = await parser.extract_download_urls(
            bvid=bvid, page_index=page_idx
        )
        if not audio_url:
            yield event.plain_result("未找到可下载的音频")
            return

        audio_path = await self.downloader.download_audio(
            audio_url, audio_name=f"{bvid}-{page_idx}.mp3", ext_headers=parser.headers
        )
        yield event.chain_result([Record(audio_path)])  # type: ignore

        if self.config["upload_audio"]:
            pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ym")
    async def ym(self, event: AstrMessageEvent):
        """获取油管的音频"""
        text = event.message_str
        parser = self.get_parser_by_type(YouTubeParser)
        _, matched = parser.search_url(text)
        if not matched:
            yield event.plain_result("请发送正确的油管链接")
            return

        url = matched.group(0)

        audio_path = await self.downloader.download_audio(url, use_ytdlp=True)
        yield event.chain_result([Record(audio_path)])  # type: ignore

        if self.config["upload_audio"]:
            pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("登录B站", alias={"blogin", "登录b站"})
    async def login_bilibili(self, event: AstrMessageEvent):
        """扫码登录B站"""
        parser: BilibiliParser = self.get_parser_by_type(BilibiliParser)  # type: ignore
        qrcode = await parser.login_with_qrcode()
        yield event.chain_result([Image.fromBytes(qrcode)])
        async for msg in parser.check_qr_state():
            yield event.plain_result(msg)

    @filter.command("开启解析")
    async def open_parser(self, event: AstrMessageEvent):
        """开启当前会话的解析"""
        umo = event.unified_msg_origin
        if umo in self.config["disabled_sessions"]:
            self.config["disabled_sessions"].remove(umo)
            self.config.save_config()
            yield event.plain_result("解析已开启")
        else:
            yield event.plain_result("解析已开启，无需重复开启")

    @filter.command("关闭解析")
    async def close_parser(self, event: AstrMessageEvent):
        """关闭当前会话的解析"""
        umo = event.unified_msg_origin
        if umo not in self.config["disabled_sessions"]:
            self.config["disabled_sessions"].append(umo)
            self.config.save_config()
            yield event.plain_result("解析已关闭")
        else:
            yield event.plain_result("解析已关闭，无需重复关闭")

    async def terminate(self):
        """插件卸载时"""
        # 关下载器里的会话
        await self.downloader.close()
        # 关所有解析器里的会话
        await BaseParser.close_session()
        # 关缓存清理器
        await self.cleaner.stop()
