"""从 CHANGELOG.md 提取某个版本的发布说明，供 GitHub Release 使用。

用法:
    python tools/release_notes.py v1.3.0            # 写到 stdout
    python tools/release_notes.py v1.3.0 --out file # 写到文件

设计要点：
  - 只认 `## [x.y.z] - date` 形式的版本标题（Keep a Changelog 约定）
  - 截到下一个同级版本标题为止，并剥掉文件末尾的链接定义区
  - 末尾附上「安装」与「完整变更对比」两段固定信息，让 Release 页面自解释
  - 找不到版本时以非零码退出，避免 CI 静默发出空说明
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"
REPO = "https://github.com/xmwy0712/ai-council"

HEADING = re.compile(r"^## \[(?P<ver>[^\]]+)\](?:\s*-\s*(?P<date>.+))?$", re.MULTILINE)

INSTALL_BLOCK = """---

### 安装 / Install

| 产物 | 说明 |
|---|---|
| `ai-council.exe` | Windows 单文件，下载即用（无需 Python） |
| `ai-council-win64.zip` | Windows 解压即用，启动更快 |
| `AI-Council-Setup-<ver>.exe` | Windows 用户级安装器，带开始菜单图标 |
| `ai_council-<ver>-py3-none-any.whl` | `uv pip install <file>` 或 `pip install <file>` |
| `ai_council-<ver>.tar.gz` | 源码发行包 |

```bash
uv pip install ai-council        # 从 PyPI 安装
council run "你的问题"            # 首场无需密钥，内置 fake 适配器
council serve                    # 浏览器界面 http://127.0.0.1:8765
```
"""

# 面向普通用户的直白摘要（每个版本一段，写给人看而不是给机器看）。
# 没登记的版本退化为只有技术正文，不影响发布。
PLAIN_SUMMARY: dict[str, str] = {
    "1.3.0": """## 这次更新了什么

**一句话：模型列表能自动保鲜了，界面也顺手了不少。**

### 🆕 新功能

- **模型清单自动更新（默认关闭）**：以前厂商发了新模型，你得等仓库更新或自己改配置。
  现在到「设置 → 模型清单更新」打开开关，AI Council 会问各家自己的接口现在有哪些模型，
  把缺的自动补上。新模型会直接出现在可选列表里。
- **自动补进来的模型单独成组**：不会和已经人工核实过的条目混在一起，一眼分得清。
- **新模型也会带出思考档位**：能拿到厂商的可靠信号就声明档位；拿不到就不声明——
  宁可没有这个旋钮，也不瞎猜（猜错会直接报错）。

### 🔧 改进

- **修好了一个模型可能“消失”的毛病**：以前两份配置文件写了同一家厂商，后者会整个盖掉前者，
  内置模型全没了。现在改为按厂商合并。
- **修好了每次打开页面多请一次接口**：白跑一趟，现在只请一次。
- **补上了标签页图标**：以前浏览器每次加载都在猜，猜不到就报 404；现在有图标了。
- **点「立即检查」不再报错**：如果在后台已经在检查，过去会给你一个 409 错误；
  现在会等那一轮跑完并把结果复用给你。

### 🎨 界面

- **失败策略和导出格式搬进了「设置」**：不再挤在会议页顶部。
- **模型下拉多了「不选择」**：选过模型后还能退回默认，不会卡住。
- **历史对话可按主题和日期搜索**：会话多了也找得到。
- **入口三张卡片的图案重画了**：圆桌会议、文档、双齿轮，线条更干净。
""",
    "1.2.2": """## 这次更新了什么

**一句话：修好了鼠标移到页面右边会出现彩色碎块的问题。**

### 修复

- **鼠标移到页面右侧不再出现色块错乱**：以前鼠标靠近右边缘时，绕它转的彩色光点会被页面边界
  切掉，露出一条彩色的残片。现在改成光点轨道靠近边缘时自动缩一缩，鼠标可以自由移动，
  光点再也不会被硬切；靠近卡片时也会平滑淡出，不留下半圈硬痕。
- **历史对话里的超长问题不再挤破排版**：问题太长时截到 32 个字加省略号，鼠标悬停看全文。
""",
    "1.2.1": """## 这次更新了什么

**一句话：光效不再留痕，而且只选一个模型也能跑完。**

### 修复

- **鼠标移到页面最右边不再留下彩色痕迹**：原因和上面那版类似，这次先把行星轨道收在了
  安全范围内。（这个方案后来又在 1.2.2 里重做过——不再限制鼠标活动范围。）
- **挑选了模型的节点不再混入讨论**：只给部分成员指定了真实模型时，剩下还在用演示档的
  节点会自动退出，它们的模板回复不再混进提案里。
- **只选一个模型也能跑完一场会议**：以前只有一个参与者时，评审阶段会因为「没有第二个
  评审人」而卡住；现在直接跳过评审进入裁定，不再把会话冻住。
""",
    "1.2.0": """## 这次更新了什么

**一句话：换了方正字体排版，并新增本地 CLI 环境检查。**

### 🆕 新功能

- **字体排版升级**：全站改用方正仿宋（正文）+ 方正大标宋（标题），字体文件自带，不依赖
  你电脑上装没装。页面主区域也加宽了，读起来更舒服。
- **本地 CLI 环境检查**：设置里可以一键检查 codex / claude / antigravity 三个本地命令行
  工具装没装、什么版本、登录了没。
- **运行参数可调**：卡死判定超时与重试次数能在设置里改，每次开会自动应用。

### 修复

- **选真实模型后真的会用它了**：以前给演示节点贴上真实模型名，它还是会用内置模板
  “假装”回答（你的密钥和思考设置根本没生效）；现在选中真实模型会连带切换到底层适配器。
- **推理模型不再报 temperature 错误**：GPT-5.6/6 系与 Moonshot K 系不接受自定义
  temperature，现在会自动省略这个参数。
""",
    "1.1.1": """## 这次更新了什么

**一句话：思考档位全部改成由注册表驱动，并修正了几家厂商的档位。**

### 改进

- **思考档位由数据驱动**：每家厂商的档位数量和命名都不一样，现在全部从注册表读取，
  不再写死在代码里。
- **Claude 5 系支持自适应思考**（五档 effort）。
- **修正了 Kimi 与 Spark 的档位**：按官方文档核实后修正了之前写错的值。
""",
    "1.1.0": """## 这次更新了什么

**一句话：模型注册表大幅扩充，设置中心统一收口。**

### 🆕 新功能

- **模型库扩到 14 家厂商 / 126 个模型**（127 个条目）——每一个都对着官方文档核实过。
- **零代码接入新厂商**：只要声明 `adapter` 字段就能接入，不用写代码。
- **设置中心**：密钥、主题、语言、鼠标光效统一在一个地方管理。
- **会话级阵容调整**：可以只给这一场会议换模型，不影响全局配置。
- **模型目录接口** `GET /api/models`。
""",
    "1.0.0": """## 这次更新了什么

**第一个正式版。**

八阶段会谈引擎完整落地：独立提案 → 评审 → 辩论 → 裁定 → 修订，直到收敛出终稿。

### 包含什么

- **完整引擎**：八阶段状态机、事件溯源存储、断点续跑、人工干预协议。
- **Web 界面**（`council serve`）：实时事件流、暂停/恢复/终止、导出即下载。
- **命令行**：`run` / `resume` / `sessions` / `config-check` / `export`。
- **CI 与发布**：自动跑 lint、类型检查与全平台测试；打标签自动出安装包。
""",
}


def plain_summary(version: str) -> str:
    """取该版本的通俗摘要；未登记则返回空串。"""
    return PLAIN_SUMMARY.get(version.lstrip("v"), "")


def extract(version: str, text: str) -> str:
    """取出该版本的正文（不含标题行），到下一个版本标题为止。"""
    ver = version.lstrip("v")
    matches = list(HEADING.finditer(text))
    for i, m in enumerate(matches):
        if m.group("ver") != ver:
            continue
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end]
        # 剥掉文件末尾的链接定义区（形如 [1.3.0]: https://...）
        body = re.split(r"^\[[^\]]+\]:\s*https?://", body, flags=re.MULTILINE)[0]
        return body.strip()
    raise SystemExit(f"错误：CHANGELOG 里找不到版本 {version}")


def build(version: str, body: str) -> str:
    ver = version.lstrip("v")
    parts: list[str] = []
    # 先放通俗摘要（给普通用户看），再放技术正文（给想看细节的人）
    summary = plain_summary(ver)
    if summary:
        parts.append(summary.rstrip())
        parts.append("<details>\n<summary>技术细节（完整变更记录）</summary>\n")
        parts.append(f"## v{ver}\n\n{body}")
        parts.append("</details>")
    else:
        parts.append(f"## v{ver}\n\n{body}")

    parts.append(INSTALL_BLOCK.rstrip())
    # 前一版优先查真实 tag；仓库无 tag（如 CI 检出）时回退到 CHANGELOG 的版本列表
    prev = _prev_tag(ver) or _prev_from_changelog(ver)
    if prev:
        parts.append(
            f"\n**完整变更对比 / Full Changelog**: "
            f"[`{prev}...v{ver}`]({REPO}/compare/{prev}...v{ver})"
        )
    else:
        # 首个版本没有可对比的前一版，列全部提交
        parts.append(f"\n**全部提交 / All commits**: [{REPO}/commits]({REPO}/commits)")
    return "\n\n".join(parts) + "\n"


def _prev_tag(ver: str) -> str:
    """取紧邻的前一个版本 tag，从**真实的 tag 列表**而非版本号递减推算。

    递减推算会错：1.3.0 的前一版是 1.2.2（不是 1.2.0），因为 1.2.x 有补丁版。
    优先用 git tag，失败时回退到 GitHub Releases，最后才退化到推算。
    """
    import subprocess

    def parse(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in re.findall(r"\d+", v.lstrip("v"))[:3])

    tags: list[str] = []
    try:
        out = subprocess.run(
            ["git", "tag", "--list", "v*"],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=str(ROOT),
        )
        if out.returncode == 0:
            tags = [t.strip() for t in out.stdout.splitlines() if t.strip()]
    except Exception:
        tags = []

    if not tags:
        try:
            out = subprocess.run(
                ["gh", "release", "list", "--limit", "100"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if out.returncode == 0:
                tags = [ln.split()[0] for ln in out.stdout.splitlines() if ln.strip()]
        except Exception:
            tags = []

    cur = parse(ver)
    earlier = [t for t in tags if parse(t) < cur]
    if earlier:
        return max(earlier, key=parse)

    # 没有更早的版本（首个发布）——返回空串，调用方会改成列全部提交的链接
    return ""


def _prev_from_changelog(version: str) -> str:
    """从 CHANGELOG 的版本标题列表里取前一个版本。

    CI 检出（actions/checkout 默认）不带 tag，本地也无 tag 时 _prev_tag 只能返回空串，
    于是发布说明会退化成「全部提交」。CHANGELOG 是随代码一起检出的，用它兜底。

    只考虑 ≥1.0.0 的版本：0.x 从未发布到 GitHub（无对应 tag），
    拿它做对比链接会指向不存在的 tag。
    """
    ver = version.lstrip("v")

    def parse(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in re.findall(r"\d+", v)[:3])

    text = CHANGELOG.read_text(encoding="utf-8") if CHANGELOG.exists() else ""
    cur = parse(ver)
    earlier = [
        m.group("ver")
        for m in HEADING.finditer(text)
        if m.group("ver") != ver
        and parse(m.group("ver")) < cur
        and parse(m.group("ver")) >= (1, 0, 0)
    ]
    return f"v{max(earlier, key=parse)}" if earlier else ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="从 CHANGELOG 提取发布说明")
    ap.add_argument("version", help="版本号，如 v1.3.0 或 1.3.0")
    ap.add_argument("--changelog", default=str(CHANGELOG))
    ap.add_argument("--out", default=None, help="输出文件；省略则打印到 stdout")
    args = ap.parse_args(argv)

    text = Path(args.changelog).read_text(encoding="utf-8")
    notes = build(args.version, extract(args.version, text))

    if args.out:
        # 显式指定 newline="": 否则 Windows 上 write_text 会把 \n 变成 \r\n，
        # 而 GitHub Release 正文与仓库其他 Markdown 都是 LF
        Path(args.out).write_text(notes, encoding="utf-8", newline="")
        print(f"已写入 {args.out}（{len(notes)} 字符）", file=sys.stderr)
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
