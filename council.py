import concurrent.futures
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# ============================================================
# 基础配置
# ============================================================

COUNCIL_DIR = Path(__file__).resolve().parent
GLM_SCRIPT = Path.home() / "glm-cli" / "glm.py"

COMMAND_TIMEOUT = 900
SESSION_DIR = COUNCIL_DIR / "sessions"

# 防止模型无限循环
MAX_REVIEW_ROUNDS = 8


# ============================================================
# 三席成员
# ============================================================

MEMBERS = {
    "GPT": {
        "label": "GPT",
        "cli": "Codex",
        "default_model": "GPT-5 / Codex",
        "default_thinking": "managed by Codex",
    },
    "Gemini": {
        "label": "Gemini",
        "cli": "agy",
        "default_model": "Gemini 3.7 Flash",
        "default_thinking": "managed by agy",
    },
    "GLM": {
        "label": "GLM",
        "cli": "glm",
        "default_model": "glm-5.3-flash",
        "default_thinking": "MAX",
    },
}


LETTER_TO_MEMBER = {
    "A": "GPT",
    "B": "Gemini",
    "C": "GLM",
}


# ============================================================
# UTF-8 子进程环境
# ============================================================


def build_utf8_environment():
    env = os.environ.copy()

    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    return env


UTF8_ENV = build_utf8_environment()


# ============================================================
# 通用命令执行
# ============================================================


def run_command(command):
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=False,
            timeout=COMMAND_TIMEOUT,
            shell=False,
            env=UTF8_ENV,
        )

        stdout = result.stdout.decode(
            "utf-8",
            errors="replace",
        ).strip()

        stderr = result.stderr.decode(
            "utf-8",
            errors="replace",
        ).strip()

        if result.returncode != 0:
            return {
                "success": False,
                "stdout": stdout,
                "stderr": stderr,
                "output": stderr or stdout or "未知错误",
            }

        return {
            "success": True,
            "stdout": stdout,
            "stderr": stderr,
            "output": stdout or stderr or "",
        }

    except FileNotFoundError:
        return {
            "success": False,
            "stdout": "",
            "stderr": "",
            "output": f"找不到命令：{command[0]}",
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "stdout": "",
            "stderr": "",
            "output": f"调用超时（{COMMAND_TIMEOUT} 秒）。",
        }

    except Exception as e:
        return {
            "success": False,
            "stdout": "",
            "stderr": "",
            "output": f"调用异常：{e}",
        }


# ============================================================
# GPT / Codex
# ============================================================


def call_gpt(prompt):
    result = run_command(
        [
            "codex",
            "exec",
            "--skip-git-repo-check",
            prompt,
        ]
    )

    if not result["success"]:
        return result

    result["content"] = clean_gpt_output(result["output"])

    result["model"] = MEMBERS["GPT"]["default_model"]
    result["thinking"] = MEMBERS["GPT"]["default_thinking"]

    return result


def clean_gpt_output(output):
    lines = output.splitlines()

    answer_start = None
    answer_end = len(lines)

    for i, line in enumerate(lines):
        if line.strip().lower() == "codex":
            answer_start = i + 1
            break

    if answer_start is None:
        return output.strip()

    for i in range(answer_start, len(lines)):
        if lines[i].strip().lower().startswith("tokens used"):
            answer_end = i
            break

    cleaned = "\n".join(lines[answer_start:answer_end]).strip()

    return cleaned or output.strip()


# ============================================================
# Gemini / agy
# ============================================================


def call_gemini(prompt):
    result = run_command(
        [
            "agy",
            "-p",
            prompt,
        ]
    )

    if not result["success"]:
        return result

    result["content"] = result["output"]
    result["model"] = MEMBERS["Gemini"]["default_model"]
    result["thinking"] = MEMBERS["Gemini"]["default_thinking"]

    return result


# ============================================================
# GLM
# ============================================================


def call_glm(prompt):
    if not GLM_SCRIPT.exists():
        return {
            "success": False,
            "stdout": "",
            "stderr": "",
            "output": f"找不到 GLM CLI：{GLM_SCRIPT}",
        }

    result = run_command(
        [
            sys.executable,
            str(GLM_SCRIPT),
            "-json",
            prompt,
        ]
    )

    if not result["success"]:
        return result

    raw = result["output"]

    try:
        data = json.loads(raw)

    except json.JSONDecodeError:
        return {
            "success": False,
            "stdout": raw,
            "stderr": result["stderr"],
            "output": "GLM 返回的不是合法 JSON。",
        }

    actual_model = data.get("model")

    if not actual_model:
        actual_model = MEMBERS["GLM"]["default_model"]

    content = ""

    try:
        message = data["choices"][0]["message"]
        content = message.get("content", "")

    except (KeyError, IndexError, TypeError):
        pass

    return {
        "success": True,
        "stdout": raw,
        "stderr": result["stderr"],
        "output": content,
        "content": content,
        "model": actual_model,
        "thinking": MEMBERS["GLM"]["default_thinking"],
    }


# ============================================================
# MemberReport
# ============================================================


def build_member_report(member, result):
    config = MEMBERS[member]

    if result.get("success"):
        return {
            "member": member,
            "cli": config["cli"],
            "model": result.get(
                "model",
                config["default_model"],
            ),
            "thinking": result.get(
                "thinking",
                config["default_thinking"],
            ),
            "success": True,
            "content": result.get(
                "content",
                result.get("output", ""),
            ),
        }

    return {
        "member": member,
        "cli": config["cli"],
        "model": result.get(
            "model",
            config["default_model"],
        ),
        "thinking": result.get(
            "thinking",
            config["default_thinking"],
        ),
        "success": False,
        "content": "",
        "error": result.get(
            "output",
            "未知错误",
        ),
    }


# ============================================================
# 显示报告
# ============================================================


def print_member_report(
    report,
    letter=None,
    title=None,
):
    print()
    print("=" * 70)

    if title:
        print(title)

    elif letter:
        print(f"【{letter}】{report['member']}")

    else:
        print(report["member"])

    print("-" * 70)

    if not report["success"]:
        print("[ERROR]")
        print(report["error"])
        return

    print(f"Model     : {report['model']}")

    print(f"CLI       : {report['cli']}")

    print(f"Thinking  : {report['thinking']}")

    print()
    print(report["content"])


# ============================================================
# 评审状态提取
# ============================================================


def extract_review_status(text):
    if not text:
        return "NEED_REVISION"

    normalized = text.upper()

    patterns = [
        r"【最终评审状态】\s*(PASS|NEED_REVISION|NEED_USER_DECISION)",
        r"最终评审状态\s*[:：]\s*(PASS|NEED_REVISION|NEED_USER_DECISION)",
        r"FINAL\s+REVIEW\s+STATUS\s*[:：]?\s*(PASS|NEED_REVISION|NEED_USER_DECISION)",
        r"【REVIEW\s+STATUS】\s*(PASS|NEED_REVISION|NEED_USER_DECISION)",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            normalized,
            flags=re.IGNORECASE,
        )

        if match:
            return match.group(1).upper()

    if "NEED_USER_DECISION" in normalized:
        return "NEED_USER_DECISION"

    if "NEED_REVISION" in normalized:
        return "NEED_REVISION"

    if re.search(
        r"\bPASS\b",
        normalized,
    ):
        return "PASS"

    return "NEED_REVISION"


# ============================================================
# 获取当前方案
# ============================================================


def get_current_proposal(session):
    current = session.get("current_proposal")

    if current:
        return {
            "version": current.get(
                "version",
                1,
            ),
            "author": current.get(
                "author",
                session["selected"]["author"],
            ),
            "content": current.get(
                "content",
                "",
            ),
        }

    author = session["selected"]["author"]

    return {
        "version": 1,
        "author": author,
        "content": session["reports"][author]["content"],
    }


# ============================================================
# Reviewer 1 Prompt
# ============================================================


def build_reviewer1_prompt(
    question,
    author,
    version,
    proposal,
):
    return f"""
你是 AI Council 的 Reviewer 1。

当前问题：
{question}

方案作者：
{author}

当前版本：
V{version}

==================================================
当前方案
==================================================
{proposal}
==================================================

请独立、严格审查当前版本。

你的任务不是重新提出完整的新方案，而是判断当前方案是否存在会影响成立的问题。

重点检查：

1. 核心逻辑
2. 事实与推理
3. 重要遗漏
4. 关键假设
5. 内部矛盾
6. 可执行性
7. 风险
8. 歧义
9. 是否真正回答原始问题

请区分：

【必须修改】
影响正确性、完整性、可执行性或核心目标。

【建议修改】
可以提高质量，但不影响方案成立。

【没有问题】
当前已经足够可靠。

不要参考其他 Reviewer 的意见。
不要故意制造问题。
不要把个人偏好当成错误。
只评价当前 V{version}。

严格输出：

【Reviewer 1 初步结论】
PASS / NEED_REVISION / NEED_USER_DECISION

如果选择 NEED_USER_DECISION，
必须说明为什么 AI 无法自行解决。
"""


# ============================================================
# Reviewer 2 Prompt
# ============================================================


def build_reviewer2_prompt(
    question,
    author,
    version,
    proposal,
):
    return f"""
你是 AI Council 的 Reviewer 2。

当前问题：
{question}

方案作者：
{author}

当前版本：
V{version}

==================================================
当前方案
==================================================
{proposal}
==================================================

请独立、严格审查当前版本。

你的任务不是重新提出完整的新方案，而是判断当前方案是否存在会影响成立的问题。

重点检查：

1. 核心逻辑
2. 事实与推理
3. 重要遗漏
4. 关键假设
5. 内部矛盾
6. 可执行性
7. 风险
8. 歧义
9. 是否真正回答原始问题

请区分：

【必须修改】
影响正确性、完整性、可执行性或核心目标。

【建议修改】
可以提高质量，但不影响方案成立。

【没有问题】
当前已经足够可靠。

不要参考其他 Reviewer 的意见。
不要故意制造问题。
不要把个人偏好当成错误。
只评价当前 V{version}。

严格输出：

【Reviewer 2 初步结论】
PASS / NEED_REVISION / NEED_USER_DECISION

如果选择 NEED_USER_DECISION，
必须说明为什么 AI 无法自行解决。
"""


# ============================================================
# Reviewer 讨论：Reviewer 1 回应 Reviewer 2
# ============================================================


def build_reviewer1_debate_prompt(
    question,
    author,
    version,
    proposal,
    reviewer1,
    reviewer2,
):
    return f"""
你是 AI Council 的 Reviewer 1。

这是一次正式的双 Reviewer 评审讨论。

当前问题：
{question}

方案作者：
{author}

当前版本：
V{version}

==================================================
当前方案
==================================================
{proposal}
==================================================

你的初步审查：
==================================================
{reviewer1}
==================================================

Reviewer 2 的独立审查：
==================================================
{reviewer2}
==================================================

现在请回应 Reviewer 2，进行真正的同行评审讨论。

你必须：

1. 指出 Reviewer 2 哪些观点成立，并说明理由。
2. 指出 Reviewer 2 哪些观点不成立，并说明理由。
3. 回应 Reviewer 2 对方案提出的关键问题。
4. 检查自己初步审查中是否存在遗漏或误判。
5. 明确哪些问题属于【必须修改】。
6. 明确哪些问题只是【建议修改】。
7. 列出双方仍然存在的实质性分歧。

注意：
- 不要为了达成共识而强行让步。
- 不要因为 Reviewer 2 提出了问题，就默认问题成立。
- 不要把个人偏好当成硬性要求。
- 如果发现自己判断错误，要明确修正。
- 不要重新设计整个方案。

严格输出：

【Reviewer 1 对 Reviewer 2 的回应】
...

【Reviewer 1 修正后的必须修改】
...

【Reviewer 1 修正后的建议修改】
...

【Reviewer 1 仍坚持的分歧】
...
"""


# ============================================================
# Reviewer 讨论：Reviewer 2 最终回应 Reviewer 1
# ============================================================


def build_reviewer2_debate_prompt(
    question,
    author,
    version,
    proposal,
    reviewer2,
    reviewer1,
    reviewer1_response,
):
    return f"""
你是 AI Council 的 Reviewer 2。

这是一次正式的双 Reviewer 评审讨论。

当前问题：
{question}

方案作者：
{author}

当前版本：
V{version}

==================================================
当前方案
==================================================
{proposal}
==================================================

你的初步审查：
==================================================
{reviewer2}
==================================================

Reviewer 1 的初步审查：
==================================================
{reviewer1}
==================================================

Reviewer 1 对你的回应：
==================================================
{reviewer1_response}
==================================================

现在进行第二轮、也是本轮最后一次同行评审回应。

你必须：

1. 回应 Reviewer 1 的核心论点。
2. 明确接受、拒绝或修正双方争议点。
3. 检查自己是否存在误判。
4. 明确哪些问题经过讨论后确实必须修改。
5. 明确哪些问题只是建议修改。
6. 明确列出仍然无法消除的实质性分歧。
7. 判断这些分歧是否需要用户决定。

注意：
- 不要机械坚持原观点。
- 不要为了继续循环而制造新问题。
- 不要把个人偏好当成硬性要求。
- 不要重新设计整个方案。
- 如果方案已经可靠，可以明确说明。

严格输出：

【Reviewer 2 最终回应】
...

【双方达成的共识】
...

【必须修改】
...

【建议修改】
...

【仍存在的实质性分歧】
...
"""


# ============================================================
# Reviewer 最终共识
# ============================================================


def build_reviewer_consensus_prompt(
    question,
    author,
    version,
    proposal,
    reviewer1,
    reviewer2,
    reviewer1_response,
    reviewer2_response,
):
    return f"""
你是 AI Council 的评审协调员。

注意：你不是新的方案提出者。
你的唯一任务是根据两名 Reviewer 的完整评审记录，形成一份严格的最终评审共识。

当前问题：
{question}

方案作者：
{author}

当前版本：
V{version}

==================================================
当前方案
==================================================
{proposal}
==================================================

Reviewer 1 初步审查：
==================================================
{reviewer1}
==================================================

Reviewer 2 初步审查：
==================================================
{reviewer2}
==================================================

Reviewer 1 对 Reviewer 2 的回应：
==================================================
{reviewer1_response}
==================================================

Reviewer 2 的最终回应：
==================================================
{reviewer2_response}
==================================================

请根据完整讨论记录形成最终评审结论。

判断原则：

PASS：
不存在会影响方案成立、正确性、完整性、可执行性的关键问题。
仍然可以优化，不等于必须继续修改。

NEED_REVISION：
存在 Author 可以通过修改解决的实质性问题。

NEED_USER_DECISION：
存在必须由用户进行价值取舍、授权、目标选择或其他 AI 无法自行决定的问题。

特别注意：
- 不要追求绝对完美。
- 不要为了继续循环而制造小问题。
- 不要把个人偏好当硬性要求。
- 不要改变原始问题。
- 不要重新设计整个方案。
- 如果 Reviewer 之间只是措辞、风格或可选优化上的分歧，应视为 PASS。
- 如果双方存在分歧，必须判断该分歧是否真的影响方案成立。
- 只有确实无法由 AI 自行消除的价值判断，才升级给用户。

严格输出：

【共识】
...

【分歧】
...

【必须修改】
...

【建议修改】
...

【未解决的关键问题】
...

【最终评审状态】
PASS / NEED_REVISION / NEED_USER_DECISION
"""


# ============================================================

# Author 修改
# ============================================================


def build_author_revision_prompt(
    question,
    author,
    version,
    proposal,
    reviewer1,
    reviewer2,
    consensus,
):
    next_version = version + 1

    return f"""
你是 AI Council 的方案提出者（Author）。

当前问题：

{question}

你的身份：
{author}

当前方案：
V{version}

==================================================
V{version}
==================================================
{proposal}
==================================================

Reviewer 1：
==================================================
{reviewer1}
==================================================

Reviewer 2：
==================================================
{reviewer2}
==================================================

统一评审意见：
==================================================
{consensus}
==================================================

请将方案修改为 V{next_version}。

要求：

1. 保留 V{version} 中仍然成立的内容。
2. 必须解决所有真正的“必须修改”。
3. 合理吸收“建议修改”。
4. 如果 Reviewer 的判断错误，可以拒绝，但必须说明理由。
5. 不要无意义扩大原任务。
6. 不要改变原始问题目标。
7. 不要虚构已经发生的现实验证。
8. V{next_version} 必须完整、独立可读。
9. 不要只输出修改日志，必须输出完整的新方案。

严格输出：

【方案正文】
完整的 V{next_version}。

【本轮修改摘要】
V{version} → V{next_version} 的关键变化。

【仍存在的不确定性】
没有则写“无”。
"""


# ============================================================
# 用户选择主案
# ============================================================


def ask_for_selection():
    print()
    print("=" * 70)
    print("                     请选择主案")
    print("=" * 70)

    print()
    print("A → GPT")
    print("B → Gemini")
    print("C → GLM")
    print()

    while True:
        choice = input("> ").strip().upper()

        if choice in {
            "A",
            "B",
            "C",
        }:
            return choice

        print("无效选择，请输入 A、B 或 C。")


# ============================================================
# 用户裁决
# ============================================================


def ask_user_decision(
    issue_text,
):
    print()
    print("=" * 70)
    print("                 需要你的最终裁决")
    print("=" * 70)

    print()
    print(issue_text)

    print()
    print("请输入你的决定。")

    return input("> ").strip()


# ============================================================
# 保存 Session
# ============================================================


def save_session(
    session,
    session_file=None,
):
    SESSION_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    if session_file is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        session_file = SESSION_DIR / f"session_{timestamp}.json"

    with session_file.open(
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            session,
            f,
            ensure_ascii=False,
            indent=2,
        )

    return session_file


# ============================================================
# 三方独立提案
# ============================================================


def gather_proposals(prompt):

    print()
    print("=" * 70)
    print("                     独立提案")
    print("=" * 70)

    tasks = {
        "GPT": call_gpt,
        "Gemini": call_gemini,
        "GLM": call_glm,
    }

    raw_results = {}

    start_time = datetime.now()

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        future_map = {
            executor.submit(
                func,
                prompt,
            ): member
            for member, func in tasks.items()
        }

        for future in concurrent.futures.as_completed(future_map):
            member = future_map[future]

            try:
                raw_results[member] = future.result()

            except Exception as e:
                raw_results[member] = {
                    "success": False,
                    "output": str(e),
                }

    reports = {
        member: build_member_report(
            member,
            raw_results.get(
                member,
                {
                    "success": False,
                    "output": "没有收到结果。",
                },
            ),
        )
        for member in tasks
    }

    for member, letter in [
        ("GPT", "A"),
        ("Gemini", "B"),
        ("GLM", "C"),
    ]:
        print_member_report(
            reports[member],
            letter,
        )

    elapsed = (datetime.now() - start_time).total_seconds()

    return reports, elapsed


# ============================================================
# 执行完整 Council
# ============================================================


def run_council(prompt):

    # --------------------------------------------------------
    # 第一阶段：独立提案
    # --------------------------------------------------------

    reports, elapsed = gather_proposals(prompt)

    # --------------------------------------------------------
    # 用户选择
    # --------------------------------------------------------

    selected_letter = ask_for_selection()

    author = LETTER_TO_MEMBER[selected_letter]

    reviewers = [member for member in MEMBERS if member != author]

    print()
    print("=" * 70)
    print("                     角色分配")
    print("=" * 70)

    print()
    print(f"AUTHOR      ：{author}")
    print(f"REVIEWER 1  ：{reviewers[0]}")
    print(f"REVIEWER 2  ：{reviewers[1]}")

    # --------------------------------------------------------
    # 创建 Session
    # --------------------------------------------------------

    session_file = save_session(
        {
            "timestamp": datetime.now().isoformat(),
            "question": prompt,
            "phase": "selection_complete",
            "status": "running",
            "selected": {
                "letter": selected_letter,
                "author": author,
                "reviewers": reviewers,
            },
            "reports": reports,
            "current_proposal": {
                "version": 1,
                "author": author,
                "content": reports[author]["content"],
            },
            "review_rounds": [],
            "revisions": [],
            "user_decisions": [],
            "history": [
                {
                    "phase": "proposal",
                    "version": 1,
                    "timestamp": datetime.now().isoformat(),
                }
            ],
        }
    )

    # ========================================================
    # 自动循环
    # ========================================================

    for round_number in range(
        1,
        MAX_REVIEW_ROUNDS + 1,
    ):
        current = get_current_proposal(
            json.load(
                session_file.open(
                    "r",
                    encoding="utf-8",
                )
            )
        )

        # --------------------------------------------
        # 每一轮都重新加载最新 Session
        # --------------------------------------------

        with session_file.open(
            "r",
            encoding="utf-8",
        ) as f:
            session = json.load(f)

        current = get_current_proposal(session)

        version = current["version"]
        proposal = current["content"]

        author = session["selected"]["author"]

        reviewers = session["selected"]["reviewers"]

        reviewer1 = reviewers[0]
        reviewer2 = reviewers[1]

        print()
        print("=" * 70)
        print(f"                  REVIEW ROUND {round_number}")
        print(f"                    CURRENT V{version}")
        print("=" * 70)

        # ================================================
        # Reviewer 1：独立审查
        # ================================================

        print()
        print(f"{reviewer1} 正在独立审查 V{version} ...")

        reviewer1_result = {
            "GPT": call_gpt,
            "Gemini": call_gemini,
            "GLM": call_glm,
        }[reviewer1](
            build_reviewer1_prompt(
                prompt,
                author,
                version,
                proposal,
            )
        )

        reviewer1_report = build_member_report(
            reviewer1,
            reviewer1_result,
        )

        print_member_report(
            reviewer1_report,
            title=(f"【Reviewer 1 独立审查】{reviewer1}"),
        )

        # ================================================
        # Reviewer 2：独立审查
        # ================================================

        print()
        print(f"{reviewer2} 正在独立审查 V{version} ...")

        reviewer2_result = {
            "GPT": call_gpt,
            "Gemini": call_gemini,
            "GLM": call_glm,
        }[reviewer2](
            build_reviewer2_prompt(
                prompt,
                author,
                version,
                proposal,
            )
        )

        reviewer2_report = build_member_report(
            reviewer2,
            reviewer2_result,
        )

        print_member_report(
            reviewer2_report,
            title=(f"【Reviewer 2 独立审查】{reviewer2}"),
        )

        # ================================================
        # Reviewer 1 回应 Reviewer 2
        # ================================================

        print()
        print(f"{reviewer1} 正在回应 {reviewer2} 的审查 ...")

        reviewer1_debate_result = {
            "GPT": call_gpt,
            "Gemini": call_gemini,
            "GLM": call_glm,
        }[reviewer1](
            build_reviewer1_debate_prompt(
                prompt,
                author,
                version,
                proposal,
                reviewer1_report.get("content", ""),
                reviewer2_report.get("content", ""),
            )
        )

        reviewer1_debate_report = build_member_report(
            reviewer1,
            reviewer1_debate_result,
        )

        print_member_report(
            reviewer1_debate_report,
            title=(f"【Reviewer 1 回应 Reviewer 2】{reviewer1}"),
        )

        # ================================================
        # Reviewer 2 最终回应
        # ================================================

        print()
        print(f"{reviewer2} 正在回应 {reviewer1} ...")

        reviewer2_debate_result = {
            "GPT": call_gpt,
            "Gemini": call_gemini,
            "GLM": call_glm,
        }[reviewer2](
            build_reviewer2_debate_prompt(
                prompt,
                author,
                version,
                proposal,
                reviewer2_report.get("content", ""),
                reviewer1_report.get("content", ""),
                reviewer1_debate_report.get("content", ""),
            )
        )

        reviewer2_debate_report = build_member_report(
            reviewer2,
            reviewer2_debate_result,
        )

        print_member_report(
            reviewer2_debate_report,
            title=(f"【Reviewer 2 最终回应】{reviewer2}"),
        )

        # ================================================
        # 最终评审共识
        # ================================================

        print()
        print("评审协调员正在根据完整讨论记录形成最终共识 ...")

        # 为避免新增第四个模型，协调器仍使用 Reviewer 1。
        # 关键区别：协调器此时看到的是完整双向讨论，而不是仅仅两份初步意见。
        consensus_result = {
            "GPT": call_gpt,
            "Gemini": call_gemini,
            "GLM": call_glm,
        }[reviewer1](
            build_reviewer_consensus_prompt(
                prompt,
                author,
                version,
                proposal,
                reviewer1_report.get("content", ""),
                reviewer2_report.get("content", ""),
                reviewer1_debate_report.get("content", ""),
                reviewer2_debate_report.get("content", ""),
            )
        )

        consensus_report = build_member_report(
            reviewer1,
            consensus_result,
        )

        print_member_report(
            consensus_report,
            title="【最终评审共识】",
        )

        consensus_text = consensus_report.get(
            "content",
            "",
        )

        status = extract_review_status(consensus_text)

        # ================================================
        # 保存本轮评审
        # ================================================

        review_record = {
            "round": round_number,
            "version": version,
            "author": author,
            "reviewers": [
                reviewer1,
                reviewer2,
            ],
            "reviewer_reports": {
                reviewer1: reviewer1_report,
                reviewer2: reviewer2_report,
            },
            "reviewer_debate": {
                "reviewer1_response": reviewer1_debate_report,
                "reviewer2_response": reviewer2_debate_report,
            },
            "consensus": {
                "member": reviewer1,
                "content": consensus_text,
                "success": consensus_report.get(
                    "success",
                    False,
                ),
            },
            "status": status,
            "timestamp": datetime.now().isoformat(),
        }

        session.setdefault(
            "review_rounds",
            [],
        )

        session["review_rounds"].append(review_record)

        session.setdefault(
            "history",
            [],
        )

        session["history"].append(
            {
                "phase": "review",
                "round": round_number,
                "version": version,
                "status": status,
                "timestamp": datetime.now().isoformat(),
            }
        )

        # ================================================
        # 需要用户裁决
        # ================================================

        if status == "NEED_USER_DECISION":
            session["phase"] = "awaiting_user_decision"

            session["status"] = "awaiting_user_decision"

            save_session(
                session,
                session_file,
            )

            print()
            print("=" * 70)
            print("          AI 无法自行解决当前分歧")
            print("=" * 70)

            decision = ask_user_decision(consensus_text)

            session.setdefault(
                "user_decisions",
                [],
            )

            session["user_decisions"].append(
                {
                    "round": round_number,
                    "version": version,
                    "decision": decision,
                    "timestamp": datetime.now().isoformat(),
                }
            )

            # --------------------------------------------
            # 将用户决定交给 Author
            # --------------------------------------------

            revision_prompt = f"""
你是 AI Council 的方案提出者（Author）。

原始问题：
{prompt}

当前方案 V{version}：

==================================================
{proposal}
==================================================

Reviewer 的最终意见：

==================================================
{consensus_text}
==================================================

用户已经做出最终裁决：

==================================================
{decision}
==================================================

请根据用户决定修改方案。

生成 V{version + 1}。

必须：

1. 遵守用户刚刚做出的决定。
2. 保留原方案仍然成立的部分。
3. 解决评审中真正存在的问题。
4. 不得重新改变用户已经确认的目标。
5. 输出完整的新方案。

严格输出：

【方案正文】
...

【本轮修改摘要】
...

【仍存在的不确定性】
...
"""

            author_result = {
                "GPT": call_gpt,
                "Gemini": call_gemini,
                "GLM": call_glm,
            }[author](revision_prompt)

            author_report = build_member_report(
                author,
                author_result,
            )

            if not author_report["success"]:
                print()
                print("Author 修改失败，会议暂停。")

                session["status"] = "error"

                save_session(
                    session,
                    session_file,
                )

                return

            new_content = author_report["content"]

            next_version = version + 1

            session.setdefault(
                "revisions",
                [],
            )

            session["revisions"].append(
                {
                    "version": next_version,
                    "author": author,
                    "content": new_content,
                    "model": author_report["model"],
                    "thinking": author_report["thinking"],
                    "timestamp": datetime.now().isoformat(),
                }
            )

            session["current_proposal"] = {
                "version": next_version,
                "author": author,
                "content": new_content,
            }

            session["phase"] = "revision_complete"

            session["status"] = "running"

            session["history"].append(
                {
                    "phase": "revision",
                    "version": next_version,
                    "author": author,
                    "reason": "user_decision",
                    "timestamp": datetime.now().isoformat(),
                }
            )

            save_session(
                session,
                session_file,
            )

            print()
            print("=" * 70)
            print(f"用户裁决已吸收，{author} 已生成 V{next_version}。")
            print("=" * 70)

            continue

        # ================================================
        # PASS
        # ================================================

        if status == "PASS":
            session["phase"] = "final_approved"

            session["status"] = "final_approved"

            session["final_proposal"] = {
                "version": version,
                "author": author,
                "content": proposal,
                "approved_at": datetime.now().isoformat(),
                "review_round": round_number,
            }

            session["history"].append(
                {
                    "phase": "final_approved",
                    "version": version,
                    "author": author,
                    "timestamp": datetime.now().isoformat(),
                }
            )

            save_session(
                session,
                session_file,
            )

            # --------------------------------------------
            # 最终方案直接交给用户
            # --------------------------------------------

            print()
            print("=" * 70)
            print("                    APPROVED")
            print("=" * 70)

            print()
            print(f"最终方案作者：{author}")

            print(f"最终版本：V{version}")

            print()

            print("=" * 70)
            print("                 最终方案")
            print("=" * 70)

            print()
            print(proposal)

            print()
            print("=" * 70)

            print(f"会议完成，共经历 {round_number} 轮评审。")

            print(f"会议记录：{session_file}")

            print("=" * 70)

            return

        # ================================================
        # NEED_REVISION
        # ================================================

        print()
        print("=" * 70)

        print(f"当前 V{version} 需要修改。")

        print(f"{author} 正在生成 V{version + 1} ...")

        print("=" * 70)

        revision_prompt = build_author_revision_prompt(
            prompt,
            author,
            version,
            proposal,
            reviewer1_report.get(
                "content",
                "",
            ),
            reviewer2_report.get(
                "content",
                "",
            ),
            consensus_text,
        )

        author_result = {
            "GPT": call_gpt,
            "Gemini": call_gemini,
            "GLM": call_glm,
        }[author](revision_prompt)

        author_report = build_member_report(
            author,
            author_result,
        )

        print_member_report(
            author_report,
            title=(f"【AUTHOR V{version + 1}】{author}"),
        )

        if not author_report["success"]:
            session["phase"] = "error"

            session["status"] = "error"

            save_session(
                session,
                session_file,
            )

            print()
            print("Author 修改失败，会议暂停。")

            return

        next_version = version + 1

        new_content = author_report["content"]

        session.setdefault(
            "revisions",
            [],
        )

        session["revisions"].append(
            {
                "version": next_version,
                "author": author,
                "content": new_content,
                "model": author_report["model"],
                "thinking": author_report["thinking"],
                "timestamp": datetime.now().isoformat(),
            }
        )

        session["current_proposal"] = {
            "version": next_version,
            "author": author,
            "content": new_content,
        }

        session["phase"] = "revision_complete"

        session["status"] = "running"

        session["history"].append(
            {
                "phase": "revision",
                "version": next_version,
                "author": author,
                "timestamp": datetime.now().isoformat(),
            }
        )

        save_session(
            session,
            session_file,
        )

        print()
        print("=" * 70)
        print(f"V{next_version} 已完成。")
        print("自动进入下一轮评审。")
        print("=" * 70)

    # ========================================================
    # 达到最大轮数
    # ========================================================

    with session_file.open(
        "r",
        encoding="utf-8",
    ) as f:
        session = json.load(f)

    session["phase"] = "max_rounds_reached"

    session["status"] = "max_rounds_reached"

    session["history"].append(
        {
            "phase": "max_rounds_reached",
            "max_rounds": MAX_REVIEW_ROUNDS,
            "timestamp": datetime.now().isoformat(),
        }
    )

    save_session(
        session,
        session_file,
    )

    print()
    print("=" * 70)
    print(f"已达到最大评审轮数：{MAX_REVIEW_ROUNDS}")
    print()
    print("为了防止无限循环，会议暂停。")
    print(f"会议记录：{session_file}")
    print("=" * 70)


# ============================================================
# CLI
# ============================================================


def main():

    if len(sys.argv) > 1:
        if sys.argv[1].lower() in {
            "--help",
            "-h",
        }:
            print()
            print("AI Council")
            print()
            print('用法：python council.py "你的问题"')
            print()
            print("流程：")
            print("1. GPT / Gemini / GLM 独立提出方案")
            print("2. 用户选择主案")
            print("3. 双 Reviewer 独立审查并相互讨论")
            print("4. 形成评审共识后 Author 修改")
            print("5. 持续评审循环直到 PASS")
            print("6. 需要用户裁决时暂停")
            print()
            return

    if len(sys.argv) > 1:
        prompt = " ".join(sys.argv[1:])
    else:
        prompt = input("请输入问题：\n> ").strip()

    if not prompt:
        print("错误：问题不能为空。")
        sys.exit(1)

    if not GLM_SCRIPT.exists():
        print()
        print("错误：找不到 GLM CLI：")
        print(GLM_SCRIPT)
        sys.exit(1)

    run_council(prompt)


# ============================================================
# Entry
# ============================================================

if __name__ == "__main__":
    main()
