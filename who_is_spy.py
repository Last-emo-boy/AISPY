import streamlit as st
import openai
import os
import re
import random
from faker import Faker

fake = Faker()

# 如果你在环境变量里设了OPENAI_API_KEY，此处留空或省略即可
# 否则就在这里写（明文不安全，建议仅在测试时使用，我就写着了，回头我删了就行）
openai.api_key = os.getenv("OPENAI_API_KEY", "sk-d64ff9a1f79349378eac006cbda57f55")

# (可选) 如果需要走代理/自定义 Endpoint，在此修改，我用的DS：
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
        st.session_state.public_messages = {}     # {name: "...上一轮公开发言..."}
        st.session_state.active_players = []      # 当前还存活(未被淘汰)的玩家列表(按索引/名字都可)
        st.session_state.round_index = 0

        # 卧底/词汇
        st.session_state.spy_index = None
        st.session_state.normal_word = ""
        st.session_state.spy_word = ""

        # 结果
        st.session_state.winner = None

        # 公共聊天记录 (列表[(speaker, public_text), ...])
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
    2) 若 word_option= "用户提供"，则直接用 user_normal_word / user_spy_word
       若 word_option= "AI GM自动"，则先让GM生成(含<think>)，并在公开部分给出 normal_word=..., spy_word=...
    3) 随机指定1位玩家做卧底
    4) 给GM、卧底玩家、普通玩家分别下发system prompt
    5) 初始化 active_players
    """

    # 先重置
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


    # 生成角色：GM + num_players
    gm_name = "GM_" + generate_random_name()
    player_names = ["Player_" + generate_random_name() for _ in range(num_players)]
    st.session_state.agent_names = [gm_name] + player_names

    # 给每个人初始化空的对话列表 & public message
    for name in st.session_state.agent_names:
        st.session_state.conversations[name] = []
        st.session_state.public_messages[name] = ""

    # 处理“词汇来源”：
    if word_option == "用户提供":
        # 直接从用户输入使用
        st.session_state.normal_word = user_normal_word.strip()
        st.session_state.spy_word = user_spy_word.strip()

        # 给GM 先放一个最简单的system
        GM_SYSTEM_PROMPT = f"""你是游戏主持人(GM)，名字叫{{agent_name}}。
本局单词由用户指定：
- 普通玩家：{st.session_state.normal_word}
- 卧底：{st.session_state.spy_word}
你的职责：引导游戏继续。
在回答中使用<think>...</think>写私有思考。
"""
        st.session_state.conversations[gm_name].append({"role": "system", "content": GM_SYSTEM_PROMPT.format(agent_name=gm_name)})

    else:
        # 由AI GM自动生成词
        # Step 1: 让GM在回答中产生 normal_word=..., spy_word=...
        gm_init_system_prompt = f"""你是游戏主持人(GM)，名字叫 {gm_name}。
请想出两个相似但不同的词汇：一个给普通玩家，一个给卧底玩家。
必须在公开部分最后一行写：normal_word=XXX, spy_word=YYY
可用 <think>...</think> 表示私有思考。
"""
        st.session_state.conversations[gm_name].append({"role": "system", "content": gm_init_system_prompt})
        st.session_state.conversations[gm_name].append({"role": "user", "content": "请生成两个相似的词，并按格式输出。"})
        gm_reply_text = generate_reply(st.session_state.conversations[gm_name])
        st.session_state.conversations[gm_name].append({"role": "assistant", "content": gm_reply_text})

        # 展示在前端(可展开查看)
        with st.expander("[GM自动生成词] 完整回答(含<think>)"):
            st.write(gm_reply_text)

        # 解析
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

    # Step 2: 随机指定一位玩家为卧底（不含GM -> 下标1~num_players）
    st.session_state.spy_index = random.randint(1, num_players)

    # Step 3: 给所有角色插入最终System提示
    GM_PROMPT = f"""你是游戏主持人(GM)，名字叫{{agent_name}}。
本局共 {num_players} 位玩家 + 你（GM）。
有1位卧底，卧底拿到的词是“{st.session_state.spy_word}”，普通玩家拿到的词是“{st.session_state.normal_word}”。
你的职责：
- 每轮先引导玩家依次描述
- 然后让所有存活的玩家投票(含GM)
- 最高票者被淘汰(若平票则无人淘汰)
- 若淘汰者是卧底则平民胜利；若只剩2人(卧底+平民)则卧底胜利
在回答中使用 <think>...</think> 写私有思考。
"""

    SPY_PROMPT = f"""你是一名玩家，名字叫“{{agent_name}}”。
你是**卧底**！你的词是“{st.session_state.spy_word}”。
请隐藏你的真实词汇与身份，你可以在<think>...</think>中说真话，但公开部分要谨慎描述。
请描述，而且不能说“我是卧底”。不能和他人描述的内容相同或者雷同，可以欺骗。
投票时使用 `###Vote: 某某玩家` 或 `###Vote: None`。
"""

    NORMAL_PROMPT = f"""你是一名玩家，名字叫“{{agent_name}}”。
你是**普通玩家**！你的词是“{st.session_state.normal_word}”。
你需要揪出那个词不同的卧底。
请在<think>...</think>写下私有思考，公开部分仅给出模糊描述。
不能和他人描述的内容相同或者雷同。
投票时使用 `###Vote: 某某玩家` 或 `###Vote: None`。
"""

    for idx, name in enumerate(st.session_state.agent_names):
        if idx == 0:
            # GM
            sys_txt = GM_PROMPT.format(agent_name=name)
            # 如果用户自己指定了词汇，而GM没有进行“自动生成”，
            # 就把system prompt直接插到 conversation[gm_name] 的开头:
            st.session_state.conversations[name].insert(0, {"role": "system", "content": sys_txt})
        else:
            if idx == st.session_state.spy_index:
                st.session_state.conversations[name].append({"role": "system", "content": SPY_PROMPT.format(agent_name=name)})
            else:
                st.session_state.conversations[name].append({"role": "system", "content": NORMAL_PROMPT.format(agent_name=name)})

    # 初始化所有玩家都存活
    st.session_state.active_players = list(range(1, num_players+1))  # 包含GM(0) + N位玩家(1~N)

    st.success(f"游戏已创建：1位GM + {num_players}位玩家，其中1位是卧底。")


def run_one_round():
    """
    每一轮游戏流程：
    1) GM 发言(如果GM还存活的话)
    2) 存活的非GM玩家依次发言
    3) 存活的所有人投票
    4) 找到最高票者淘汰(若平票则无人淘汰)
    5) 若淘汰者是卧底 => 平民胜利; 若剩2人且卧底仍在 => 卧底胜利
    """
    
    if not st.session_state.game_inited:
        st.warning("游戏尚未初始化，无法进行下一轮。")
        return
    if st.session_state.game_over:
        st.warning("游戏已结束，若要重新开始请点击“开始游戏”")
        return

    st.session_state.round_index += 1

    # ========== 1) GM 发言 (若GM存活) ==========
    if 0 in st.session_state.active_players:
        gm_public = do_speak(0)
        add_chat_record(st.session_state.agent_names[0], gm_public)

    # ========== 2) 存活玩家依次发言：先存到一个临时字典 this_round_msgs = {name: 公开内容} ==========
    this_round_msgs = {}
    for idx in st.session_state.active_players:
        if idx == 0:
            continue
        pub_msg = do_speak(idx)
        add_chat_record(st.session_state.agent_names[idx], pub_msg)
        # 先只保存到 this_round_msgs，不立刻写到 public_messages
        name = st.session_state.agent_names[idx]
        this_round_msgs[name] = pub_msg

    # ========== 3) 统一更新 st.session_state.public_messages，让所有人看得到本轮全部发言 ==========
    for name, msg in this_round_msgs.items():
        st.session_state.public_messages[name] = msg

    # ========== 4) 让所有(存活的 + GM若存活)进行投票，此时他们会看到完整的本轮发言 ==========
    votes_map = {}
    for idx in st.session_state.active_players:
        # GM 可能也要投票
        if idx == 0 or idx in st.session_state.active_players:
            voted_person = do_vote(idx)
            votes_map[idx] = voted_person

    # ========== 5) 统计票数 & 淘汰 & 检查游戏结束 ==========
    eliminated = do_elimination(votes_map)
    check_game_end(eliminated)



def do_speak(player_idx):
    """
    让编号 player_idx 的角色发言 (含<think>)。
    整理【上一轮公开发言】作为User消息。
    """
    name = st.session_state.agent_names[player_idx]

    # 收集上一轮公开发言
    user_content = "【上一轮公开发言】\n"
    for other_idx in st.session_state.active_players:
        if other_idx == player_idx:
            continue
        other_name = st.session_state.agent_names[other_idx]
        public_msg = st.session_state.public_messages.get(other_name, "")
        if not public_msg:
            public_msg = "(无公开发言)"
        user_content += f"{other_name}: {public_msg}\n"

    user_content += "\n请你做本轮发言，用<think>...</think>写出私有思考。"

    # 放进对话上下文
    st.session_state.conversations[name].append({"role": "user", "content": user_content})
    reply_text = generate_reply(st.session_state.conversations[name])
    st.session_state.conversations[name].append({"role": "assistant", "content": reply_text})

    # 将发言(含<think>)展示在前端(可展开)
    private_thoughts, public_text = extract_think_and_public(reply_text)
    with st.expander(f"{name} 发言 (含<think>) - 第{st.session_state.round_index}轮"):
        st.write(reply_text)

    return public_text

def do_vote(player_idx):
    """
    让存活玩家投票 (含<think>)。
    返回 vote_target(是一个玩家姓名)，若未解析到则None
    """
    name = st.session_state.agent_names[player_idx]

    user_content = "请进行投票。使用 `###Vote: 某某玩家` 或 `###Vote: None`。"

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
    根据 votes_map ( {player_idx: "投给了某某玩家名字" or None} ) 来统计票数。
    - 找到票数最高的玩家姓名
    - 若有平票则无人淘汰
    - 若有人最高票，淘汰该玩家
    返回被淘汰的 player_idx，若无人淘汰则 None
    """
    # 先统计 [player_name -> 票数]
    # 这里存活玩家可能都在投票，但可能投给已被淘汰的人名 or 无效人名。
    # 我们要映射一下 "名字" -> idx
    name_to_idx = {st.session_state.agent_names[i]: i for i in st.session_state.active_players}
    count_map = {}

    for voter_idx, target_name in votes_map.items():
        if not target_name:
            continue
        # 如果投票目标在 name_to_idx 中，则为有效投票
        if target_name in name_to_idx:
            target_idx = name_to_idx[target_name]
            count_map[target_idx] = count_map.get(target_idx, 0) + 1

    if not count_map:
        st.info("本轮无人被投票或所有票无效 => 无人淘汰")
        return None

    # 找到最高票
    # 例如 count_map = {player_idx: 票数}
    sorted_items = sorted(count_map.items(), key=lambda x: x[1], reverse=True)
    top_idx, top_votes = sorted_items[0]

    # 检查是否平票
    # 若有多个并列最高，则不淘汰
    # (当第二名的票数 == top_votes，就算平票)
    if len(sorted_items) > 1:
        second_idx, second_votes = sorted_items[1]
        if second_votes == top_votes:
            st.info(f"出现平票，最高票数 {top_votes} 不是唯一 => 无人淘汰")
            return None

    # 否则淘汰 top_idx
    eliminated_idx = top_idx
    eliminated_name = st.session_state.agent_names[eliminated_idx]
    st.warning(f"**{eliminated_name} 被淘汰** (获得最高票数 {top_votes} )")
    st.session_state.active_players.remove(eliminated_idx)
    return eliminated_idx

def check_game_end(eliminated_idx):
    """
    若 eliminated_idx 是卧底 -> 平民胜利
    若 只剩2人 并且卧底还存活 -> 卧底胜利
    否则继续
    """
    if eliminated_idx is not None:
        if eliminated_idx == st.session_state.spy_index:
            # 卧底被淘汰 => 平民胜利
            st.success("卧底被淘汰！平民胜利！")
            st.session_state.game_over = True
            st.session_state.winner = "平民"
            return

    # 如果只剩2人 => 如果卧底还活着 => 卧底胜利，否则平民胜利
    if len(st.session_state.active_players) == 2:
        if st.session_state.spy_index in st.session_state.active_players:
            # 间谍还在 => 间谍赢
            st.warning("只剩2人存活(含卧底)，卧底胜利！")
            st.session_state.game_over = True
            st.session_state.winner = "卧底"
        else:
            # 间谍已不在
            st.success("只剩2人存活(卧底已被淘汰)，平民胜利！")
            st.session_state.game_over = True
            st.session_state.winner = "平民"


def add_chat_record(speaker_name, public_text):
    """
    将一条公开消息追加到公共聊天记录中。
    这样可在UI中像群聊一样直观查看。
    """
    if not public_text.strip():
        return
    st.session_state.public_chat_history.append((speaker_name, public_text))


# ========== Streamlit 界面 ==========

def main():
    st.set_page_config(page_title="谁是卧底 AI斗蛐蛐", layout="wide")

    st.title("谁是卧底 AI斗蛐蛐")
    st.markdown("""
    - 你可以选择自己提供「普通词/卧底词」，或让AI GM自动生成相似词汇。
    - **游戏流程**：
      1. 所有人(含GM)依次发言描述  
      2. 投票选出最可疑者，若平票则无人被淘汰  
      3. 若卧底被淘汰，平民胜利；若只剩2人时卧底仍在，则卧底胜利  

    - 在下方【公共聊天记录】中，会依次显示各玩家在本轮的公开发言（不含<think>）。
    - 若要查看完整对话（含<think>），请展开下面的【各AI完整对话记录】。
    """)

    # 初始化 session_state
    init_session_state()

    # ========== 左侧/下方：参数设置 ==========
    with st.sidebar:
        st.subheader("游戏 & 模型参数")

        # 玩家数量
        num_players = st.number_input("玩家数量(不含GM)", min_value=2, max_value=10, value=3, step=1)

        # 词汇来源：用户 or AI
        word_option = st.radio("谁来指定词汇？", ["用户提供", "AI GM自动"])

        user_normal_word = ""
        user_spy_word = ""
        if word_option == "用户提供":
            user_normal_word = st.text_input("普通玩家的词", value="苹果")
            user_spy_word = st.text_input("卧底玩家的词", value="梨子")

        st.markdown("---")

        # OpenAI参数 (Temperature / Top-p / Penalties)
        st.session_state.temperature = st.slider("Temperature (随机度)", 0.0, 2.0, st.session_state.temperature, 0.1)
        st.session_state.top_p = st.slider("Top-p (核采样)", 0.1, 1.0, st.session_state.top_p, 0.05)
        st.session_state.presence_penalty = st.slider("Presence Penalty", 0.0, 2.0, st.session_state.presence_penalty, 0.1)
        st.session_state.frequency_penalty = st.slider("Frequency Penalty", 0.0, 2.0, st.session_state.frequency_penalty, 0.1)

    # 两个按钮：开始游戏(重置) & 进行下一轮
    col1, col2 = st.columns(2)
    with col1:
        if st.button("开始游戏(重置)"):
            setup_game(num_players, word_option, user_normal_word, user_spy_word)

    with col2:
        # 只有游戏已初始化且尚未结束才能进行下一轮
        if st.session_state.game_inited and not st.session_state.game_over:
            if st.button("进行下一轮"):
                run_one_round()

    # 若游戏已初始化，展示信息
    if st.session_state.game_inited:
        st.write("---")
        st.write(f"**玩家(含GM)名单**: {st.session_state.agent_names}")

        # 获取卧底名 (若已确定)
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

        # 展示公共聊天记录（只显示公开内容）
        st.header("公共聊天记录 (仅公开内容)")
        if not st.session_state.public_chat_history:
            st.write("(暂无公共发言)")
        else:
            for speaker, msg in st.session_state.public_chat_history:
                st.markdown(f"**{speaker}**: {msg}")

        # 展示各AI完整对话(含<think>)
        st.header("各AI完整对话记录 (含<think>)")
        for name in st.session_state.agent_names:
            with st.expander(f"查看 {name} 的全部对话历史"):
                conversation = st.session_state.conversations[name]
                for i, c in enumerate(conversation):
                    st.write(f"**[{c['role']} {i}]**: {c['content']}")


if __name__ == "__main__":
    main()
