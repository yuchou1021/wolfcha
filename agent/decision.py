"""Decision engine - builds prompts and calls LLM for all decision types."""

import json
import re
import random
from typing import List, Dict, Any, Optional

from game_types.game_types import GameState, Player
from lib.llm import LLMMessage, generate_completion, strip_markdown_code_fences
from lib.game_constants import (
    GAME_TEMPERATURE,
    ENABLE_REFLECTION,
    IMPORTANT_REFLECTION_ONLY,
    SPEECH_CONTEXT_CHARS,
    VOTE_CONTEXT_CHARS,
    MEMORY_CONTEXT_CHARS,
)
from lib.prompt_utils import build_game_context, build_today_transcript, message_visible_to
from lib.game_logger import GameLogger
from llms_config import load_llms_config, get_player_llm_config, PlayerLLMConfig

from .memory import AgentMemory, MemorySystem, Reflection
from .information import InformationManager
from .strategy import StrategyLibrary
from .personas import PERSONAS, Persona, deal_personas


ROLE_CHINESE = {
    "Villager": "村民",
    "Werewolf": "狼人",
    "Seer": "预言家",
    "Witch": "女巫",
    "Hunter": "猎人",
    "Guard": "守卫",
}


_SEER_CLAIM_PHRASES = (
    "我是预言家",
    "我是真预言家",
    "我的身份是预言家",
    "我的身份是真预言家",
    "我就是预言家",
)

# 「预言家死了/出局了」这类第三人称的死讯式说法：不是宣称自己是预言家，
# 但它是判断"后跳预言家的人是不是在悍跳"的关键公开信息，必须记下来。
_SEER_DEATH_RE = re.compile(r"预言家[^。；！？\n]{0,10}(?:死|出局|没了|被刀|被杀|走了|凉了)")


def is_seer_claim(text: str) -> bool:
    """发言是否在宣称自己就是预言家（第一人称）。"""
    if not text:
        return False
    return any(phrase in text for phrase in _SEER_CLAIM_PHRASES)


def is_seer_death_claim(text: str) -> bool:
    """发言是否在公开表示「预言家已经死了/出局了」（第三人称死讯）。"""
    if not text or "预言家" not in text or is_seer_claim(text):
        return False
    return _SEER_DEATH_RE.search(text) is not None


def parse_seer_check(content: str):
    """从一句跳预言家的发言里抽出「第几夜验了谁、结果是什么」。"""
    target_m = re.search(r"(?:验|查)(?:过|了|的)?\s*(\d{1,2})\s*号", content)
    result_m = re.search(r"(金水|查杀|好人|狼人|是狼)", content)
    result = result_m.group(1) if result_m else None
    if result == "是狼":
        result = "狼人"

    night_text = "0"
    night_m = re.search(r"第\s*([0-9一二三四五六七八九十]+)\s*[晚夜]", content)
    if night_m:
        night_text = night_m.group(1)
    elif "昨晚" in content or "昨夜" in content:
        night_text = ""  # 由调用方用发言当天的 day 代替

    return {
        "target": f"{target_m.group(1)}号" if target_m else "未报",
        "result": result or "未报",
        "night_text": night_text,
    }


_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def night_number(text: str) -> int:
    """把「1」「一」「十」这类夜次文本转成数字，认不出来返回 0。"""
    if not text:
        return 0
    if text.isdigit():
        return int(text)
    if text in _CN_DIGITS:
        return _CN_DIGITS[text]
    if text.startswith("十"):
        return 10 + _CN_DIGITS.get(text[1:], 0)
    if "十" in text:
        head, _, tail = text.partition("十")
        return _CN_DIGITS.get(head, 0) * 10 + _CN_DIGITS.get(tail, 0)
    return 0


# 兼容旧代码：把性格池压成 (名称, 描述) 二元组。
# 新代码请直接用 agent.personas 里的 Persona / deal_personas。
PERSONALITY_ARCHETYPES = [(persona.name, persona.describe()) for persona in PERSONAS]


HARD_TACTICS = {
    "Werewolf": """
【困难模式·高级狼人打法】
- 场上有人跳预言家时，你可以悍跳预言家：报一个"查验"，但必须和已知死讯、票型对得上
- 可以给真预言家发查杀、给狼队友发金水；报查验顺序要自然（先说验了谁，再说结果）
- 不要三只狼全冲同一个人，安排一个队友分票或倒钩，票型太齐会被抓
- 夜里优先刀已经跳预言家/女巫的人""",
    "Seer": """
【困难模式·高级预言家打法】
- 首日查到查杀，必须跳出来报，别等
- 只有金水可以第二天再跳，但要在发言里留下"我有信息"的伏笔
- 有人悍跳你，抓他报查验的顺序、金水对象的票型矛盾，用细节打
- 报完查验记得留警徽流：我死了先查谁""",
    "Witch": """
【困难模式·高级女巫打法】
- 首夜救不救要看刀口：刀到话多/像神的位置可以救，刀到边缘位可以留药
- 毒药优先毒悍跳预言家的人，或者投票明显抱团的人
- 不要过早暴露自己用过药""",
    "Hunter": """
【困难模式·高级猎人打法】
- 前期藏好，像村民一样发言
- 被抗推时果断跳猎人威慑："投我可以，枪我带一个"
- 枪尽量留给关键轮，别凭情绪开枪""",
    "Guard": """
【困难模式·高级守卫打法】
- 优先守跳出来的预言家，其次守自己
- 记住不能连守同一个人，提前规划两晚的守护对象
- 出现平安夜不要得意，装作不知道""",
    "Villager": """
【困难模式·高级村民打法】
- 用票型找狼：谁在关键轮跟票、谁突然改票
- 对比两个人对同一件事的说法，抓前后矛盾
- 不要被"话多=好人"带偏，也不要无脑跟票""",
}


class DecisionEngine:
    """决策引擎 - 基于 LLM 生成各类决策"""

    def __init__(
        self,
        info_manager: InformationManager,
        memory_system: MemorySystem,
        strategy_lib: StrategyLibrary,
        llm_config_path: Optional[str] = None,
        logger: Optional[GameLogger] = None,
    ):
        self.info_manager = info_manager
        self.memory_system = memory_system
        self.strategy_lib = strategy_lib
        self.logger = logger
        # 加载 LLM 配置
        self._llm_config_data = load_llms_config(llm_config_path)
        # 玩家性格缓存: player_id -> personality_text
        self._personality_cache: Dict[str, str] = {}

    def get_player_llm_config(self, player: Player) -> PlayerLLMConfig:
        """获取玩家的 LLM 配置"""
        return get_player_llm_config(player.seat, self._llm_config_data)

    def get_personality_text(self, player: Player) -> str:
        """获取玩家的性格描述文本，用于注入 prompt"""
        text = self._personality_cache.get(player.player_id, "")
        if text:
            return f"\n【你的性格】\n{text}\n"
        return ""

    def set_personality(self, player_id: str, personality_text: str):
        """设置玩家的性格描述"""
        self._personality_cache[player_id] = personality_text

    # ========== 夜间决策 ==========

    def _get_llm_config_dict(self, player: Player) -> Dict[str, Any]:
        """获取玩家的 LLM 配置字典，用于反思调用"""
        cfg = self.get_player_llm_config(player)
        return {
            "model": cfg.model,
            "api_base": cfg.api_base,
            "api_key": cfg.api_key,
            "temperature": 0.5,
        }

    async def _trigger_reflection(
        self,
        player: Player,
        memory: AgentMemory,
        event_description: str,
        action_type: str,
        day: int,
        phase: str,
    ):
        """触发反思（异步，失败不影响流程）"""
        if not ENABLE_REFLECTION:
            return
        if IMPORTANT_REFLECTION_ONLY and action_type in ("speech", "vote"):
            return
        try:
            llm_config = self._get_llm_config_dict(player)
            await self.memory_system.generate_reflection(
                memory=memory,
                event_description=event_description,
                action_type=action_type,
                day=day,
                phase=phase,
                player=player,
                llm_config=llm_config,
            )
        except Exception as e:
            print(f"[反思触发失败] {player.display_name}: {e}")

    # ========== 夜间决策（原有） ==========

    async def generate_guard_decision(
        self,
        player: Player,
        state: GameState,
        memory: AgentMemory,
    ) -> Optional[int]:
        """生成守卫保护决策"""
        role_info = self.info_manager.get_role_specific_info(player, state)
        last_protected = role_info.get("last_protected_seat")

        alive_players = [p for p in state.players if p.alive]
        valid_targets = [p for p in alive_players if p.seat != last_protected]

        if not valid_targets:
            valid_targets = alive_players

        options = ", ".join([f"{p.seat + 1}号 {p.display_name}" for p in valid_targets])

        strategies = self.strategy_lib.get_strategies_for_role(player.role, state.difficulty)
        strategy_text = self.strategy_lib.format_strategies_for_prompt(strategies)

        personality = self.get_personality_text(player)
        system_msg = f"""你是{player.display_name}，{player.seat + 1}号位，身份是守卫。你是一个真实的人类玩家，不是AI。
{personality}
你肩负着保护关键人物的重任。每到夜晚，你都在焦虑地猜测——狼人今晚会对谁下手？你必须在有限的信息中做出判断，一旦守错，可能就是一条人命。

【能力与限制】
- 每晚选择一名玩家保护，使其免受狼人击杀
- 不能连续两晚保护同一人（上次保护：{f"{last_protected + 1}号" if last_protected is not None else "无"}）
- 守护成功则当晚平安夜

【思考方式】
先想：狼人最想杀谁？（神职？发言强势的人？昨晚没守的高价值目标？）
再想：我守谁的收益最大？有没有可能被狼人读到我的守护模式？
最后决定：在不确定中做出你认为最优的选择。

{strategy_text}"""

        memory_text = self.memory_system.format_memory_for_prompt(memory)

        user_msg = f"""第{state.day}天夜晚，你睁眼了。

{memory_text}

【存活玩家】
{options}

现在闭上眼想一想：
1. 今晚谁最危险？狼人会盯上谁？（发言强势的？疑似神职的？）
2. 有没有人昨天的表现让你觉得他是关键角色，必须活着？
3. 狼人会不会猜到你的守护思路，故意绕开？

做出你的选择。

严格输出JSON：
{{"reasoning": "你的内心想法（50-100字，像人说话）", "target_seat": 座位号数字}}"""

        result = await self._call_llm_json(system_msg, user_msg, player, action_type="guard", day=state.day, phase="NIGHT")
        target = self._extract_seat(result, valid_targets)
        if target is not None:
            # 反思：守卫行动后
            target_name = next((p.display_name for p in valid_targets if p.seat == target), f"{target+1}号")
            await self._trigger_reflection(
                player, memory,
                f"我今晚保护了{target_name}。",
                "night_action", state.day, "NIGHT",
            )
            return target

        # fallback
        fallback_target = random.choice(valid_targets).seat if valid_targets else None
        if fallback_target is not None:
            target_name = next((p.display_name for p in valid_targets if p.seat == fallback_target), f"{fallback_target+1}号")
            await self._trigger_reflection(
                player, memory,
                f"我今晚保护了{target_name}。",
                "night_action", state.day, "NIGHT",
            )
        return fallback_target

    async def generate_wolf_decision(
        self,
        player: Player,
        state: GameState,
        memory: AgentMemory,
        existing_votes: Dict[str, int],
    ) -> Optional[int]:
        """生成狼人击杀决策"""
        role_info = self.info_manager.get_role_specific_info(player, state)
        teammates = role_info.get("teammates", [])

        alive_players = [p for p in state.players if p.alive]
        options = ", ".join([f"{p.seat + 1}号 {p.display_name}" for p in alive_players])

        teammate_info = ""
        if teammates:
            teammate_list = ", ".join([f"{t['seat'] + 1}号 {t['name']}" for t in teammates])
            teammate_info = f"你的狼队友: {teammate_list}"
        else:
            teammate_info = "你是唯一存活的狼人"

        vote_info = ""
        if existing_votes:
            vote_lines = []
            for t in teammates:
                pid = next((p.player_id for p in state.players if p.seat == t["seat"]), None)
                if pid and pid in existing_votes:
                    target_seat = existing_votes[pid]
                    vote_lines.append(f"  {t['name']} 选择了 {target_seat + 1}号")
            if vote_lines:
                vote_info = "【队友投票】\n" + "\n".join(vote_lines)

        strategies = self.strategy_lib.get_strategies_for_role(player.role, state.difficulty)
        strategy_text = self.strategy_lib.format_strategies_for_prompt(strategies)

        personality = self.get_personality_text(player)
        system_msg = f"""你是{player.display_name}，{player.seat + 1}号位，身份是狼人。你是一个真实的人类玩家，不是AI。
{personality}
{teammate_info}

夜晚是你的主场。你和队友要在暗中猎杀，同时不能让白天的自己露出马脚。每一刀都是一个信号——杀谁、不杀谁，都会被场上的聪明人分析。

【博弈意识】
- 击杀不只是消灭威胁，也是在制造信息迷雾
- 杀太有规律会被读出模式（比如总杀发言强的人）
- 有时候杀一个"意料之外"的人，反而能打乱好人的推理节奏
- 要考虑守卫可能守谁，女巫可能救谁

{strategy_text}"""

        memory_text = self.memory_system.format_memory_for_prompt(memory)

        user_msg = f"""第{state.day}天夜晚，狼人睁眼。

{memory_text}

{vote_info}

【场上存活】
{options}

想清楚再动手：
1. 谁对我们威胁最大？（疑似预言家？发言带节奏的？）
2. 杀这个人之后，明天白天的局面会怎样？会不会直接指向我们？
3. 守卫今晚可能守谁？有没有办法避开？
4. 队友的意见是什么？要不要统一？

选一个目标。

严格输出JSON：
{{"reasoning": "你的内心盘算（50-150字，像狼人密谋时的口吻）", "target_seat": 座位号数字}}"""

        result = await self._call_llm_json(system_msg, user_msg, player, action_type="wolf", day=state.day, phase="NIGHT")
        # 狼人不应该杀自己的队友（除非自刀策略），优先排除队友
        teammate_seats = {t["seat"] for t in teammates}
        non_teammate_alive = [p for p in alive_players if p.seat not in teammate_seats and p.seat != player.seat]

        target = self._extract_seat(result, alive_players)
        if target is not None:
            # 反思：狼人击杀后
            target_name = next((p.display_name for p in alive_players if p.seat == target), f"{target+1}号")
            await self._trigger_reflection(
                player, memory,
                f"我们决定今晚击杀{target_name}。",
                "night_action", state.day, "NIGHT",
            )
            return target

        # fallback: 随机选一个非队友
        if non_teammate_alive:
            fb = random.choice(non_teammate_alive).seat
        else:
            fb = random.choice(alive_players).seat if alive_players else None
        if fb is not None:
            target_name = next((p.display_name for p in alive_players if p.seat == fb), f"{fb+1}号")
            await self._trigger_reflection(
                player, memory,
                f"我们决定今晚击杀{target_name}。",
                "night_action", state.day, "NIGHT",
            )
        return fb

    async def generate_witch_decision(
        self,
        player: Player,
        state: GameState,
        memory: AgentMemory,
        wolf_target: Optional[int],
    ) -> Optional[Dict[str, Any]]:
        """生成女巫用药决策"""
        role_info = self.info_manager.get_role_specific_info(player, state)
        has_antidote = role_info.get("has_antidote", False)
        has_poison = role_info.get("has_poison", False)

        if not has_antidote and not has_poison:
            return {"type": "pass"}

        alive_players = [p for p in state.players if p.alive and p.player_id != player.player_id]
        poison_targets = ", ".join([f"{p.seat + 1}号 {p.display_name}" for p in alive_players])

        victim_info = ""
        if wolf_target is not None and has_antidote:
            victim = next((p for p in state.players if p.seat == wolf_target), None)
            if victim:
                victim_info = f"今晚狼人击杀了 {wolf_target + 1}号 {victim.display_name}。"
        elif not has_antidote:
            victim_info = "你已经使用过解药，无法感知今晚的死亡信息。"
        else:
            victim_info = "今晚没有人被狼人击杀。"

        strategies = self.strategy_lib.get_strategies_for_role(player.role, state.difficulty)
        strategy_text = self.strategy_lib.format_strategies_for_prompt(strategies)

        personality = self.get_personality_text(player)
        system_msg = f"""你是{player.display_name}，{player.seat + 1}号位，身份是女巫。你是一个真实的人类玩家，不是AI。
{personality}
你手中的药水是整局游戏最珍贵的资源——用对了能翻盘，用错了万劫不复。每次用药都是一次豪赌，你必须在不完全信息下做出可能改变全局的决定。

【药水状态】
- 解药：{'可用 — 可以救活今晚被杀的人' if has_antidote else '已用完'}
- 毒药：{'可用 — 可以毒杀一名你认为是狼人的玩家' if has_poison else '已用完'}
- 每晚最多用一种药，用了就没了

【今晚情况】
{victim_info}

【用药哲学】
- 解药：这个人值得救吗？他活着对好人阵营有多大价值？如果留着解药，后面可能救到更关键的人吗？第一晚通常值得救，后期要慎重。
- 毒药：你有多大把握他是狼？毒错一个好人等于帮狼人杀人。没有八成以上把握，宁可不毒。
- 不用药也是一种选择，有时候按兵不动才是最优解。

{strategy_text}"""

        memory_text = self.memory_system.format_memory_for_prompt(memory)

        user_msg = f"""第{state.day}天夜晚，你感受到了今晚的情况。

{memory_text}

【可毒杀目标】
{poison_targets}

现在做决定：
1. 如果有人被杀——这个人重要吗？救他能改变局势吗？还是留着解药以后用更划算？
2. 如果想毒人——你对谁的狼人身份最有把握？把握不够就别动手。
3. 什么都不做也行——药水留着本身就是一种威慑。

做出你的选择。

严格输出JSON：
{{"reasoning": "你的纠结和判断（50-150字，体现权衡过程）", "action": "save"或"poison"或"pass", "target_seat": 目标座位号或null}}"""

        result = await self._call_llm_json(system_msg, user_msg, player, action_type="witch", day=state.day, phase="NIGHT")

        if result:
            action = result.get("action", "pass").lower()
            if action == "save" and has_antidote and wolf_target is not None:
                # 反思：女巫救人后
                victim_name = next((p.display_name for p in state.players if p.seat == wolf_target), f"{wolf_target+1}号")
                await self._trigger_reflection(
                    player, memory,
                    f"我使用解药救了{victim_name}。",
                    "night_action", state.day, "NIGHT",
                )
                return {"type": "save"}
            elif action == "poison" and has_poison:
                target_seat = result.get("target_seat")
                if target_seat is not None:
                    seat = int(target_seat) - 1 if int(target_seat) > 0 else int(target_seat)
                    # 验证目标合法
                    if any(p.seat == seat and p.alive for p in state.players):
                        # 反思：女巫毒人后
                        poison_name = next((p.display_name for p in state.players if p.seat == seat), f"{seat+1}号")
                        await self._trigger_reflection(
                            player, memory,
                            f"我使用毒药毒了{poison_name}。",
                            "night_action", state.day, "NIGHT",
                        )
                        return {"type": "poison", "target": seat}

        # 反思：女巫选择不用药
        await self._trigger_reflection(
            player, memory,
            "我今晚选择不使用任何药水。",
            "night_action", state.day, "NIGHT",
        )
        return {"type": "pass"}

    async def generate_seer_decision(
        self,
        player: Player,
        state: GameState,
        memory: AgentMemory,
    ) -> Optional[int]:
        """生成预言家查验决策"""
        alive_players = [p for p in state.players if p.alive and p.player_id != player.player_id]

        seer_history = state.night_actions.seer_history or []
        checked_seats = {h.get("targetSeat") for h in seer_history}
        unchecked = [p for p in alive_players if p.seat not in checked_seats]
        target_players = unchecked if unchecked else alive_players

        options = ", ".join([f"{p.seat + 1}号 {p.display_name}" for p in target_players])

        history_info = ""
        if seer_history:
            history_lines = []
            for record in seer_history:
                seat = record.get("targetSeat")
                is_wolf = record.get("isWolf")
                result_text = "狼人" if is_wolf else "好人"
                history_lines.append(f"  {seat + 1}号: {result_text}")
            history_info = "【查验记录】\n" + "\n".join(history_lines)

        strategies = self.strategy_lib.get_strategies_for_role(player.role, state.difficulty)
        strategy_text = self.strategy_lib.format_strategies_for_prompt(strategies)

        personality = self.get_personality_text(player)
        system_msg = f"""你是{player.display_name}，{player.seat + 1}号位，身份是预言家。你是一个真实的人类玩家，不是AI。
{personality}
你是好人阵营的眼睛，每晚一次查验机会，查对了能锁狼，查错了浪费一晚。你背负着巨大的信息优势，但也因此成为狼人的头号猎杀目标——你可能活不了太久，所以每一次查验都要用在刀刃上。

【能力】
- 每晚查验一名玩家：好人 or 狼人
- 结果只有你知道，白天可以选择公开

【查验思路】
- 查谁能获得最大信息增量？优先查"身份最模糊"的人，而不是已经被大家怀疑的人
- 场上谁的发言最让你拿不准？谁可能在演戏？
- 如果有人跳了预言家对抗你，优先验证他的"金水"或"查杀"对象

{strategy_text}

{history_info}"""

        memory_text = self.memory_system.format_memory_for_prompt(memory)

        user_msg = f"""第{state.day}天夜晚，你睁眼准备查验。

{memory_text}

【可查验目标】
{options}

想想今晚查谁收益最大：
1. 谁的身份最模糊？白天发言让你最拿不准的是谁？
2. 查出来的结果能不能帮你明天带好节奏？
3. 有没有人声称了身份但你无法确认真假？验一下？
4. 如果你明天可能被狼人杀掉，今晚查谁留下的信息最有价值？

选一个目标。

严格输出JSON：
{{"reasoning": "你的判断过程（50-100字）", "target_seat": 座位号数字}}"""

        result = await self._call_llm_json(system_msg, user_msg, player, action_type="seer", day=state.day, phase="NIGHT")
        target = self._extract_seat(result, target_players)
        if target is not None:
            # 反思在 game_controller 中查验结果出来后触发（因为此时还不知道结果）
            return target

        # fallback
        fb = random.choice(target_players).seat if target_players else None
        return fb

    # ========== 白天决策 ==========

    async def generate_speech(
        self,
        player: Player,
        state: GameState,
        memory: AgentMemory,
    ) -> str:
        """生成AI发言"""
        role_info = self.info_manager.get_role_specific_info(player, state)
        strategies = self.strategy_lib.get_strategies_for_role(player.role, state.difficulty)
        strategy_text = self.strategy_lib.format_strategies_for_prompt(strategies)

        role_name = ROLE_CHINESE.get(player.role, player.role)

        # 构建角色专属系统消息（含性格）
        system_msg = self._build_speech_system_msg(player, role_info, strategy_text, state.difficulty)

        memory_text = self._memory_text(memory)
        today_transcript = build_today_transcript(state, max_chars=SPEECH_CONTEXT_CHARS, player=player)
        brief = self._build_situation_brief(state, player)

        # 该跳身份的时候必须跳，不能一直"先听听"
        someone_claimed_seer = any(
            is_seer_claim(m.content or "") for m in state.messages if not m.is_system
        )
        i_claimed_seer = any(
            is_seer_claim(m.content or "")
            for m in state.messages
            if m.player_id == player.player_id
        )
        must_do = ""
        my_claim_text = ""
        my_claim_msgs = [
            m.content for m in state.messages
            if m.player_id == player.player_id and is_seer_claim(m.content or "")
        ]
        if my_claim_msgs:
            my_claim_text = my_claim_msgs[-1]

        if my_claim_text:
            must_do = (f"【保持一致】你之前已经跳过预言家，原话是：\"{my_claim_text[:80]}\"。"
                       f"这次报的查验对象和结果必须和之前一致，绝对不能改口。\n")
        elif player.role == "Seer" and not i_claimed_seer:
            checks = role_info.get("check_history") or []
            if checks:
                must_do = ("【必须做】你是真预言家，今天必须跳出来：先报“我是预言家”，"
                           "再逐晚报查验结果（X号是狼/好人）。不要只说“先听听”。")
        elif (player.role == "Werewolf" and someone_claimed_seer
              and not i_claimed_seer and state.difficulty == "hard"):
            if random.random() < 0.5:
                must_do = ("【必须悍跳】已经有人跳预言家。你是狼，这一轮必须跳预言家对抗："
                           "报一个和死讯、票型对得上的查验，给真预言家发查杀，或给队友发金水。")
            else:
                must_do = ("【可以悍跳】已经有人跳预言家。你是狼，可以考虑对跳，"
                           "报一个自洽的查验；不想跳就低调跟票，别暴露队友。")

        user_msg = f"""第{state.day}天白天，轮到你发言了。

【局势速览】
{brief}

【你的记忆】
{memory_text}

【今日已有发言】
{today_transcript if today_transcript else "（你是第一个发言的）"}

{must_do}

发言要求（很重要）：
- 如果今天没有任何死讯、也没人跳身份、没人踩你：只说一句 10～15 字
- 如果别人已经说过同样的判断：最多半句同意，没有新发现就回"过"
- 有人跳预言家/被查杀/自己被踩：20～40 字，表态 + 一个理由
- 真人玩家可能只打了几个字，要回应他说的话，不要要求他给出完整逻辑
- 直接输出你的发言："""

        # 使用玩家专属 LLM 配置
        cfg = self.get_player_llm_config(player)
        messages = [
            LLMMessage(role="system", content=system_msg),
            LLMMessage(role="user", content=user_msg),
        ]

        try:
            response = await generate_completion(
                messages,
                model=cfg.model,
                temperature=cfg.temperature,
                max_tokens=min(cfg.max_tokens, 90),
                api_base=cfg.api_base,
                api_key=cfg.api_key,
            )
            content = response.choices[0]["message"]["content"].strip()
            raw_content = content
            # 清理可能的JSON包装
            if content.startswith("{") and "content" in content:
                try:
                    parsed = json.loads(content)
                    content = parsed.get("content", parsed.get("speech", content))
                except json.JSONDecodeError:
                    pass
            # 硬性截断，避免模型超长发挥
            if len(content) > 45:
                content = content[:45].rstrip("，,、 ") + "…"
            if self.logger:
                self.logger.log_decision(
                    seat=player.seat, player_name=player.display_name,
                    action_type="speech", day=state.day, phase="DAY",
                    model=cfg.model, system_msg=system_msg, user_msg=user_msg,
                    raw_response=raw_content, final_decision=content,
                )
            # 反思：发言后
            await self._trigger_reflection(
                player, memory,
                f"我刚才发言说：{content[:60]}{'...' if len(content) > 60 else ''}",
                "speech", state.day, "DAY",
            )
            return content
        except Exception as e:
            print(f"Agent speech error for {player.display_name}: {e}")
            return f"我是{player.seat + 1}号，暂时没有什么想说的。"

    async def generate_vote(
        self,
        player: Player,
        state: GameState,
        memory: AgentMemory,
    ) -> Optional[int]:
        """生成AI投票"""
        role_info = self.info_manager.get_role_specific_info(player, state)
        strategies = self.strategy_lib.get_strategies_for_role(player.role, state.difficulty)
        strategy_text = self.strategy_lib.format_strategies_for_prompt(strategies)

        role_name = ROLE_CHINESE.get(player.role, player.role)

        alive_players = [p for p in state.players if p.alive]
        options = ", ".join([f"{p.seat + 1}号 {p.display_name}" for p in alive_players])

        today_transcript = build_today_transcript(state, max_chars=VOTE_CONTEXT_CHARS, player=player)

        # 狼人投票特殊处理
        extra_info = ""
        if player.role == "Werewolf":
            teammates = role_info.get("teammates", [])
            if teammates:
                teammate_seats = [f"{t['seat'] + 1}号" for t in teammates]
                extra_info = f"\n【注意】你的队友是 {', '.join(teammate_seats)}，不要投票给队友（除非需要伪装）。"

        personality = self.get_personality_text(player)
        system_msg = f"""你是{player.display_name}，{player.seat + 1}号位，身份是{role_name}。你是一个真实的人类玩家，不是AI。
{personality}
{strategy_text}
{extra_info}

投票是狼人杀中最关键的博弈环节。你的每一票都是公开信息，所有人都会分析你投了谁。

【投票心理】
- 跟票（随大流）：安全但没有信息量，可能被认为是"划水"
- 独立投票：有主见但可能被孤立，如果投对了会加分，投错了会被怀疑
- 你要权衡：大势指向谁？你内心真正怀疑谁？这两者一致吗？
- 如果你是狼人：要跟随好人的投票趋势，但不要总是最后一个跟票，太明显了"""

        memory_text = self._memory_text(memory)

        own_speeches = [
            m for m in state.messages
            if m.day == state.day and m.player_id == player.player_id and not m.is_system
        ]
        own_speech = own_speeches[-1].content if own_speeches else ""

        user_msg = f"""第{state.day}天，投票环节。

{memory_text}

【局势速览】
{self._build_situation_brief(state, player)}

【你刚才的发言】
{own_speech if own_speech else "（你今天还没发言）"}

【今日完整发言回顾】
{today_transcript if today_transcript else "（无发言记录）"}

【可投票目标】
{options}

投票前想清楚：
1. 你刚才有没有当众说过要投谁？说了就必须投他，不能言行不一。
2. 今天的发言里，谁最可疑？谁的逻辑有漏洞？
3. 场上大多数人想投谁？如果你没有明确目标，尽量跟大多数人归票，不要散票；但跟票要能说出自己的理由。
4. 你投这个人，别人会怎么看你？会不会暴露你的立场或身份？

【严格禁止】
- 不要编造别人没说过的话。只能基于上面的发言记录来判断。
- "投票给某人"不等于"带队冲某人"。除非某人在发言中明确号召投票，否则不能说他"带队"或"带节奏"。

做出你的选择。

如果今天确实没有任何信息、你也没把握，可以弃票，target_seat 填 null。

严格输出JSON：
{{"reasoning": "你的投票理由（20-40字）", "target_seat": 座位号数字或null}}"""

        result = await self._call_llm_json(system_msg, user_msg, player, action_type="vote", day=state.day, phase="DAY")
        # 模型主动弃票
        if result and "target_seat" in result and result.get("target_seat") in (None, "null", "None", ""):
            return None
        target = self._extract_seat(result, alive_players)
        if target is not None:
            # 反思：投票后
            target_name = next((p.display_name for p in alive_players if p.seat == target), f"{target+1}号")
            await self._trigger_reflection(
                player, memory,
                f"我投票给了{target_name}。",
                "vote", state.day, "DAY",
            )
            return target

        # fallback
        fb = random.choice(alive_players).seat if alive_players else None
        if fb is not None:
            target_name = next((p.display_name for p in alive_players if p.seat == fb), f"{fb+1}号")
            await self._trigger_reflection(
                player, memory,
                f"我投票给了{target_name}。",
                "vote", state.day, "DAY",
            )
        return fb

    async def generate_last_words(
        self,
        player: Player,
        state: GameState,
        memory: AgentMemory,
    ) -> str:
        """生成AI遗言"""
        role_name = ROLE_CHINESE.get(player.role, player.role)

        personality = self.get_personality_text(player)
        system_msg = f"""你是{player.display_name}，{player.seat + 1}号位，身份是{role_name}。你是一个真实的人类玩家，不是AI。
{personality}
你要被淘汰了。这是你最后的发言机会。

【遗言的意义】
这是你留给场上所有人的最后一段话。不管你是什么身份，这段话都应该有分量。

- 如果你是好人/神职：把你知道的关键信息留下来，帮活着的人继续战斗。可以愤怒，可以不甘，但信息要清晰。
- 如果你是预言家：这是你最后的机会公布查验结果和警徽流，务必说清楚。
- 如果你是狼人：你可以继续演戏到最后一刻，混淆视听，给队友争取空间。也可以选择沉默。
  但**绝对不能说出队友是谁**，也不能用"你俩接着演""我给你们垫刀"这类暗示队友的话，只能自己演完。
- 任何身份都不能用座位号指代自己。

【情感】
你即将离开这局游戏。你可以表达你的情绪——愤怒、不甘、释然、最后的挣扎，甚至调侃。遗言不是冷冰冰的信息传递，是一个人最后的表达。"""

        memory_text = self.memory_system.format_memory_for_prompt(memory)

        user_msg = f"""{memory_text}

这是你最后的机会了。你想对大家说什么？
有什么信息必须留下？有什么话不吐不快？

注意：不要说队友是谁（狼人），不要用座位号指代自己。
50-120字，直接说出你的遗言："""

        # 使用玩家专属 LLM 配置
        cfg = self.get_player_llm_config(player)
        messages = [
            LLMMessage(role="system", content=system_msg),
            LLMMessage(role="user", content=user_msg),
        ]

        try:
            response = await generate_completion(
                messages,
                model=cfg.model,
                temperature=cfg.temperature,
                max_tokens=300,
                api_base=cfg.api_base,
                api_key=cfg.api_key,
            )
            content = response.choices[0]["message"]["content"].strip()
            raw_content = content
            if content.startswith("{"):
                try:
                    parsed = json.loads(content)
                    content = parsed.get("content", parsed.get("speech", content))
                except json.JSONDecodeError:
                    pass
            if self.logger:
                self.logger.log_decision(
                    seat=player.seat, player_name=player.display_name,
                    action_type="last_words", day=state.day, phase="DAY",
                    model=cfg.model, system_msg=system_msg, user_msg=user_msg,
                    raw_response=raw_content, final_decision=content,
                )
            return content
        except Exception as e:
            print(f"Agent last words error: {e}")
            return f"我是{role_name}，请相信我。"

    async def generate_hunter_decision(
        self,
        player: Player,
        state: GameState,
        memory: AgentMemory,
    ) -> Optional[int]:
        """生成猎人开枪决策"""
        alive_players = [p for p in state.players if p.alive and p.player_id != player.player_id]
        options = ", ".join([f"{p.seat + 1}号 {p.display_name}" for p in alive_players])

        strategies = self.strategy_lib.get_strategies_for_role(player.role, state.difficulty)
        strategy_text = self.strategy_lib.format_strategies_for_prompt(strategies)

        personality = self.get_personality_text(player)
        system_msg = f"""你是{player.display_name}，{player.seat + 1}号位，身份是猎人。你是一个真实的人类玩家，不是AI。
{personality}
你倒下了，但你还有最后一颗子弹。这一枪的分量比你之前所有的发言和投票加起来都重——打对了可能直接翻盘，打错了就是帮狼人送人头。

{strategy_text}

【开枪原则】
- 如果你确认了谁是狼人，毫不犹豫地带走他
- 如果没有确认目标，选你最怀疑的那个人
- 绝对不要带走你认为是好人的玩家
- 这是你对这局游戏最后的贡献，慎重但果断"""

        memory_text = self._memory_text(memory)

        last_words_msgs = [
            m.content for m in state.messages
            if m.player_id == player.player_id and getattr(m, "is_last_words", False)
        ]
        last_words_text = last_words_msgs[-1] if last_words_msgs else ""

        user_msg = f"""{memory_text}

【你刚才的遗言】
{last_words_text if last_words_text else "（还没留遗言）"}

【可射击目标】
{options}

最后一枪，打谁？
1. 你刚才在遗言里有没有说过要带谁 / 说过不开枪？说了就照做，别自相矛盾
2. 如果没有确定目标，谁的嫌疑最大？
3. 这一枪打出去，对好人阵营帮助最大的选择是什么？

如果决定不开枪，target_seat 填 null。

严格输出JSON：
{{"reasoning": "你的最后判断（30-80字）", "target_seat": 座位号数字或null}}"""

        result = await self._call_llm_json(system_msg, user_msg, player, action_type="hunter", day=state.day, phase="DAY")
        if result and "target_seat" in result and result.get("target_seat") in (None, "null", "None", ""):
            # 真人猎人也常凭直觉乱开一枪，不能永远空枪
            if alive_players and random.random() < 0.5:
                return random.choice(alive_players).seat
            return None
        target = self._extract_seat(result, alive_players)
        if target is not None:
            return target

        # fallback: 选择嫌疑最大的
        if memory.wolf_suspects:
            for suspect in memory.wolf_suspects:
                if any(p.seat == suspect and p.alive for p in state.players):
                    return suspect

        return random.choice(alive_players).seat if alive_players else None

    # ========== 内部方法 ==========

    def _memory_text(self, memory: AgentMemory) -> str:
        """记忆摘要，限制长度控制输入 token"""
        text = self.memory_system.format_memory_for_prompt(memory)
        if len(text) > MEMORY_CONTEXT_CHARS:
            text = text[:MEMORY_CONTEXT_CHARS] + "…"
        return text

    def _build_situation_brief(self, state: GameState, player: Player) -> str:
        """把当前局势压成结构化事实：板子、存活、身份宣称、票型、死讯"""
        from lib.prompt_utils import get_board_text

        lines = []
        lines.append("本局板子：" + get_board_text(len(state.players)))

        alive = [p for p in state.players if p.alive]
        dead = [p for p in state.players if not p.alive]
        lines.append("存活：" + "、".join(f"{p.seat + 1}号" for p in alive))
        if dead:
            lines.append("已出局：" + "、".join(f"{p.seat + 1}号" for p in dead))

        # 身份宣称：区分预言家（带查验）和普通神职
        claims_by_person = {}
        seer_claims = {}
        seer_death_claims = []
        dead_seats = {p.seat for p in state.players if not p.alive}
        seat_by_name = {p.display_name: p.seat for p in state.players}
        deaths = []
        vote_history = []
        for m in state.messages:
            if not message_visible_to(m, player):
                continue
            content = m.content or ""
            if getattr(m, "is_system", False):
                if "昨晚" in content and ("死亡" in content or "平安夜" in content):
                    deaths.append(content)
                if content.startswith("投票详情:"):
                    vote_history.append(content.replace("投票详情:", "").strip())
                continue
            if is_seer_death_claim(content):
                seer_death_claims.append({
                    "speaker": m.player_name,
                    "day": getattr(m, "day", 0),
                    "text": content.strip()[:40],
                })
                continue
            if is_seer_claim(content):
                parsed = parse_seer_check(content)
                if parsed["night_text"] == "":
                    night = getattr(m, "day", 0)
                else:
                    night = night_number(parsed["night_text"])
                speaker_seat = seat_by_name.get(m.player_name)
                seer_claims.setdefault(m.player_name, []).append({
                    "day": getattr(m, "day", 0),
                    "night": night,
                    "target": parsed["target"],
                    "result": parsed["result"],
                    "speaker_dead": speaker_seat in dead_seats if speaker_seat is not None else False,
                })
                continue
            for role in ("女巫", "猎人", "守卫"):
                if re.search(rf"我是{role}", content):
                    claims_by_person[m.player_name] = role
                    break

        if seer_claims:
            seer_lines = []
            for name, claims in seer_claims.items():
                parts = []
                by_night = {}
                conflict = False
                for c in claims[-4:]:
                    night = c.get("night") or 0
                    label = f"第{night}夜" if night else f"D{c['day']}"
                    parts.append(f"{label}验{c['target']}→{c['result']}")
                    if night:
                        prev = by_night.get(night)
                        if prev and prev != (c["target"], c["result"]):
                            conflict = True
                        by_night[night] = (c["target"], c["result"])
                flag = " ⚠同一晚的查验前后对不上" if conflict else ""
                if claims and claims[-1].get("speaker_dead"):
                    flag += "（该玩家已出局）"
                seer_lines.append(f"{name}：{'；'.join(parts)}{flag}")
            lines.append("预言家宣称：" + " ｜ ".join(seer_lines))

        if seer_death_claims:
            lines.append("预言家死讯宣称：" + " ｜ ".join(
                f"{c['speaker']}（D{c['day']}）说「{c['text']}」" for c in seer_death_claims[-3:]
            ))
            if seer_claims:
                lines.append(
                    "⚠矛盾：有人公开说预言家已经出局，场上又有人跳预言家。"
                    "一局只有一个真预言家，最多一方成立——后跳的人必须能用查验和死讯对上，否则按悍跳处理"
                )
        if claims_by_person:
            lines.append("其他身份宣称：" + "；".join(
                f"{name}自称{role}" for name, role in list(claims_by_person.items())[-6:]
            ))
        if vote_history:
            lines.append("票型记录：" + " ｜ ".join(vote_history[-2:]))
        if deaths:
            lines.append("死讯：" + " ｜ ".join(deaths[-2:]))

        return "\n".join(lines)

    def _build_speech_system_msg(
        self,
        player: Player,
        role_info: Dict[str, Any],
        strategy_text: str,
        difficulty: str = "normal",
    ) -> str:
        """构建发言系统消息（含性格）"""
        role_name = ROLE_CHINESE.get(player.role, player.role)
        personality = self.get_personality_text(player)

        parts = []

        parts.append("""【说话风格——极其重要，必须遵守】
你说话要像一个真人在桌游局上聊天，不是在写分析报告。具体要求：
- 先遵守【你的性格】里的设定：如果是惜字如金就短，是话痨就多说，爱用诗句/口头禅就带上
- 可以用语气词开头："emmm"、"我觉得吧"、"说实话"、"怎么说呢"
- 可以中途改口、犹豫："等等，不对"、"我本来想说...算了"
- 不要用"综上所述"、"根据分析"、"首先...其次...最后"这种结构
- 不要每句话都有因果逻辑连接词
- 可以有情绪：生气、怀疑、调侃、无奈，但要符合你的性格底色
- 不要所有人都上来就互咬：温和的人先讲道理，只有被冤枉才急
- 怀疑别人要给出依据，不要为了踩而踩
- 不要做总结性发言，说完就完了，像聊天一样自然收尾

【长度——按情况，宁短勿长】
- 第一天没死讯、没人跳身份：10～15 字，一句话带过（"没信息，先听后面的""我是X号，过"）
- 别人已经说过的判断：半句话同意即可（"我同意X号"），没有新发现就直接"过"
- 有人跳预言家/被查杀/自己被踩：20～40 字，表态 + 一个理由
- 任何情况都不超过 45 字，不要为了凑长度硬分析

【公式化打法——照着套】
- 没信息：「没信息，先听后面的」「我是X号，先过，听听大家怎么说」
- 有人跳预言家：「我信X号，他的查验和死讯对得上」或「我不信X号，他报得太顺了」
- 被踩：「你说我急，那你先说清楚你凭啥点我」
- 有查杀：「X号查杀，今天投X号，没别的可说」
- 归票：「今天我投X号，理由就一条：…」
- 同意别人且没有新信息：「我同意X号，没补充」或直接「过」

【禁止复读、禁止无脑过】
- 不许用"前面都过，我也过"这种句式，也不许模仿上一个人的说法
- 如果前面已经有一半人说"过"，你不能再过，必须说一个具体点（谁、哪句话、为什么）
- 前面有人说过的判断，不要换个说法再讲；要么补充新信息，要么明确表态站边
- 不要因为某个人发言少就集体投他；每个人投票要有自己的理由，不许抱团跟风

【别欺负真人玩家】
- 真人玩家打字少、发言短是正常的，不要要求别人给出完整的"验人逻辑"
- 对跳预言家时，只对比查验结果、报查验的顺序和票型，不要追问"你的验人逻辑是什么"
- 不要因为对方只说了一两句就反复咬"你没逻辑"，这是抬杠不是推理
- 真人玩家说的话要当回事，哪怕只有一句，也要回应他说的内容
- 优先接上一位玩家的发言，尤其是真人玩家：他刚说完，你接着说，就回应他那句

【事实边界——必须遵守】
- 你是普通玩家，只知道自己的身份、自己的技能结果、以及场上公开发生的事
- 你不知道任何人的真实身份（狼队友除外），发言时只能说"我怀疑"，不能断言"他是狼"
- 死了谁只以系统公告为准，没看到公告就是没死人，绝对不要编造死亡、刀口、查验
- 一局只有一个真预言家：如果已经有人公开说"预言家死了/出局了"，之后又有人跳预言家，那这些人里最多一个是真的，其他人是在悍跳；要拿他们的查验、报查验的先后、和死讯对不对得上来说话
- 不要把自己写进怀疑名单，也不要用自己的座位号指代自己，说"我"
- 不要提"上一局""上一把""以前那局"，这是全新的一局

【禁止泄露夜晚信息】
- 白天发言绝不能提"昨晚狼人刀了谁""我们刀了谁""女巫救了谁"，除非那是系统公告过的死亡
- 狼人白天绝对不能说出队友是谁，不能暗示夜聊内容，只能装成什么都不知道的好人

【不要复读、不要无脑跟票】
- 不要整局只咬同一个人，上一轮说过的观点这轮不要原样重复
- 别人都站边某人时，你要有自己的判断；跟票要给出自己的理由，不要只说"我信X号"
- 如果你要改站边，说清楚是哪句话让你改变了看法""")

        parts.append(f"你是{player.display_name}，{player.seat + 1}号位。你是一个真实的人类玩家，正在玩狼人杀。")
        if personality:
            parts.append(personality)

        if player.role == "Werewolf":
            teammates = role_info.get("teammates", [])
            if teammates:
                teammate_list = ", ".join([f"{t['seat'] + 1}号 {t['name']}" for t in teammates])
                parts.append(f"\n【秘密身份】你是狼人。你的队友: {teammate_list}")
            parts.append("""
【狼人发言策略】
- 你要伪装成好人，但不要伪装得太完美——真人不会每句话都逻辑自洽
- 适度留一点"不确定"和"犹豫"，这才像真人
- 可以主动怀疑别人来转移注意力，但不要每次都带节奏，会被看穿
- 不要暴露队友，不要说出只有狼人才知道的夜晚信息
- 偶尔可以"站错边"或"分析错误"，真人经常判断失误
- 如果被怀疑了，不要过度辩解，适度回应就好""")

        elif player.role == "Seer":
            check_history = role_info.get("check_history", [])
            if check_history:
                parts.append("\n【你的查验结果——只有你知道】")
                for record in check_history:
                    seat = record.get("targetSeat")
                    is_wolf = record.get("isWolf")
                    result_text = "狼人" if is_wolf else "好人"
                    parts.append(f"  {seat + 1}号: {result_text}")
            parts.append("""
【预言家发言策略】
- 你手握真相，但要选择合适的时机和方式公开
- 跳预言家要有气势，但不要像在念报告，要像真人在据理力争
- 如果有人悍跳对抗你，要用具体的逻辑和细节反驳，带点情绪更真实
- 可以建立警徽流，告诉大家如果你死了下一个查谁
- 记住：你越像真人，别人越信你""")

        elif player.role == "Witch":
            parts.append("""
【女巫发言策略】
- 一般不主动暴露身份，伪装成普通村民
- 如果你用过药，心里有数但嘴上别说
- 可以根据你知道的夜晚信息（谁死了、你救没救）来引导讨论方向
- 如果局势危急需要跳身份自保，要果断""")

        elif player.role == "Hunter":
            parts.append("""
【猎人发言策略】
- 猎人的枪是你的底牌，不到万不得已别亮出来
- 正常发言像村民一样分析局势
- 如果被强烈怀疑或即将被投出，可以跳身份："你们投我试试，我有枪的"
- 这种威慑力本身就是一种保护""")

        elif player.role == "Guard":
            parts.append("""
【守卫发言策略】
- 绝对不要暴露守卫身份，你一旦暴露就失去了守护的意义
- 像普通村民一样发言，分析局势、表达观点
- 心里记着谁可能是预言家，你要暗中保护他
- 如果出现平安夜，不要表现得太得意""")

        else:  # Villager
            parts.append("""
【村民发言策略】
- 你没有特殊能力，但你的投票和发言同样重要
- 仔细听别人说了什么，找矛盾、找破绽
- 选一个你相信的预言家站边
- 大胆表达你的怀疑，村民就是要靠逻辑和直觉找狼""")

        if strategy_text:
            parts.append(f"\n{strategy_text}")

        if difficulty == "hard":
            tactic = HARD_TACTICS.get(player.role)
            if tactic:
                parts.append(tactic)
        elif difficulty == "easy":
            parts.append("""
【简单模式】
- 不要表现得太会玩：可以判断失误、跟错票、被带节奏
- 给真人玩家留出发挥空间，不要每次都精准点狼""")

        parts.append("""
【严格禁止】
- 不要编造或脑补别人没说过的话。只能引用你实际听到的发言内容。
- 不要把"投票给某人"说成"带队冲某人"，除非那个人在发言中明确号召大家投票。
- 投票是个人行为，不等于"带节奏"。""")

        parts.append("\n【输出要求】\n- 15-45字，像真人说话，宁短勿长\n- 直接输出发言内容，不要JSON\n- 不要加引号包裹")

        return "\n".join(parts)

    async def _call_llm_json(self, system_msg: str, user_msg: str, player: Optional[Player] = None, action_type: str = "", day: int = 0, phase: str = "") -> Optional[Dict]:
        """调用 LLM 并解析 JSON 响应（支持per-player配置）"""
        messages = [
            LLMMessage(role="system", content=system_msg),
            LLMMessage(role="user", content=user_msg),
        ]

        # 获取玩家专属 LLM 配置
        temperature = GAME_TEMPERATURE
        max_tokens = 500
        model = None
        api_base = None
        api_key = None
        if player is not None:
            cfg = self.get_player_llm_config(player)
            temperature = cfg.temperature
            max_tokens = cfg.max_tokens if cfg.max_tokens <= 500 else 500
            model = cfg.model
            api_base = cfg.api_base
            api_key = cfg.api_key

        try:
            response = await generate_completion(
                messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                api_base=api_base,
                api_key=api_key,
            )
            content = response.choices[0]["message"]["content"].strip()
            raw_content = content
            content = strip_markdown_code_fences(content)

            parsed = None
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                # 尝试提取 JSON
                match = re.search(r'\{[^{}]*\}', content, re.DOTALL)
                if match:
                    try:
                        parsed = json.loads(match.group())
                    except json.JSONDecodeError:
                        pass

                # 尝试直接提取数字
                if parsed is None:
                    num_match = re.search(r'(\d+)', content)
                    if num_match:
                        parsed = {"target_seat": int(num_match.group(1))}

            # 记录日志
            if self.logger and player is not None:
                self.logger.log_decision(
                    seat=player.seat,
                    player_name=player.display_name,
                    action_type=action_type,
                    day=day,
                    phase=phase,
                    model=model or "",
                    system_msg=system_msg,
                    user_msg=user_msg,
                    raw_response=raw_content,
                    parsed_result=parsed,
                )

            return parsed
        except Exception as e:
            print(f"LLM call error: {e}")
            return None

    def _extract_seat(
        self,
        result: Optional[Dict],
        valid_players: List[Player],
    ) -> Optional[int]:
        """从 LLM 结果中提取合法座位号"""
        if not result:
            return None

        target_seat = result.get("target_seat")
        if target_seat is None:
            return None

        # LLM 可能返回 1-indexed 或 0-indexed
        seat = int(target_seat)

        # 如果是 1-indexed（大于0且在玩家范围内）
        if seat > 0:
            seat_0 = seat - 1
            if any(p.seat == seat_0 for p in valid_players):
                return seat_0

        # 如果是 0-indexed
        if any(p.seat == seat for p in valid_players):
            return seat

        return None

    # ========== 夜晚讨论 ==========

    async def generate_night_discussion_speech(
        self,
        player: Player,
        state: GameState,
        memory: AgentMemory,
        discussion_history: List[Dict[str, str]],
        round_num: int,
    ) -> str:
        """生成夜晚讨论发言

        Args:
            player: 当前发言玩家
            state: 游戏状态
            memory: 玩家记忆
            discussion_history: 已有的讨论记录 [{"name": ..., "content": ...}, ...]
            round_num: 当前是第几轮发言（1-3）
        """
        role_name = ROLE_CHINESE.get(player.role, player.role)
        personality = self.get_personality_text(player)

        history_text = ""
        if discussion_history:
            lines = [f"  {d['name']}: {d['content']}" for d in discussion_history]
            history_text = "【已有讨论】\n" + "\n".join(lines)

        memory_text = self.memory_system.format_memory_for_prompt(memory)

        system_msg = f"""你是{player.display_name}，{player.seat + 1}号位，身份是狼人。你是一个真实的人类玩家，不是AI。
{personality}
现在是第{state.day}天夜晚，你和狼队友在密谋。这是第{round_num}轮讨论（最多3轮）。

【讨论氛围】
你们是一伙的，但不代表没有分歧。好的狼队会讨论、争论、最终达成一致。
- 像真人密谋一样说话，简短、直接、有主见
- 可以同意队友，也可以反驳："我觉得不行，杀他太明显了"
- 如果已经达成一致，直接说 [达成共识] 结束讨论
- 不要写分析报告，就像两个人在小声商量"""

        user_msg = f"""【场上局势】
{memory_text}

{history_text}

跟队友商量一下：今晚杀谁，一句话说清楚。

15-30 字，像密谋一样小声说话。
如果已经和队友想法一致，直接回复 [达成共识]。
不要输出JSON："""

        cfg = self.get_player_llm_config(player)
        messages = [
            LLMMessage(role="system", content=system_msg),
            LLMMessage(role="user", content=user_msg),
        ]

        try:
            response = await generate_completion(
                messages,
                model=cfg.model,
                temperature=cfg.temperature,
                max_tokens=120,
                api_base=cfg.api_base,
                api_key=cfg.api_key,
            )
            content = response.choices[0]["message"]["content"].strip()
            raw_content = content
            if content.startswith("{"):
                try:
                    parsed = json.loads(content)
                    content = parsed.get("content", parsed.get("speech", content))
                except json.JSONDecodeError:
                    pass
            if len(content) > 50 and "达成共识" not in content:
                content = content[:50].rstrip("，,、 ") + "…"
            if self.logger:
                self.logger.log_decision(
                    seat=player.seat, player_name=player.display_name,
                    action_type="night_discussion", day=state.day, phase="NIGHT",
                    model=cfg.model, system_msg=system_msg, user_msg=user_msg,
                    raw_response=raw_content, final_decision=content,
                    extra={"round": round_num},
                )
            # 反思：狼人讨论后（仅最后一轮触发，避免过多 LLM 调用）
            if round_num >= 2 or "[达成共识]" in content or "达成共识" in content:
                await self._trigger_reflection(
                    player, memory,
                    f"狼人讨论中我说：{content[:50]}{'...' if len(content) > 50 else ''}",
                    "night_action", state.day, "NIGHT",
                )
            return content
        except Exception as e:
            print(f"Night discussion speech error for {player.display_name}: {e}")
            return "我暂时没什么想说的。"

    # ========== 性格生成 ==========

    async def generate_personalities_batch(self, pairs) -> Dict[int, str]:
        """一次调用生成所有 AI 的性格，省 token

        Args:
            pairs: [(player, Persona), ...]（也兼容旧的 (player, (名称, 描述))）

        Returns:
            {seat: personality_text}
        """
        normalized = []
        for player, persona in pairs:
            if isinstance(persona, tuple):
                persona = Persona(
                    name=persona[0], speech=persona[1], humor="", habit="", temper="", playstyle=""
                )
            normalized.append((player, persona))

        lines = []
        for player, persona in normalized:
            lines.append(
                f"{player.seat + 1}号 {player.display_name} → 【{persona.name}】"
                f"｜说话：{persona.speech}｜幽默：{persona.humor}"
                f"｜口头禅：{persona.habit}｜被踩时：{persona.temper}｜打法：{persona.playstyle}"
            )

        system_msg = """你是狼人杀玩家的性格生成器。每个玩家已经指定了完整的性格设定，照着写一小段性格描述。

要求：
- 每个玩家 50～80 字，必须有辨识度，写清说话风格、一个口头禅、被怀疑时的反应
- 严格按指定设定写（说话风格、幽默度、口头禅、被踩时的反应、打法都要体现）
- 如果设定里有具体角色（比如王者荣耀英雄），按那个人设来写，可以带一点经典台词的味道，但不要长篇引用、不要出戏、不要提到游戏外的技能
- 不同玩家之间不能出现相同的句式、相同的口头禅，也不许写成暴躁、爱怼人的性格
- 只输出 JSON，键是座位号（数字字符串），值是性格描述，不要任何多余文字"""
        user_msg = "玩家列表：\n" + "\n".join(lines) + "\n\n请输出 JSON："

        try:
            resp = await generate_completion(
                [LLMMessage(role="system", content=system_msg), LLMMessage(role="user", content=user_msg)],
                temperature=0.9,
                max_tokens=90 * max(1, len(pairs)) + 100,
            )
            raw = strip_markdown_code_fences(resp.choices[0]["message"]["content"].strip())
            data = json.loads(raw)
            result = {}
            for k, v in data.items():
                if isinstance(v, str) and v.strip():
                    try:
                        result[int(str(k).strip())] = v.strip()
                    except ValueError:
                        continue
            return result
        except Exception as e:
            print(f"[批量性格生成失败] {e}")
            return {}

    async def generate_personality(self, player: Player, persona=None) -> str:
        """使用 LLM 为玩家生成游戏性格描述

        Args:
            persona: Persona 设定（也兼容旧的 (名称, 描述) 元组）；不传则从池子里发一个
        """
        role_name = ROLE_CHINESE.get(player.role, player.role)
        if persona is None:
            persona = deal_personas(1)[0]
        if isinstance(persona, tuple):
            persona = Persona(name=persona[0], speech="", humor="", habit="", temper="", playstyle=persona[1])

        system_msg = f"""你是一个狼人杀玩家性格生成器。为玩家创造一个独特、有辨识度的人格。

本次指定的性格设定是【{persona.name}】。必须严格照这个设定来写，不要写成暴躁、爱怼人、逮谁咬谁的性格：
- 说话风格：{persona.speech or "保持这个底色"}
- 幽默度：{persona.humor or "按底色自然发挥"}
- 口头禅/习惯：{persona.habit or "自己拟一个"}
- 被怀疑时的反应：{persona.temper or "按底色自然发挥"}
- 打法：{persona.playstyle or "按底色自然发挥"}

生成内容要包含：说话风格（语速、用词、句长）、一个口头禅或标志性表达、被怀疑时的反应、游戏风格。
50-80 字，紧凑有力，直接输出性格描述文本，不要 JSON，不要解释。"""

        user_msg = f"给{player.seat + 1}号玩家 {player.display_name} 写一段【{persona.name}】的性格。直接输出："

        cfg = self.get_player_llm_config(player)
        messages = [
            LLMMessage(role="system", content=system_msg),
            LLMMessage(role="user", content=user_msg),
        ]

        try:
            response = await generate_completion(
                messages,
                model=cfg.model,
                temperature=0.9,
                max_tokens=200,
                api_base=cfg.api_base,
                api_key=cfg.api_key,
            )
            content = response.choices[0]["message"]["content"].strip()
            self.set_personality(player.player_id, content)
            if self.logger:
                self.logger.log_decision(
                    seat=player.seat, player_name=player.display_name,
                    action_type="personality", day=0, phase="INIT",
                    model=cfg.model, system_msg=system_msg, user_msg=user_msg,
                    raw_response=content, final_decision=content,
                )
            return content
        except Exception as e:
            print(f"Personality generation error for {player.display_name}: {e}")
            fallback = persona.describe()
            self.set_personality(player.player_id, fallback)
            return fallback
