# LLM-Wiki（论文实现）

对论文 **《Retrieval as Reasoning: Self-Evolving Agent-Native Retrieval via LLM-Wiki》**（arXiv:2605.25480）的独立实现：把文档**编译**成带双向链接的结构化 Wiki，查询时由 Agent 组合 `wiki_search` / `wiki_read` 进行遍历推理，并通过 **Error Book** 实现持久的自我纠错。

## 安装

```bash
python -m venv .venv
.venv/Scripts/pip install pyyaml pytest   # Windows
# .venv/bin/pip install pyyaml pytest     # Linux/macOS
```

唯一运行时依赖是 `pyyaml`（LLM 调用走标准库 urllib）。

## 配置 LLM

OpenAI 兼容接口均可，环境变量三选一配置：

```bash
export LLM_WIKI_BASE_URL="https://api.moonshot.cn/v1"   # 默认值，可换任何兼容端点
export LLM_WIKI_API_KEY="sk-..."
export LLM_WIKI_MODEL="kimi-k2-0711-preview"            # 默认值，可换
```

本地模型（Ollama 等）：`LLM_WIKI_BASE_URL=http://localhost:11434/v1`。

## 使用

```bash
# 编译文档进 Wiki（算法 1 全流程：选页→编译→校验→Error Book→修复）
python -m llm_wiki --wiki ./wiki ingest my_notes.txt

# 提问（Agent 遍历：搜索→阅读→跟链接→充分性检查→作答）
python -m llm_wiki --wiki ./wiki query "哪部电影的导演更年长？"

# 结构校验（5 类确定性错误检测）
python -m llm_wiki --wiki ./wiki validate

# 代码自动修复；--finalize 追加 3 轮 代码↔LLM 修复（论文 §3.3 定稿阶段）
python -m llm_wiki --wiki ./wiki fix --finalize

# 删除一篇已入库的文档并恢复 Wiki 一致性（先 --dry-run 预览影响面）
python -m llm_wiki --wiki ./wiki delete my_notes.txt --dry-run
python -m llm_wiki --wiki ./wiki delete my_notes.txt

# 查看错误记录本
python -m llm_wiki --wiki ./wiki errorbook
```

（`--wiki` 是全局参数，需放在子命令之前。）

无 API key 时可跑脚本化端到端演示（编译本论文自身 + 多跳查询）：

```bash
python examples/demo_paper.py
```

## 论文 → 代码 映射

| 论文 | 实现 |
|---|---|
| 算法 1 索引期编译（附录 D） | `llm_wiki/compile.py`（`Compiler.compile_passage` 逐行对应） |
| 页面 Schema（附录 E） | `llm_wiki/schema.py` + `store.py`（frontmatter + 三个必备章节 + 双向 wikilink） |
| 7 类错误分类（附录 F） | `llm_wiki/validators.py`（5 类确定性 + 2 类 LLM 验证） |
| Error Book 五阶段（§3.3） | `llm_wiki/error_book.py`（Discover→Attribute→Constrain→Inject→Verify&Close） |
| 双层修复（§3.3） | `code_autofix` / `llm_periodic_fix` / `finalize`（3 轮循环） |
| wiki_search/wiki_read（§3.2） | `llm_wiki/search.py`（结构化信号优先）+ `store.read_many` |
| 遍历策略与终止（§3.2、附录 H） | `llm_wiki/agent.py`（Tmax=15，P=3，作答前至少一次 wiki_read） |

## 超参数（论文 §4.4）

`T_max=15`（工具调用预算）、`P=3`（连续空搜索耐心阈值）、`k=5`（SelectPages 上限）、每 10 篇文章触发一次 LLM 周期修复——均为默认值，可在 `agent.py` / `compile.py` 中调整。

## 测试

```bash
.venv/Scripts/python -m pytest tests/ -q   # 65 个用例，FakeLLM 驱动，无需 API key
```

## 与论文的差异（刻意取舍）

- **wiki_search 无向量嵌入**：以页名/别名/标签/摘要的结构化匹配为主、正文匹配回退（论文本就如此排序优先级）；未接入嵌入模型。
- **未复现实验**：不含基准评测代码（HotpotQA/MuSiQue/2Wiki/AuthTrace）。
- **Agent 工具协议为 JSON action** 而非原生 function calling：兼容任意 OpenAI 兼容端点（含不支持 tools 参数的本地模型）。
- **文档删除（仓库扩展）**：论文未定义删除语义；`llm_wiki/delete.py` 实现算法 1 的逆过程——反查引用 → 删孤儿页 → LLM 重验证存活页事实 → 级联清链 → 重建索引，以结构校验零错误收尾。
