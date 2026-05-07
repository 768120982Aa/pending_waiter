"""
pending_waiter - 话没说完？我等你

当用户发来"我想想""我看看"等信号时，先确认是否为"话没说完"，
若是则回复简短确认并等待续文，合并后调用 LLM 统一回复。

行为边界:
  - 关键词预检命中后才走 LLM，日常对话零开销
  - 指令消息（/、#、!开头）自动跳过，不干扰命令
  - 私聊：正常运作
  - 群聊：不区分消息是否面向 bot，命中即触发。
    如果群聊中有多人交互，可能会出现 bot 对别人的对话插嘴的情况。
    建议群聊场景下交给调校者自行处理——比如 combined with @提及过滤。

流程:
  用户消息 → 关键词预检 → LLM 二次确认
  → [是] 回复简短确认 → session_waiter 等待续文
        → 收到续文后合并 → 调用 LLM 回复 → 结束
  → [否] 放行，走正常 AI 流程
  → [超时 60s] 提示并结束

自定义:
  插件目录下的 config.json 可覆盖所有文本和行为参数，
  修改后重载插件即可生效。详见 README。
"""

import json
import os
import re

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.utils.session_waiter import session_waiter, SessionController


@register("pending_waiter", "soulfish", "话没说完？我等你", "1.0.0", "https://github.com/soulfish/pending_waiter")
class PendingWaiterPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._cfg = {}  # 配置文件缓存
        self._load_config()

    # ── 配置文件加载 ──────────────────────────────────

    def _load_config(self):
        """读取插件目录下的 config.json，不存在则用硬编码默认值。"""
        cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
        try:
            if os.path.exists(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                # 过滤掉以 _ 开头的说明字段
                self._cfg = {k: v for k, v in raw.items() if not k.startswith("_")}
                logger.info(f"pending_waiter: 已加载配置文件 ({cfg_path})")
            else:
                logger.info("pending_waiter: 未找到 config.json，使用默认配置")
                self._cfg = {}
        except Exception as e:
            logger.error(f"pending_waiter: 配置文件读取失败 —— {e}")
            self._cfg = {}

        # 编译关键词正则
        keywords = self._cfg.get("keywords", [
            r'我[想看看][一想查查考考考虑思]',
            r'让[你我][想看看][一想查查考考]',
            r'等[一等一下一哈会会儿]',
            r'稍等',
            r'怎么[说讲]呢',
            r'这个嘛',
            r'(嗯|唔|额|呃)\s*[。\.…]*$',
            r'好问题',
            r'good question',
        ])
        self._compiled = [re.compile(p, re.IGNORECASE) for p in keywords]

    def _get(self, key: str, default=None):
        """从配置取值，不存在时返回 default。"""
        return self._cfg.get(key, default)

    # ── 关键词预检 ──────────────────────────────────

    def _check_keywords(self, text: str) -> bool:
        return any(p.search(text) for p in self._compiled)

    def _pick_ack(self, text: str) -> str:
        acks = self._get("acknowledgments", {})
        for kw, ack in acks.items():
            if kw in text:
                return ack
        return acks.get("_default", "嗯，我在听。")

    # ── LLM 判断 ────────────────────────────────────

    async def _judge_pending(self, text: str, umo: str) -> bool:
        """用 LLM 判断是否为"话没说完"信号。极小调用。"""
        template = self._get("judge_prompt",
            "判断用户消息是否表示ta话没说完（需要思考、查询、组织语言）。\n"
            "用户消息：{text}\n"
            "只回答一个字：是 或 否"
        )
        prompt = template.replace("{text}", text)
        sys_prompt = self._get("judge_system_prompt", "你是一个判断助手。只回答是或否。")

        try:
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                session_id=None,
                system_prompt=sys_prompt,
            )
            raw = resp.completion_text if hasattr(resp, 'completion_text') else str(resp)
            return raw.strip().startswith('是')
        except Exception as e:
            logger.error(f"pending_waiter: 判断失败 —— {e}")
            return False

    # ── 调用 LLM 生成合并回复 ────────────────────────

    async def _merge_reply(self, event: AstrMessageEvent, merged: str) -> str:
        """用当前会话的 LLM 处理合并文本"""
        fallback = self._get("fallback_message", "嗯，你接着说。")
        try:
            umo = event.unified_msg_origin
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=merged,
                session_id=umo,
            )
            return resp.completion_text if hasattr(resp, 'completion_text') else str(resp)
        except Exception as e:
            logger.error(f"pending_waiter: 合并回复失败 —— {e}")
            return fallback

    # ── 消息入口 ─────────────────────────────────────

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE
        | filter.EventMessageType.PRIVATE_MESSAGE
        | filter.EventMessageType.OTHER_MESSAGE
    )
    async def on_message(self, event: AstrMessageEvent):
        text = event.message_str.strip()
        if not text:
            return

        # 跳过指令前缀
        prefixes = tuple(self._get("skip_prefixes", ["/", "#", "!", "。"]))
        if text.startswith(prefixes):
            return

        # 1. 关键词预检
        if not self._check_keywords(text):
            return

        # 2. LLM 二次确认
        if not await self._judge_pending(text, event.unified_msg_origin):
            return

        # 3. 是未完信号
        yield event.plain_result(self._pick_ack(text))
        event.stop_event()
        logger.info(f"pending_waiter: 进入等待 —— [{text[:50]}]")

        # 4. session_waiter 等待续文
        timeout = self._get("timeout", 60)
        timeout_msg = self._get("timeout_message", "嗯？还在吗？没回话我先忙别的了。")
        empty_msg = self._get("empty_message", "嗯？你说。")
        fallback_msg = self._get("fallback_message", "嗯，你接着说。")
        merge_tpl = self._get("merge_template", "{original}\n——接着：{continuation}")

        try:
            @session_waiter(timeout=timeout, record_history_chains=False)
            async def waiter(controller: SessionController, ev: AstrMessageEvent):
                cont = ev.message_str.strip()
                if not cont:
                    await ev.send(ev.plain_result(empty_msg))
                    controller.keep(timeout=timeout, reset_timeout=True)
                    return

                merged = merge_tpl.replace("{original}", text).replace("{continuation}", cont)
                logger.info(f"pending_waiter: 收到续文 —— [{merged[:80]}...]")

                reply = await self._merge_reply(ev, merged)
                await ev.send(ev.plain_result(reply))
                controller.stop()

            await waiter(event)

        except TimeoutError:
            yield event.plain_result(timeout_msg)
        except Exception as e:
            logger.error(f"pending_waiter: 异常 —— {e}")
            yield event.plain_result(fallback_msg)

    async def terminate(self):
        logger.info("pending_waiter: 插件已卸载")
