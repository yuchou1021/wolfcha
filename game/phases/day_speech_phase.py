"""Day speech phase implementation."""

from game.core.game_phase import GamePhase
from game.core.types import GameContext, GameAction, PromptResult, SystemPromptPart
from game_types.game_types import Player, GameState
from lib.prompt_utils import (
    build_game_context,
    build_difficulty_speech_hint,
    get_role_text,
    get_win_condition,
    build_system_text_from_parts,
    build_today_transcript,
)


class DaySpeechPhase(GamePhase):
    """Day speech phase - handles player speeches."""

    async def on_enter(self, context: GameContext) -> None:
        """Called when entering day speech phase."""
        pass

    def get_prompt(self, context: GameContext, player: Player) -> PromptResult:
        """Generate prompt for player speech."""
        state = context.state
        game_context = build_game_context(state, player)
        difficulty_hint = build_difficulty_speech_hint(state.difficulty)

        today_transcript = build_today_transcript(state, max_chars=10000, player=player)

        is_last_words = state.phase == "DAY_LAST_WORDS"
        is_badge_speech = state.phase == "DAY_BADGE_SPEECH"
        is_pk_speech = state.phase == "DAY_PK_SPEECH"

        task = "发表你的遗言" if is_last_words else "发表你的竞选演说" if (is_badge_speech or is_pk_speech) else "发言讨论"

        base_content = f"""你是 {player.seat + 1}号玩家 {player.display_name}。
你的身份是: {get_role_text(player.role)}
你的胜利条件: {get_win_condition(player.role)}
{difficulty_hint}"""

        task_content = f"""现在是白天{task}阶段。
请根据你的身份和当前局势进行发言。

硬性要求:
- 必须提到至少一个具体座位号（例如"3号发言有问题"），不要只说"没什么想说的"或空泛表态
- 给出你的判断和理由：谁像狼、谁像好人、今天建议投谁
- 预言家要报出查验结果（金水/查杀），必要时跳身份带队
- 狼人要伪装成好人：可以悍跳预言家、给队友发金水、踩好人，或倒钩真预言家
- 控制在 60～120 字，像真人语音发言，不要念稿
"""

        system_parts = [
            SystemPromptPart(text=base_content, cacheable=True, ttl="1h"),
            SystemPromptPart(text=task_content),
        ]
        system = build_system_text_from_parts(system_parts)

        user_content = f"""当前游戏状态:
{game_context}

今日发言记录:
{today_transcript if today_transcript else "（暂无发言）"}

请发表你的观点:"""

        return PromptResult(system=system, user=user_content, system_parts=system_parts)

    async def handle_action(self, context: GameContext, action: GameAction) -> None:
        """Handle day speech actions."""
        pass

    async def on_exit(self, context: GameContext) -> None:
        """Called when exiting day speech phase."""
        pass
