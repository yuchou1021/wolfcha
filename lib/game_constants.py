"""Game constants and configuration."""

# Delay configurations (in milliseconds)
class DELAY_CONFIG:
    """Timing configurations for game flow."""
    DIALOGUE = 2000  # General dialogue delay
    LONG = 3000  # Long delay for important messages
    NIGHT_RESOLVE = 2000  # Night resolution delay


# Game configuration
class GAME_CONFIG:
    """General game configuration."""
    MAX_REVOTE_COUNT = 3  # Maximum number of wolf revotes


# 省钱开关：反思只在关键节点触发（跳预言家/被查杀/出局），不再每句话都反思
ENABLE_REFLECTION = True
IMPORTANT_REFLECTION_ONLY = True
REFLECTION_SAMPLE = 2  # 每次关键事件最多让 2 个 AI 反思

# 每次拼给模型的历史长度上限（字符），控制输入 token
SPEECH_CONTEXT_CHARS = 2200
VOTE_CONTEXT_CHARS = 2600
# 记忆里【已死亡玩家】排在【玩家分析】后面，太短会被截掉，AI 就会说"没人死"
MEMORY_CONTEXT_CHARS = 1400
BRIEF_CONTEXT_CHARS = 600

# Temperature for AI decisions
GAME_TEMPERATURE = 0.7
