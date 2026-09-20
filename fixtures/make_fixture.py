#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate fixtures/state.sample.json per CONTRACT.md section 1 (frontend v0 dev fixture).

Deterministic hand-authored 60-topic payload. Timestamps are anchored to the
generation time so relative times in the UI ("12 分钟前") read sensibly.

Usage: python3 fixtures/make_fixture.py [--out fixtures/state.sample.json]
       python3 fixtures/make_fixture.py --phase next --out fixtures/state.sample.next.json
"""
import argparse
import json
import os
from datetime import datetime, timedelta, timezone

MODEL = "deepseek/deepseek-v4.1-flash"
TAG = "人工智能"
TAG_URL = "https://linux.do/tag/444-tag/444"
TOTAL_TOPICS = 312

# (title, author, category, tags, state, score, filter_category, reason, summary,
#  replies, views, likes, body, excerpt, detail_fetched)
T = []


def t(title, author, category, tags, state, score, fcat, reason, summary,
      replies, views, likes, body="", excerpt=None, detail=True):
    T.append(dict(title=title, author=author, category=category, tags=tags,
                  state=state, base=state, score=score, fcat=fcat, reason=reason,
                  summary=summary, replies=replies, views=views, likes=likes,
                  body=body, excerpt=excerpt, detail=detail))


# ---------------------------------------------------------------- picked (15)
t("OpenAI 发布 GPT-6：2M 上下文，输入价格再降 40%", "kevinzhou", "Develop",
  ["人工智能", "资讯"], "picked", 92, "模型发布",
  "新旗舰开放 API，上下文与价格都有实质变化",
  "GPT-6 全量开放 API，上下文 2M，输入价格下调 40%，官方给出迁移指南与旧模型弃用时间表。",
  86, 14203, 214,
  "官方博客确认 GPT-6 已通过 API 全量开放，上下文窗口 2M，输出 128k。输入价格从 $1.5/M 降到 $0.9/M，"
  "缓存命中再打一折。同时发布迁移指南，gpt-5.x 系列将在 12 月 1 日进入废弃状态。需要关注的是新的 "
  "reasoning 参数默认开启，旧 prompt 的 token 消耗会明显上升，实测同一段代码审查任务成本涨了约 18%。",
  None, True)

t("Claude Opus 5 代码实测：SWE-bench 首次超过人类基线", "cthulhu", "Develop",
  ["人工智能", "评测"], "picked", 90, "模型发布",
  "权威榜单反超人类基线，附完整复现步骤",
  "Opus 5 在 SWE-bench Verified 上达到 68.4%，超过人类标注基线，作者给出容器化复现脚本与失败案例分析。",
  142, 22871, 396,
  "用官方 docker 镜像跑了三轮，SWE-bench Verified 分别为 67.9 / 68.4 / 68.1%，人类基线是 65.3%。"
  "有意思的是失败案例集中在需要跨文件重构的任务上，单一函数修复几乎全过。复现脚本和数据都放在仓库里，"
  "另外附了一份 40 道题的错误分类，Python 3.13 新语法的题表现反而更差。",
  None, True)

t("DeepSeek-V4 权重开源，1.6T MoE 量化后单卡可跑", "moelover", "Resource",
  ["人工智能", "开源"], "picked", 94, "模型发布",
  "真开源权重加量化方案，单卡可跑 1.6T MoE",
  "V4 权重与推理代码放出，官方给了 4bit 量化脚本，1.6T MoE 在单张 96G 卡上可跑 8k 上下文。",
  203, 31044, 528,
  "权重、推理代码、量化脚本一起放出来了。MoE 结构是 256 专家、激活 37B，官方 4bit 量化后权重约 210G，"
  "单张 96G 卡开 offload 能跑 8k 上下文，吞吐大概 12 tok/s。有人已经在 8 卡机器上跑到 90 tok/s。"
  "注意官方的量化脚本默认关掉了 lm_head 的混合精度，长文本会有轻微的重复输出，需要手动改一行。",
  None, True)

t("Qwen4 开源全家桶：0.6B 到 480B 一次性放出", "shuizhu", "Resource",
  ["人工智能", "开源"], "picked", 88, "模型发布",
  "全尺寸开源，含端侧小模型与长上下文版本",
  "Qwen4 一次放出 0.6B/4B/32B/235B-A22B/480B 五个尺寸，含 1M 上下文与视觉版本，许可可商用。",
  168, 26510, 441,
  "五个尺寸一起放：0.6B、4B、32B、235B-A22B、480B，另外有 1M 上下文的变体和视觉版本。许可改了，"
  "商用不需要再申请。4B 那个在端侧是真能用，安卓上 15 tok/s 左右。480B 的推理成本还是太高，"
  "除非你有 8 卡机器，否则 235B-A22B 是性价比最好的选择。",
  None, True)

t("Gemini 4 Ultra 免费额度提到每天 500 次", "pengpeng", "General",
  ["人工智能", "资讯"], "picked", 76, "行业观察",
  "免费额度大幅提升，对个人开发者成本影响直接",
  "Gemini 4 Ultra 免费层提到 500 次/天、并发 10，付费层价格不变，配额由 1M 上下文全额支持。",
  97, 18630, 189,
  "官方配额页已经更新：免费层 500 次/天、并发 10，1M 上下文全额可用；付费层价格没动。"
  "实测免费层在香港节点会偶发 429，需要重试。对个人项目来说这个额度基本够用了，"
  "但如果做批量任务还是要注意 10 并发这个上限。",
  None, True)

t("本地跑 70B 的三种方案对比：双 4090 / M3 Ultra / 二手 A6000", "hardwareman", "Develop",
  ["人工智能", "本地部署", "硬件"], "picked", 84, "本地部署",
  "三种硬件路线的实测数据齐全，含功耗与噪声",
  "作者用同一套 GGUF 量化权重在三种硬件上测 tok/s、功耗、噪声与总成本，给出取舍建议。",
  121, 17402, 267,
  "同一份 Q4_K_M 权重，双 4090 拼 48G 显存跑 70B 大概 38 tok/s，整机功耗 780W；M3 Ultra 512G 统一内存"
  "约 21 tok/s，功耗 190W，几乎没声音；二手 A6000 48G 单卡 30 tok/s，功耗 300W。算总成本的话"
  "A6000 二手价加上电源改造其实不比双 4090 贵多少，但折腾程度最低。表格数据都在附件里。",
  None, True)

t("某 AI 浏览器插件被提示注入攻破，可读取全部会话内容", "secnotes", "Develop",
  ["人工智能", "安全", "漏洞"], "picked", 89, "安全事件",
  "真实可利用的提示注入，影响面大且有复现",
  "网页内嵌指令可让插件把历史会话摘要发到外部地址，作者给出最小复现页面，官方已确认并发布修复。",
  156, 24770, 402,
  "插件的 summarize 功能会把当前页面内容拼进 system prompt，页面里一段白色小字就能劫持它，"
  "让它把用户历史会话摘要 POST 到外部地址。最小复现页面不到 60 行，我本地 Chromium 稳定触发。"
  "官方在 1.8.4 里加了输出过滤和域名白名单，但更根本的问题是这类插件普遍没有指令与数据的边界。",
  None, True)

t("vLLM 0.12 重构 PagedAttention：吞吐与显存实测", "gpuqueen", "Develop",
  ["人工智能", "推理", "性能"], "picked", 82, "性能优化",
  "核心数据结构重构，附同机对照实测",
  "0.12 把 KV 块管理改成两级缓存，同机上 8k 并发吞吐提升 23%，显存碎片下降，附完整压测参数。",
  64, 9830, 143,
  "0.12 把 KV 块管理换成两级缓存，块回收不再依赖 GC 停顿。同一台 8 卡 H20 上，8k 并发下吞吐从 "
  "3240 tok/s 到 3980 tok/s，p99 从 820ms 降到 610ms。长输入下显存碎片明显减少，"
  "压测脚本和参数在评论里，想复现的同学注意要关掉 prefix caching 才是纯对比。",
  None, True)

t("把公司客服换成自训小模型，三个月省下 80% 推理成本", "liangliang", "General",
  ["人工智能", "实践", "成本"], "picked", 85, "行业观察",
  "真实落地案例，成本数据与坑都写清楚了",
  "用 8B 自训模型替换第三方 API，月成本从 4.2 万降到 8 千，附数据清洗流程与人工兜底策略。",
  188, 21055, 331,
  "客服场景其实很窄，把最近两年工单清洗一遍就有 12 万条高质量 SFT 数据。8B 模型 LoRA 微调，"
  "月成本从 4.2 万降到 8 千，回答准确率（人工抽检 500 条）从 71% 到 86%。"
  "最难的不是训练，是把兜底流程做对——凡是模型置信度低的必须转人工，不然投诉会变多。",
  None, True)

t("MCP 2.0 草案：工具调用加入权限粒度与审计", "mcpspec", "Develop",
  ["人工智能", "协议", "MCP"], "picked", 83, "工具推荐",
  "协议层终于加了权限模型，影响所有 agent 项目",
  "草案引入 scope 声明、调用审计与人类确认钩子，作者对比了现有三家实现的差异。",
  73, 11208, 176,
  "草案里最关键的是 scope 声明：工具要显式说明自己需要读文件还是发网络请求，客户端可以按 scope 拒绝。"
  "另外加了调用审计和 human-in-the-loop 钩子。现有的 LangGraph、AutoGen、自研框架三家的实现"
  "差异很大，迁移成本主要在自己写的 tool wrapper 上。评论区有人在讨论资源型 scope 的写法。",
  None, True)

t("llama.cpp 新的 KV 量化：长上下文显存下降 42%", "gguf", "Develop",
  ["人工智能", "推理", "量化"], "picked", 87, "性能优化",
  "显存优化幅度大，长上下文可用性明显提升",
  "新 KV 量化方案在 128k 上下文下显存下降 42%，困惑度上升不到 0.4%，附不同量化档位对比。",
  91, 13660, 208,
  "新的 KV 量化把 K 和 V 分开处理，V 用更粗的档位，困惑度几乎不动。128k 上下文的 32B 模型显存"
  "从 41G 降到 24G，同一张 3090 现在能开 64k 了。Q4 和 Q5 的差别在长文档问答里能感觉到，"
  "短对话基本无感。作者建议 3060 这类 12G 卡用 Q4 档加 32k 上下文。",
  None, True)

t("Anthropic 可解释性新论文：定位到具体的拒答回路", "paperreader", "Resource",
  ["人工智能", "论文", "可解释性"], "picked", 81, "论文解读",
  "可解释性研究有实质推进，方法可复用",
  "论文用稀疏自编码器定位到拒答相关的少量特征回路，并给出可干预的验证实验。",
  58, 8420, 132,
  "论文用稀疏自编码器在残差流里找到一组与拒答强相关的特征，数量比预期少得多。"
  "干预实验很有意思：抑制这几个特征后模型会回答本应拒绝的问题，而其他能力基本不变。"
  "作者也承认这套方法在更大模型上还不稳定，但方向比之前的探针方法扎实。",
  None, True)

t("某大厂 AI 编程助手被曝把完整仓库上传到第三方服务器", "watchdog", "General",
  ["人工智能", "安全", "隐私"], "picked", 91, "安全事件",
  "涉及代码外泄，有抓包证据，影响所有用户",
  "用户抓包发现插件在索引阶段把整个仓库打包上传，官方回应称是遥测，作者给出完整证据链。",
  214, 33280, 587,
  "抓包可以看到插件在索引阶段把整个仓库打包成 zip 上传到收集域名，包括 .env 和私钥文件。"
  "官方回应称是匿名的代码结构遥测，但请求体里明显有完整文件内容。作者给出了完整的 DNS + "
  "mitmproxy 证据链，以及一份几十行的本地复现脚本。建议先把插件禁用，检查一下 git 历史里有没有密钥。",
  None, True)

t("手把手：8 张 3090 训一个可用的 7B 领域模型", "trainhard", "Develop",
  ["人工智能", "训练", "教程"], "picked", 80, "教程实践",
  "完整可复现的中文训练教程，含踩坑记录",
  "从数据清洗到 LoRA 合并全流程，8 卡 3090 两天的实测配置与常见报错处理。",
  112, 15960, 252,
  "全流程都写了：数据清洗（去重、长度过滤、去模板腔）、tokenizer 扩充、LoRA 配置、断点续训。"
  "8 张 3090 用 deepspeed zero2，7B 模型 40 万条数据大约两天，成本不到 300 块电费。"
  "踩坑记录比正文值钱：flash attention 版本不匹配、数据里有超长样本导致 OOM、"
  "以及 eval loss 下降但实际效果变差的那种坑。",
  None, True)

t("开源 Agent 框架横评：LangGraph / AutoGen / 自研调度", "agentdev", "Develop",
  ["人工智能", "Agent", "评测"], "picked", 78, "工具推荐",
  "多框架横向对比，含状态管理与调试体验",
  "作者用同一套任务在三套框架上实现，比较状态管理、并发、可观测性与维护成本。",
  69, 10420, 158,
  "同一套客服工单任务在三套框架上各写一遍。LangGraph 的状态图最清晰但样板代码多；"
  "AutoGen 上手最快，但超过三个 agent 之后调试基本靠日志猜；自研调度最可控也最贵。"
  "可观测性这块三家都不太行，最后统一接了 OpenTelemetry 才好一点。",
  None, True)

# ---------------------------------------------------------------- pending (7)
t("有没有人用 AI 做过自动整理标签的脚本", "newbie2026", "General",
  ["人工智能", "提问"], "pending", None, None, None, None,
  0, 0, 0, "", "", False)

t("刚发的：有人横向对比过这几个量化档位的实际差距吗", "quantask", "Develop",
  ["人工智能", "量化", "提问"], "pending", None, None, None, None,
  0, 0, 0,
  "帖子刚发出来，想问问有没有人做过 Q4 和 Q5 在长文档问答上的对照实验，"
  "我这边只有单卡，跑两轮要一整天。",
  "想问问有没有人做过 Q4 和 Q5 在长文档问答上的对照实验。", True)

t("新出的那个 1.5B 端侧模型有人测过吗", "smallship", "Develop",
  ["人工智能", "端侧"], "pending", None, None, None, None,
  4, 61, 2,
  "看参数挺香的，1.5B 还能 128k 上下文，想知道实际中文能力和内存占用怎么样，"
  "有没有人在手机上跑过的。",
  "看参数挺香的，1.5B 还能 128k 上下文，想知道实际中文能力和内存占用怎么样。", True)

t("求一个能跑在树莓派 5 上的语音转文字方案", "pifan", "Develop",
  ["人工智能", "端侧", "语音"], "pending", None, None, None, None,
  11, 208, 5,
  "想做个离线语音笔记，树莓派 5 8G 版本，要求中文识别能用、延迟不要超过两秒。"
  "试过 whisper.cpp tiny，中文错得离谱，有没有更好的选择。",
  "想做个离线语音笔记，树莓派 5 8G 版本，要求中文识别能用、延迟不要超过两秒。", True)

t("今天人工智能板块怎么这么多重复帖", "olduser", "General",
  ["人工智能", "反馈"], "pending", None, None, None, None,
  23, 412, 8,
  "刷了两页，同一个模型发布的帖子有五个，标题还不一样。建议合并一下，不然找有效信息太累。",
  "刷了两页，同一个模型发布的帖子有五个，标题还不一样。", True)

t("求推荐支持中文的 embedding 模型", "ragbeginner", "Develop",
  ["人工智能", "RAG", "提问"], "pending", None, None, None, None,
  17, 336, 6,
  "在做知识库检索，现在用的 bge 老版本，长文档切块效果一般。想知道现在中文检索"
  "哪个 embedding 性价比最好，能本地部署优先。",
  "在做知识库检索，现在用的 bge 老版本，长文档切块效果一般。", True)

t("有人用过国产推理卡吗，兼容性到底怎么样", "cardgeek", "Develop",
  ["人工智能", "硬件"], "pending", None, None, None, None,
  31, 705, 12,
  "看参数比同级 A 卡便宜不少，但听说生态还是麻烦。想了解 transformers、vllm、"
  "以及常用量化工具链的实际支持情况，有踩过坑的说说。",
  "看参数比同级 A 卡便宜不少，但听说生态还是麻烦。", True)

t("关于模型蒸馏，有没有系统的入门材料", "studentli", "General",
  ["人工智能", "提问", "学习"], "pending", None, None, None, None,
  9, 152, 4,
  "只看过几篇论文，概念上懂，但不知道怎么落地。想找有代码、讲清楚温度和软标签的教程，"
  "最好是中文的。",
  "只看过几篇论文，概念上懂，但不知道怎么落地。", True)

# -------------------------------------------------------------- rejected (38)
def rj(title, author, category, tags, score, fcat, reason, summary,
       replies, views, likes, body="", excerpt=None, detail=True):
    t(title, author, category, tags, "rejected", score, fcat, reason, summary,
      replies, views, likes, body, excerpt, detail)


rj("每天打卡：今天你用了哪个模型", "daka", "General", ["纯水"], 41, "水文闲聊",
   "无信息量的日常打卡帖", "楼主发起每日打卡，回复基本是模型名列表，没有可复用的结论。",
   58, 903, 21, "习惯性打卡，今天用的是本地 32B，够用。", "习惯性打卡，今天用的是本地 32B。", True)

rj("感觉现在的模型都差不多了", "hmmm", "General", ["纯水", "讨论"], 45, "水文闲聊",
   "主观感受，无数据无对比", "楼主认为各家模型体验趋同，缺少具体任务与评测支撑。",
   74, 1288, 33, "用了一圈下来感觉都差不多，可能是我任务太简单。", "用了一圈下来感觉都差不多。", True)

rj("求推荐便宜的 API 中转站", "cheapapi", "General", ["提问", "API"], 48, "资源分享",
   "求推荐类提问，且涉及第三方中转合规风险",
   "楼主求低价 API 中转渠道，回复多为广告与个人推荐，无可靠信息。",
   39, 1560, 17, "预算有限，想找便宜点的 API，有靠谱的推荐吗。", "预算有限，想找便宜点的 API。", True)

rj("这网站的配色真难看", "uicolor", "Feedback", ["吐槽"], 40, "水文闲聊",
   "纯吐槽，无具体问题与改进建议", "楼主抱怨站点配色，未给出具体页面或方案。",
   26, 488, 14, "深色模式下那几个按钮的颜色真的很难看。", "深色模式下那几个按钮的颜色真的很难看。", True)

rj("有人试过用 AI 写小说吗", "novelist", "General", ["讨论", "创作"], 52, "水文闲聊",
   "泛泛讨论，缺少具体方法与产出", "楼主询问 AI 写小说的可行性，回复以个人体验为主。",
   88, 1742, 41, "想试试写长篇，不知道现在的模型能不能撑住几十万字的连贯性。",
   "想试试写长篇，不知道现在的模型能不能撑住连贯性。", True)

rj("睡前一水", "waterman", "General", ["纯水"], 40, "水文闲聊",
   "纯水帖，无内容", "无实质内容的日常水帖。", 45, 402, 19, "晚安。", "晚安。", True)

rj("大模型是不是要泡沫了", "bubble", "General", ["讨论"], 54, "行业观察",
   "宏大叙事讨论，无数据支撑", "楼主讨论行业泡沫，缺少可验证的数据与判断依据。",
   102, 2680, 56, "感觉最近概念太多，真正落地的没几个。", "感觉最近概念太多，真正落地的没几个。", True)

rj("我买了 4090，然后不知道该干嘛", "justbuy", "General", ["硬件", "纯水"], 46, "水文闲聊",
   "个人情况陈述，没有具体问题", "楼主分享购卡经历，未提出需要解决的问题。",
   51, 866, 22, "攒了半年钱买了一张 4090，装好之后发现也就是打游戏。",
   "攒了半年钱买了一张 4090，装好之后发现也就是打游戏。", True)

rj("提问：怎么下载模型", "verynew", "General", ["提问"], 42, "教程实践",
   "基础问题，官方文档可直接查到", "楼主询问模型下载方式，属于入门基础操作。",
   33, 590, 11, "刚接触这块，不知道从哪下载模型文件。", "刚接触这块，不知道从哪下载模型文件。", True)

rj("AI 会不会取代程序员（第 10086 次）", "againandagain", "General", ["讨论"], 44, "水文闲聊",
   "重复讨论，板块内已有大量同类帖", "老话题重复发起，回复无新增信息。",
   127, 3204, 62, "每隔两周就要来一轮，还是想听听大家的看法。", "还是想听听大家的看法。", True)

rj("求个能白嫖 GPT 的办法", "freehunter", "General", ["提问"], 43, "资源分享",
   "涉及绕过付费，且无可用信息", "楼主求免费使用途径，回复多为失效方法。",
   47, 1420, 15, "学生党，实在没钱订阅。", "学生党，实在没钱订阅。", True)

rj("刚刚发现一个很水的 AI 站", "findwater", "General", ["分享"], 47, "资源分享",
   "分享内容无实质价值", "楼主分享一个功能简单的聚合站点，无独特之处。",
   29, 522, 13, "界面很朴素，就是一堆模型的入口，没什么特别的。", "界面很朴素，没什么特别的。", True)

rj("我的显卡好烫", "hotcard", "General", ["硬件"], 40, "硬件采购",
   "描述模糊，缺少型号与温度数据", "楼主反映显卡温度高，未提供型号、温度和负载信息。",
   41, 634, 16, "跑模型的时候风扇声音特别大，摸着很烫。", "跑模型的时候风扇声音特别大。", True)

rj("有人一起拼车订阅吗", "carpool", "General", ["拼车"], 49, "资源分享",
   "拼车类帖子，无技术内容且存在账号风险", "楼主发起订阅拼车，回复为拉群信息。",
   56, 1108, 20, "三个人凑一下会便宜不少，有意向的私信。", "三个人凑一下会便宜不少。", True)

rj("公司不让用 AI 怎么办", "workai", "General", ["讨论"], 51, "行业观察",
   "职场闲聊，无技术信息", "楼主咨询公司限制下的应对方式，回复以个人经验为主。",
   68, 1236, 28, "内网没法访问外部 API，只能自己想办法。", "内网没法访问外部 API。", True)

rj("求 GPT 账号共享", "shareacc", "General", ["提问"], 40, "资源分享",
   "账号共享请求，存在安全与合规风险", "楼主寻求账号共享，无可用信息。",
   38, 890, 12, "想长期用，一个人买太贵了。", "想长期用，一个人买太贵了。", True)

rj("这个模型名字好长", "longname", "General", ["吐槽"], 40, "水文闲聊",
   "吐槽帖，无内容", "楼主吐槽模型命名，无实质讨论。", 22, 344, 9,
   "一堆名字加版本号加尺寸，根本记不住。", "一堆名字加版本号加尺寸，根本记不住。", True)

rj("大家平时用什么输入法", "imefan", "General", ["讨论"], 42, "水文闲聊",
   "话题与人工智能无关", "输入法闲聊帖，与板块主题无关。", 61, 978, 24,
   "想找个不带云同步的输入法。", "想找个不带云同步的输入法。", True)

rj("有没有人会写正则", "regexhelp", "Develop", ["提问"], 48, "教程实践",
   "通用编程提问，与 AI 主题无关", "楼主求正则写法，属于通用开发问题。",
   34, 606, 11, "想把一段日志里的时间戳批量替换掉，怎么写。", "想把一段日志里的时间戳批量替换掉。", True)

rj("今天面试被问到 transformer 了", "interview", "General", ["职场", "讨论"], 50, "行业观察",
   "个人经历分享，无技术细节", "楼主分享面试经历，未涉及具体知识点。",
   57, 1044, 26, "被问到注意力机制的复杂度，答得一般。", "被问到注意力机制的复杂度。", True)

rj("求推荐笔记本跑模型", "laptopq", "General", ["硬件", "提问"], 53, "硬件采购",
   "求推荐类提问，缺少预算与规模约束", "楼主求推荐笔记本，未给出预算、模型规模等条件。",
   44, 812, 18, "预算一万左右，想跑 14B 的模型。", "预算一万左右，想跑 14B 的模型。", True)

rj("听说某模型要收费了是真的吗", "hearsay", "General", ["讨论"], 46, "行业观察",
   "传闻性质，无官方来源", "楼主转述收费传闻，未提供官方公告链接。",
   52, 934, 21, "群里有人说下个月开始收费，不知道真假。", "群里有人说下个月开始收费。", True)

rj("我做的 AI 绘画被说侵权", "artright", "General", ["讨论", "版权"], 55, "行业观察",
   "个案纠纷讨论，缺少可执行的判断依据", "楼主讲述作品争议，涉及版权问题但无法律依据展开。",
   96, 2154, 45, "用了别人的风格参考，被原作者找上门了。", "用了别人的风格参考，被原作者找上门了。", True)

rj("分享一下我的学习路线（其实没什么内容）", "roadmap", "General", ["学习"], 45, "教程实践",
   "标题自承无内容，实际也未给出材料", "楼主分享学习路线，正文只有几句建议，无具体资源。",
   63, 1188, 27, "先看视频，再看论文，然后动手。就这样。", "先看视频，再看论文，然后动手。", True)

rj("求助：显卡花屏", "artifact", "General", ["硬件", "提问"], 41, "硬件采购",
   "硬件故障求助，与 AI 使用无直接关联", "楼主反馈显卡花屏，未提供驱动与日志信息。",
   36, 572, 13, "开机就花屏，重启偶尔好。", "开机就花屏，重启偶尔好。", True)

rj("有人用 AI 炒股吗", "stockai", "General", ["讨论"], 49, "行业观察",
   "投资话题，缺少可验证方法与数据", "楼主讨论 AI 选股，回复以个人经验为主。",
   82, 1896, 34, "想做一个自动看财报的工具，不知道靠不靠谱。", "想做一个自动看财报的工具。", True)

rj("本地部署是不是智商税", "taxq", "General", ["讨论"], 56, "本地部署",
   "观点帖，无成本与场景量化", "楼主质疑本地部署价值，未给出成本对比数据。",
   118, 2760, 54, "电费加显卡钱，不如直接用 API 吧。", "电费加显卡钱，不如直接用 API 吧。", True)

rj("模型回答带脏字怎么办", "dirtyword", "General", ["提问"], 47, "工具推荐",
   "单点问题，缺少上下文与复现", "楼主反馈模型输出不当内容，未说明模型与场景。",
   31, 528, 12, "偶尔会冒出几句不合适的，怎么过滤。", "偶尔会冒出几句不合适的，怎么过滤。", True)

rj("求个稳定的镜像站", "mirrorq", "Resource", ["资源", "提问"], 44, "资源分享",
   "资源求取类提问，无具体需求描述", "楼主求镜像站点，未说明需要的具体资源。",
   42, 1064, 16, "原来的那个挂了，有没有替他的。", "原来的那个挂了，有没有替他的。", True)

rj("现在的教程都太水了", "watertut", "General", ["吐槽"], 43, "水文闲聊",
   "吐槽帖，未指出具体教程与问题", "楼主抱怨教程质量，无具体案例。",
   66, 1210, 29, "标题很唬人，点进去就是官方文档截图。", "标题很唬人，点进去就是官方文档截图。", True)

rj("吐槽一下某平台的客服", "supportq", "Feedback", ["吐槽"], 40, "水文闲聊",
   "客服投诉，与板块主题无关", "楼主投诉平台客服，无技术内容。",
   48, 736, 22, "工单挂了三天没人回。", "工单挂了三天没人回。", True)

rj("有人知道这个 bug 吗", "bugq", "Develop", ["提问"], 50, "教程实践",
   "问题描述不清，无报错与环境信息", "楼主提问 bug，未提供报错日志与环境。",
   27, 458, 10, "运行到一半就退出了，不知道什么原因。", "运行到一半就退出了。", True)

rj("用 AI 写周报会不会被发现", "weekly", "General", ["讨论"], 52, "工具推荐",
   "职场取巧讨论，无方法价值", "楼主询问用 AI 写周报的风险，回复为经验之谈。",
   79, 1520, 31, "每周都要写，想省点时间。", "每周都要写，想省点时间。", True)

rj("求一个不花钱的 GPU 平台", "freegpu", "Resource", ["资源", "提问"], 45, "资源分享",
   "资源求取，且回复多为过期信息", "楼主求免费算力，回复多为限时活动。",
   54, 1342, 20, "只跑几个实验，不想花钱。", "只跑几个实验，不想花钱。", True)

rj("今天又被限额了", "ratelimit", "General", ["吐槽"], 42, "水文闲聊",
   "吐槽帖，无具体配额数据", "楼主抱怨配额限制，未给出具体接口与用量。",
   44, 692, 17, "写了半天代码，一句话没生成就被限额了。", "写了半天代码就被限额了。", True)

rj("大模型能算命吗", "fortunetell", "General", ["纯水"], 40, "水文闲聊",
   "娱乐向无厘头提问", "楼主询问模型算命，回复为玩梗。", 38, 604, 18,
   "让模型看八字，说得还挺像回事。", "让模型看八字，说得还挺像回事。", True)

rj("有人搞过语音克隆吗 求代码", "voiceclone", "Resource", ["语音", "提问"], 58, "工具推荐",
   "求代码帖，缺少用途与素材条件说明", "楼主寻求语音克隆代码，未说明用途与素材来源。",
   71, 1642, 25, "想给自己做个语音助手，有没有现成的。", "想给自己做个语音助手，有没有现成的。", True)


def build(phase):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    fetched_at = now - timedelta(minutes=12)
    last_run_at = now - timedelta(minutes=9)
    generated_at = now - timedelta(minutes=8)

    # dev-only "next" phase: promote 4 pending topics to picked so the
    # newly-picked FLIP animation can be exercised without a backend.
    if phase == "next":
        promote = {
            "新出的那个 1.5B 端侧模型有人测过吗": (
                79, "模型发布", "端侧新模型有实测价值", "作者给出手机端实测 token/s 与中文能力对比，结论明确。"),
            "求一个能跑在树莓派 5 上的语音转文字方案": (
                77, "教程实践", "方案完整且可复用", "高赞回复给出量化后的离线方案与配置文件，树莓派 5 上延迟 1.4 秒。"),
            "求推荐支持中文的 embedding 模型": (
                75, "工具推荐", "横向评测数据完整", "回复整理了五种中文 embedding 在检索任务上的对比结果与部署成本。"),
            "关于模型蒸馏，有没有系统的入门材料": (
                76, "论文解读", "系统整理了入门路径", "高赞回复按论文与代码给出学习顺序，附可运行的蒸馏示例。"),
        }
        for item in T:
            if item["title"] in promote:
                score, fcat, reason, summary = promote[item["title"]]
                item["state"] = "picked"
                item["score"] = score
                item["fcat"] = fcat
                item["reason"] = reason
                item["summary"] = summary

    topics = []
    base_id = 2929500

    # Display order (newest first) mirrors real data: the freshly scraped,
    # still-pending rows sit on top, then the judged ones with picked items
    # spread evenly among the rejected ones. Derived from the *base* state so
    # --phase next keeps identical rows (same ids, same order).
    pending_q = [x for x in T if x["base"] == "pending"]
    picked_q = [x for x in T if x["base"] == "picked"]
    rejected_q = [x for x in T if x["base"] == "rejected"]
    interleaved = []
    pi = ri = 0
    n_p, n_r = len(picked_q), len(rejected_q)
    while pi < n_p or ri < n_r:
        if pi < n_p and (ri >= n_r or (pi * (n_p + n_r)) // n_p <= len(interleaved)):
            interleaved.append(picked_q[pi])
            pi += 1
        else:
            interleaved.append(rejected_q[ri])
            ri += 1
    order = pending_q + interleaved

    for i, item in enumerate(order):
        created = now - timedelta(minutes=17 + i * 21)
        bumped = created + timedelta(minutes=(i * 7) % 55)
        tid = base_id - i * 7
        state = item["state"]
        body = item["body"]
        excerpt = item["excerpt"]
        if excerpt is None:
            excerpt = (body[:118] + "…") if body else ""
        filt = None
        if state != "pending":
            filt = {
                "score": item["score"],
                "category": item["fcat"],
                "reason": item["reason"],
                "summary": item["summary"],
                "at": last_run_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        topics.append({
            "id": tid,
            "title": item["title"],
            "url": "https://linux.do/t/topic/%d" % tid,
            "author": item["author"],
            "created_at": created.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "bumped_at": bumped.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "reply_count": item["replies"],
            "views": item["views"],
            "like_count": item["likes"],
            "category": item["category"],
            "tags": item["tags"],
            "excerpt": excerpt[:300],
            "body_text": body[:4000],
            "detail_fetched": item["detail"],
            "state": state,
            "filter": filt,
        })

    topics.sort(key=lambda x: (x["created_at"], x["id"]), reverse=True)
    picked = sum(1 for x in topics if x["state"] == "picked")
    rejected = sum(1 for x in topics if x["state"] == "rejected")
    pending = sum(1 for x in topics if x["state"] == "pending")

    return {
        "generated_at": generated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": {
            "tag": TAG,
            "tag_url": TAG_URL,
            "fetched_at": fetched_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "total_topics": TOTAL_TOPICS,
        },
        "filter": {
            "model": MODEL,
            "prompt_version": "v1",
            "last_run_at": last_run_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "picked": picked,
            "rejected": rejected,
            "pending": pending,
            "errors": 0,
        },
        "topics": topics,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", default="base", choices=["base", "next"])
    ap.add_argument("--out", default="fixtures/state.sample.json")
    args = ap.parse_args()
    data = build(args.phase)
    txt = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    for path in {args.out}:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(txt)
        print("wrote %s (%d bytes, %d topics)" % (path, len(txt.encode()), len(data["topics"])))
    print("picked=%d rejected=%d pending=%d" % (data["filter"]["picked"],
                                                data["filter"]["rejected"],
                                                data["filter"]["pending"]))


if __name__ == "__main__":
    main()
