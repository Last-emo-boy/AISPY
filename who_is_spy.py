import streamlit as st
import openai
import os
import re
import random
from faker import Faker

fake = Faker()

# 如果你在环境变量里设了OPENAI_API_KEY，此处留空或省略即可
openai.api_key = os.getenv("OPENAI_API_KEY", "xxxxxxx")
# (可选) 如果需要走代理/自定义 Endpoint，在此修改：
openai.api_base = "https://api.deepseek.com/v1"

# ========== 全局工具函数 ==========

def generate_reply(messages):
    """
    调用OpenAI ChatCompletion接口，返回生成文本。
    这里会读取若干生成参数: temperature, top_p, presence_penalty, frequency_penalty
    """
    temp = st.session_state.get("temperature", 0.7)
    top_p = st.session_state.get("top_p", 1.0)
    presence_pen = st.session_state.get("presence_penalty", 0.0)
    freq_pen = st.session_state.get("frequency_penalty", 0.0)
    try:
        response = openai.ChatCompletion.create(
            model="deepseek-chat",
            messages=messages,
            temperature=temp,
            top_p=top_p,
            presence_penalty=presence_pen,
            frequency_penalty=freq_pen,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"[ERROR]: {str(e)}"

def extract_think_and_public(text):
    """
    从文本中提取 <think>...</think> (私有思考) 和公开部分。
    若未匹配到<think>，则返回 (None, text)。
    """
    pattern = r"<think>(.*?)</think>"
    match = re.search(pattern, text, re.DOTALL)
    if match:
        private_thoughts = match.group(1).strip()
        public_text = re.sub(pattern, "", text, count=1, flags=re.DOTALL).strip()
        return private_thoughts, public_text
    else:
        return None, text

def parse_vote_from_text(public_text):
    """
    固定投票格式: 在公开文本中使用 `###Vote: 某某玩家` 或 `###Vote: None`
    若没找到则返回 None
    """
    pattern = r"^###Vote:\s*(.+)$"
    lines = public_text.splitlines()
    for line in lines:
        line = line.strip()
        m = re.match(pattern, line, re.IGNORECASE)
        if m:
            return m.group(1)
    return None

def generate_random_name():
    """使用Faker生成一个随机人名"""
    return fake.name()

# ========== 初始化/重置 SessionState ==========

def init_session_state():
    """
    只在Session启动时调用一次。
    """
    if "initialized" not in st.session_state:
        st.session_state.initialized = True

        # 游戏控制
        st.session_state.game_inited = False
        st.session_state.game_over = False
        st.session_state.num_players = 0

        # 人员 & 对话
        st.session_state.agent_names = []
        st.session_state.conversations = {}      # {name: [ {role, content}, ...], ...}
        st.session_state.public_messages = {}     # {name: "上一轮公开发言"}
        st.session_state.active_players = []      # 当前存活玩家（仅玩家，下标 1~N）
        st.session_state.round_index = 0

        # 卧底/词汇
        st.session_state.spy_index = None
        st.session_state.normal_word = ""
        st.session_state.spy_word = ""

        # 结果
        st.session_state.winner = None

        # 公共聊天记录（跨轮累积，列表[(speaker, public_text), ...]）
        st.session_state.public_chat_history = []

        # 默认OpenAI生成参数
        st.session_state.temperature = 0.7
        st.session_state.top_p = 1.0
        st.session_state.presence_penalty = 0.0
        st.session_state.frequency_penalty = 0.0

# ========== 主要游戏流程函数 ==========

def setup_game(num_players, word_option, user_normal_word="", user_spy_word=""):
    """
    初始化游戏逻辑：
    1) 重置状态
    2) 根据 word_option 使用用户提供词汇或让AI GM生成(含<think>)，解析后存入session_state
    3) 随机指定1位玩家为卧底（注意：active_players只包含玩家，下标 1~num_players）
    4) 给GM、卧底玩家、普通玩家分别下发 system prompt
    5) 初始化 active_players
    """
    # 重置状态
    st.session_state.game_inited = True
    st.session_state.game_over = False
    st.session_state.round_index = 0
    st.session_state.num_players = num_players
    st.session_state.winner = None
    st.session_state.public_chat_history = []

    # 清空旧数据
    st.session_state.agent_names = []
    st.session_state.conversations = {}
    st.session_state.public_messages = {}
    st.session_state.spy_index = None
    st.session_state.normal_word = ""
    st.session_state.spy_word = ""
    st.session_state.active_players = []

    # 生成角色：GM + num_players个玩家
    gm_name = "GM_" + generate_random_name()
    player_names = ["Player_" + generate_random_name() for _ in range(num_players)]
    st.session_state.agent_names = [gm_name] + player_names

    # 初始化每个角色的对话列表
    for name in st.session_state.agent_names:
        st.session_state.conversations[name] = []
        st.session_state.public_messages[name] = ""

    # 处理词汇来源
    if word_option == "用户提供":
        st.session_state.normal_word = user_normal_word.strip()
        st.session_state.spy_word = user_spy_word.strip()
        GM_SYSTEM_PROMPT = f"""你是游戏主持人(GM)，名字叫{{agent_name}}。
本局单词由用户指定：
- 普通玩家：{st.session_state.normal_word}
- 卧底：{st.session_state.spy_word}
你的职责：引导游戏继续。
在回答中使用<think>...</think>写私有思考。
"""
        st.session_state.conversations[gm_name].append({"role": "system", "content": GM_SYSTEM_PROMPT.format(agent_name=gm_name)})
    else:
        # AI GM自动生成词汇
        gm_init_system_prompt = f"""你是游戏主持人(GM)，名字叫 {gm_name}。
请想出两个相似但不同的词汇：一个给普通玩家，一个给卧底玩家。
必须在公开部分最后一行写：normal_word=XXX, spy_word=YYY
可用 <think>...</think> 表示私有思考。
"""
        st.session_state.conversations[gm_name].append({"role": "system", "content": gm_init_system_prompt})
        st.session_state.conversations[gm_name].append({"role": "user", "content": "请生成两个相似的词，并按格式输出。"})
        gm_reply_text = generate_reply(st.session_state.conversations[gm_name])
        st.session_state.conversations[gm_name].append({"role": "assistant", "content": gm_reply_text})
        with st.expander("[GM自动生成词] 完整回答(含<think>)"):
            st.write(gm_reply_text)
        _, gm_public_text = extract_think_and_public(gm_reply_text)
        pattern = r"normal_word\s*=\s*(.*?),\s*spy_word\s*=\s*(.*)$"
        match = re.search(pattern, gm_public_text, re.IGNORECASE)
        if match:
            st.session_state.normal_word = match.group(1).strip()
            st.session_state.spy_word = match.group(2).strip()
        else:
            st.warning("未能解析出normal_word/spy_word，使用默认示例：苹果/梨子。")
            st.session_state.normal_word = "苹果"
            st.session_state.spy_word = "梨子"

    # 随机指定一位玩家为卧底（注意：active_players只包含玩家，下标 1~num_players）
    st.session_state.spy_index = random.randint(1, num_players)

    # 下发各角色的最终 system prompt
    GM_PROMPT = f"""你是游戏主持人(GM)，名字叫{{agent_name}}。
本局共 {num_players} 位玩家 + 你（GM）。
有1位卧底，卧底拿到的词是“{st.session_state.spy_word}”，普通玩家拿到的词是“{st.session_state.normal_word}”。
你的职责：引导玩家依次发言并统一收集投票。
请在回答中使用 <think>...</think> 写私有思考。
"""
    SPY_PROMPT = f"""你是一名玩家，名字叫“{{agent_name}}”。
你是**卧底**！你的词是“{st.session_state.spy_word}”。
请隐藏真实身份，不要直说“我是卧底”，描述时要与他人区分。
投票时请使用 `###Vote: 某某玩家` 或 `###Vote: None`。
"""
    NORMAL_PROMPT = f"""你是一名玩家，名字叫“{{agent_name}}”。
你是**普通玩家**！你的词是“{st.session_state.normal_word}”。
你的目标是揪出拿到不同词汇的卧底。
投票时请使用 `###Vote: 某某玩家` 或 `###Vote: None`。
"""

    for idx, name in enumerate(st.session_state.agent_names):
        if idx == 0:
            st.session_state.conversations[name].insert(0, {"role": "system", "content": GM_PROMPT.format(agent_name=name)})
        else:
            if idx == st.session_state.spy_index:
                st.session_state.conversations[name].append({"role": "system", "content": SPY_PROMPT.format(agent_name=name)})
            else:
                st.session_state.conversations[name].append({"role": "system", "content": NORMAL_PROMPT.format(agent_name=name)})

    # 初始化 active_players（仅玩家，下标 1~num_players）
    st.session_state.active_players = list(range(1, num_players+1))

    st.success(f"游戏已创建：1位GM + {num_players}位玩家，其中1位是卧底。")

def run_one_round():
    """
    每一轮游戏流程：
    1) 存活玩家依次发言（本轮发言的上下文累积）
    2) 存活玩家统一投票（基于本轮所有发言）
    3) 根据投票结果淘汰一人，并检查游戏是否结束
    """
    if not st.session_state.game_inited:
        st.warning("游戏尚未初始化，请先点击“开始游戏(重置)”")
        return
    if st.session_state.game_over:
        st.warning("游戏已结束，请点击“开始游戏(重置)”重新开始")
        return

    st.session_state.round_index += 1

    # 本轮发言上下文，保存为列表[(speaker, public_text), ...]
    current_round_context = []

    # 让所有存活玩家依次发言
    for idx in st.session_state.active_players:
        # 对每个玩家传入本轮已经发言的上下文
        public_msg = do_speak(idx, current_round_context)
        # 保存该玩家的发言到本轮上下文
        speaker = st.session_state.agent_names[idx]
        current_round_context.append((speaker, public_msg))
        # 同时更新该玩家的最新公开发言和公共聊天记录
        st.session_state.public_messages[speaker] = public_msg
        add_chat_record(speaker, public_msg)

    # 更新所有玩家的上下文：此时，每个玩家在投票时可看到完整本轮发言
    # 这里我们构造一个字符串，将本轮所有发言拼接起来
    full_round_context = "【本轮全部公开发言】\n"
    for speaker, msg in current_round_context:
        full_round_context += f"{speaker}: {msg}\n"

    # 让所有存活玩家依次投票，传入完整本轮上下文
    votes_map = {}
    for idx in st.session_state.active_players:
        vote = do_vote(idx, full_round_context)
        votes_map[idx] = vote

    # 根据投票结果进行淘汰
    eliminated = do_elimination(votes_map)
    check_game_end(eliminated)

def do_speak(player_idx, current_context):
    """
    让编号 player_idx 的角色发言 (含<think>)。
    其User消息中包含本轮已发言的上下文 current_context（列表形式）。
    """
    name = st.session_state.agent_names[player_idx]
    user_content = "【本轮前面玩家的公开发言】\n"
    if current_context:
        for speaker, msg in current_context:
            user_content += f"{speaker}: {msg}\n"
    else:
        user_content += "(本轮暂无其他发言)\n"
    user_content += "\n请你做本轮发言，用<think>...</think>写出私有思考。"

    st.session_state.conversations[name].append({"role": "user", "content": user_content})
    reply_text = generate_reply(st.session_state.conversations[name])
    st.session_state.conversations[name].append({"role": "assistant", "content": reply_text})

    private_thoughts, public_text = extract_think_and_public(reply_text)
    with st.expander(f"{name} 发言 (含<think>) - 第{st.session_state.round_index}轮"):
        st.write(reply_text)
    return public_text

def do_vote(player_idx, round_context):
    """
    让存活玩家投票 (含<think>)。
    传入 round_context (字符串形式的本轮全部公开发言)，作为投票前的上下文。
    返回投票目标（玩家姓名），若未解析到则返回 None。
    """
    name = st.session_state.agent_names[player_idx]
    user_content = "【本轮全部公开发言】\n" + round_context
    user_content += "\n请进行投票。使用 `###Vote: 某某玩家` 或 `###Vote: None` 表达你的投票。"

    st.session_state.conversations[name].append({"role": "user", "content": user_content})
    reply_text = generate_reply(st.session_state.conversations[name])
    st.session_state.conversations[name].append({"role": "assistant", "content": reply_text})

    private_thoughts, public_text = extract_think_and_public(reply_text)
    with st.expander(f"{name} 投票 (含<think>) - 第{st.session_state.round_index}轮"):
        st.write(reply_text)
    vote_target = parse_vote_from_text(public_text)
    if vote_target:
        st.markdown(f"**{name} 投给了：{vote_target}**")
    else:
        st.markdown(f"**{name} 未给出有效投票**")
    return vote_target

def do_elimination(votes_map):
    """
    根据 votes_map ( {player_idx: "投给了某某玩家" 或 None} ) 统计票数：
    - 找到票数最高的玩家
    - 若出现平票则无人淘汰
    - 否则淘汰票数最高者
    返回被淘汰的 player_idx（若无人淘汰则返回 None）。
    """
    # 将存活玩家姓名映射到下标
    name_to_idx = {st.session_state.agent_names[i]: i for i in st.session_state.active_players}
    count_map = {}
    for voter_idx, target_name in votes_map.items():
        if not target_name:
            continue
        if target_name in name_to_idx:
            target_idx = name_to_idx[target_name]
            count_map[target_idx] = count_map.get(target_idx, 0) + 1

    if not count_map:
        st.info("本轮无人有效投票 => 无人淘汰")
        return None

    sorted_items = sorted(count_map.items(), key=lambda x: x[1], reverse=True)
    top_idx, top_votes = sorted_items[0]
    if len(sorted_items) > 1 and sorted_items[1][1] == top_votes:
        st.info(f"出现平票，最高票数 {top_votes} 不是唯一 => 无人淘汰")
        return None

    eliminated_idx = top_idx
    eliminated_name = st.session_state.agent_names[eliminated_idx]
    st.warning(f"**{eliminated_name} 被淘汰** (获得最高票数 {top_votes})")
    st.session_state.active_players.remove(eliminated_idx)
    return eliminated_idx

def check_game_end(eliminated_idx):
    """
    检查游戏是否结束：
    - 若淘汰者是卧底，则平民胜利；
    - 若存活玩家只剩2人（且卧底仍在），则卧底胜利；
    否则游戏继续。
    """
    if eliminated_idx is not None and eliminated_idx == st.session_state.spy_index:
        st.success("卧底被淘汰！平民胜利！")
        st.session_state.game_over = True
        st.session_state.winner = "平民"
        return

    if len(st.session_state.active_players) == 2:
        if st.session_state.spy_index in st.session_state.active_players:
            st.warning("只剩2人存活(含卧底)，卧底胜利！")
            st.session_state.game_over = True
            st.session_state.winner = "卧底"
        else:
            st.success("只剩2人存活(卧底已被淘汰)，平民胜利！")
            st.session_state.game_over = True
            st.session_state.winner = "平民"

def add_chat_record(speaker_name, public_text):
    """
    将一条公开消息追加到公共聊天记录中。
    """
    if public_text.strip():
        st.session_state.public_chat_history.append((speaker_name, public_text))

# ========== Streamlit 界面 ==========

def main():
    st.set_page_config(page_title="谁是卧底 AI斗蛐蛐", layout="wide")

    st.title("谁是卧底 AI斗蛐蛐")
    st.markdown("""
    - 你可以选择自己提供「普通词/卧底词」，或让AI GM自动生成相似词汇。
    - **游戏流程**：
      1. 存活玩家依次发言（每位玩家可看到本轮前面玩家的公开发言）  
      2. 所有存活玩家统一投票（基于本轮所有公开发言）  
      3. 最高票者被淘汰（平票则无人淘汰），并检查游戏结束条件  
         - 若淘汰者为卧底，则平民胜利  
         - 若只剩2人且卧底仍在，则卧底胜利  
    - 下方【公共聊天记录】中显示本轮所有玩家的公开发言；【各AI完整对话记录】可查看详细上下文（含 `<think>`）。
    """)

    # 初始化 session_state
    init_session_state()

    # ========== 左侧：参数设置 ==========
    with st.sidebar:
        st.subheader("游戏 & 模型参数")
        num_players = st.number_input("玩家数量(不含GM)", min_value=2, max_value=10, value=3, step=1)
        word_option = st.radio("谁来指定词汇？", ["用户提供", "AI GM自动"])
        user_normal_word = ""
        user_spy_word = ""
        if word_option == "用户提供":
            user_normal_word = st.text_input("普通玩家的词", value="苹果")
            user_spy_word = st.text_input("卧底玩家的词", value="梨子")
        st.markdown("---")
        st.session_state.temperature = st.slider("Temperature (随机度)", 0.0, 2.0, st.session_state.temperature, 0.1)
        st.session_state.top_p = st.slider("Top-p (核采样)", 0.1, 1.0, st.session_state.top_p, 0.05)
        st.session_state.presence_penalty = st.slider("Presence Penalty", 0.0, 2.0, st.session_state.presence_penalty, 0.1)
        st.session_state.frequency_penalty = st.slider("Frequency Penalty", 0.0, 2.0, st.session_state.frequency_penalty, 0.1)

    col1, col2 = st.columns(2)
    with col1:
        if st.button("开始游戏(重置)"):
            setup_game(num_players, word_option, user_normal_word, user_spy_word)
    with col2:
        if st.session_state.game_inited and not st.session_state.game_over:
            if st.button("进行下一轮"):
                run_one_round()

    if st.session_state.game_inited:
        st.write("---")
        st.write(f"**玩家(含GM)名单**: {st.session_state.agent_names}")
        spy_name = "???"
        if (st.session_state.spy_index is not None and 
            st.session_state.spy_index < len(st.session_state.agent_names)):
            spy_name = st.session_state.agent_names[st.session_state.spy_index]

        if st.session_state.game_over:
            st.subheader("游戏结束!")
            st.write(f"本局卧底是：{spy_name}")
            st.write(f"获胜方：{st.session_state.winner}")
        else:
            st.write(f"**已经进行了 {st.session_state.round_index} 轮**")
            alive_names = [st.session_state.agent_names[i] for i in st.session_state.active_players]
            st.write(f"当前存活玩家：{alive_names}")

        st.header("公共聊天记录 (仅公开内容)")
        if not st.session_state.public_chat_history:
            st.write("(暂无公共发言)")
        else:
            for speaker, msg in st.session_state.public_chat_history:
                st.markdown(f"**{speaker}**: {msg}")

        st.header("各AI完整对话记录 (含<think>)")
        for name in st.session_state.agent_names:
            with st.expander(f"查看 {name} 的全部对话历史"):
                conversation = st.session_state.conversations[name]
                for i, c in enumerate(conversation):
                    st.write(f"**[{c['role']} {i}]**: {c['content']}")

if __name__ == "__main__":
    main()
