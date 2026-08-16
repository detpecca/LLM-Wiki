# LLM-Wiki 代码库指南

本文档帮助你快速理解这个代码库：先是推荐阅读顺序，然后是逐模块详解。

项目是对论文《Retrieval as Reasoning: Self-Evolving Agent-Native Retrieval via LLM-Wiki》的实现。读代码前建议先建立论文的三个核心概念：

1. **编译（Compilation）**：文档不是被切块嵌入，而是被 LLM 改写成结构化、互链的 Wiki 页面（索引期，离线）；
2. **遍历（Traversal）**：查询时 Agent 用 `wiki_search`/`wiki_read` 两个工具搜索、阅读、跟链接，直到证据充分（查询期，在线）；
3. **错误记录本（Error Book）**：编译中产生的系统性错误被记录、归因、转化为约束注入后续编译，并由双层修复机制清理（贯穿索引期）。

---

## 一、推荐阅读顺序

代码总共约 1500 行，按依赖关系从底向上读，约 1 小时可通读：

### 第 1 站：`llm_wiki/schema.py`（~140 行）—— 词汇表

系统的"数据格式定义"，无任何依赖。先搞懂：

- 一个 Wiki 页面长什么样：`Page` dataclass（`schema.py:56`）和 `render_page()`（`schema.py:81`）——frontmatter + 一行摘要 + 三个必备章节（Key Facts / Related Pages / Related Sources）；
- wikilink 语法 `[[dir/Page]]` 如何被解析：`WIKILINK_RE`（`schema.py:19`）和 `extract_links()`（`schema.py:37`）。

读完你应该能手写一个合法页面。这是理解一切的前提。

### 第 2 站：`llm_wiki/store.py`（~180 行）—— 文件系统层

`WikiStore` 类（`store.py:24`）封装 Wiki 目录树的所有读写。重点四个：

- `iter_pages()`（`store.py:53`）：什么算"知识页"（排除索引和 sources/）；
- `add_backlink()` / `sync_bidirectional_links()`（`store.py:81`/`:106`）：**双向链接如何维护**——A 链 B 时自动给 B 补回链，这是论文"bidirectional links"的落地；
- `rebuild_directory_index()` / `rebuild_global_index()`（`store.py:118`/`:144`）：`_index.md` 和 `index.md` 如何从磁盘页面重新生成——它们不是手写的，是**派生产物**；
- `directory_index_listing()`（`store.py:171`）：算法 1 里 SelectPages 看到的 "I" 就是这个。

### 第 3 站：`llm_wiki/validators.py`（~160 行）—— 错误检测

论文附录 F 的 7 类错误在这里变成 5 个确定性检查函数 + 1 个 LLM 验证：

- `check_dangling_links`（`:45`）、`check_incomplete_pages`（`:56`）、`check_malformed_refs`（`:69`）、`check_index_consistency`（`:90`）都是纯代码，逻辑直白，扫一眼即可；
- `check_unseen_overwrite`（`:109`）注意签名不同：它比较的是**集合**（更新的页面 ⊆ 选中的页面 ∪ 新建页面），是编译期的即时检查；`check_update`（`:117`）与之配套，在更新落盘前检查 U 内部的悬空链接和坏引用；
- `structural_validate()`（`:140`）是上面这些的聚合入口，对应算法 1 第 4 行；
- `llm_content_validate()`（`:166`）是 LLM 内容验证，注意它的 prompt（`FACT_CHECK_PROMPT`，`:152`）：逐条核对 Key Facts 是否有 digest 支撑。

### 第 4 站：`llm_wiki/error_book.py`（~145 行）—— 五阶段状态机

`ErrorBook` 类（`:38`）管理 `error_book.yaml`。对照论文 §3.3 五阶段读：

| 阶段 | 方法 | 类型 |
|---|---|---|
| 1 Discover | `discover()`（`:61`）——注意相同模式的错误会**合并计数**而非重复建条目 | 代码 |
| 2+3 Attribute/Constrain | `attribute_and_constrain()`（`:96`）——LLM 填 root_cause 和 constraint_rule | LLM |
| 4 Inject | `active_constraints()`（`:120`）——取出所有 open 条目的约束规则 | 代码 |
| 5 Verify & Close | `verify_and_close()`（`:126`）——错误消失且有约束的条目才关闭 | 代码 |

重点理解**条目字段**：phenomenon / root_cause / constraint_rule / status(open|closed) / occurrences——与论文 3.3 节逐字对应。

### 第 5 站：`llm_wiki/compile.py`（~290 行）—— 核心中的核心

`Compiler` 类（`:119`）就是算法 1。**强烈建议打开论文附录 D 的伪代码，与 `compile_passage()`（`:215`）逐行对照读**——函数里的注释标了伪代码行号：

```
伪代码行2  SelectPages        -> select_pages()      :130  (LLM)
伪代码行3  CompileWikiPages   -> compile_pages()     :140  (LLM, 注入约束)
伪代码行4  StructuralValidate -> check_unseen_overwrite 等
伪代码行5  ContentValidate    -> llm_content_validate（在落盘后执行）
伪代码行8-10 Error Book 更新  -> discover + attribute_and_constrain + code_autofix
伪代码行12 ApplyUpdates       -> apply_updates()     :174  (写页/写digest/补回链/重建索引)
伪代码行14-17 周期修复        -> llm_periodic_fix() + verify_and_close()
```

另外两个重点：

- 三个 prompt 模板在文件顶部（`:31`、`:48`、`:87`），尤其是 `COMPILE_PAGES_PROMPT`——**约束注入点**就在 `{constraints_block}`；
- `code_autofix()`（`:153`）是第 1 层修复的确定性逻辑：丢弃悬空链接、丢弃格式错误的引用、丢弃越权更新；
- `finalize()`（`:289`）是论文 §3.3 末尾的"3 轮代码↔LLM 修复"定稿循环。

### 第 6 站：`llm_wiki/search.py`（~70 行）—— wiki_search

`search()`（`:21`）。注意权重表 `WEIGHTS`（`:14`）：页名 8 > 别名 6 > 标签 4 > 摘要 2 > 正文 1——这就是论文 §3.2"优先结构化信号，回退正文"的具体数值化。正文匹配只在结构化信号都没命中时兜底（`elif` 分支）。

### 第 7 站：`llm_wiki/agent.py`（~130 行）—— 查询期遍历

`run_agent()`（`:67`）是 ReAct 循环。对照论文图 2 读：

- 协议：LLM 每轮输出一个 JSON action（`wiki_search`/`wiki_read`/`answer`），`_parse_action()`（`:56`）解析；
- 终止条件在循环里：步数上限 `t_max=15`、连续空搜索 `patience=3`、以及"未 wiki_read 不许作答"的硬性拒绝（`:105`）；
- 系统提示词 `SYSTEM_PROMPT`（`:23`）里写了附录 H 的三种策略（直接检索/链接跟随/浏览聚合）——策略不是代码分支，是**教给 LLM 的**。

### 第 8 站：外围（可选读）

- `llm_wiki/llm.py`：OpenAI 兼容客户端，唯一接口是 `chat(messages) -> str`——全系统所有 LLM 调用都走这里，换模型/换端点只改环境变量；
- `llm_wiki/delete.py`：文档删除（仓库扩展，算法 1 的逆过程）——足迹全匹配防前缀碰撞、扫描 Related Sources 反查引用页、孤儿页整页下线、存活页 LLM 重验证裁剪；`code_fix_wiki` 也吸收了悬空链接剪除作为崩溃恢复手段；
- `llm_wiki/cli.py`：六个子命令的薄壳，把上面所有模块串起来；
- `tests/conftest.py`：`FakeLLM`——预置回复队列的 LLM 替身，全部测试靠它摆脱 API 依赖。读测试（尤其 `test_compile.py` 和 `test_agent.py`）是理解系统行为的最快方式；
- `examples/demo_paper.py`：端到端演示， ScriptedLLM 展示了每个 LLM 步骤"应该输出什么"。

### 一句话记忆法

> **schema 定义页面 → store 管文件 → validators 查错误 → error_book 记教训 → compile 把它们串成算法 1 → search/agent 是查询侧 → llm/cli 是外壳。**

---

## 二、架构与数据流

### 索引期（ingest）

```
源文档段落 x
   │
   ▼
SelectPages(x, I) ──LLM──► 相关已有页面 S（≤5）
   │
   ▼
CompileWikiPages(x, S, C) ──LLM──► 更新集 U（新页+改页+digest）
   │                                 ▲ C = Error Book 的 open 约束
   ▼
StructuralValidate(U, W) ──代码──► 结构错误 Es
ContentValidate(U, W, A) ──LLM──► 内容错误 Ec
   │
   ├─有错误─► ErrorBook.discover → attribute_and_constrain → CodeAutoFix(U)
   │
   ▼
ApplyUpdates(W, U')：写页面 → 补双向回链 → 重建索引
   │
   ▼（每 10 篇）
LLMPeriodicFix + VerifyAndClose
   │
   ▼（全部结束后）
finalize()：3 轮 code-fix ↔ LLM-fix
```

### 查询期（query）

```
问题 ─► ┌──────────────── ReAct 循环（≤15 轮）────────────────┐
        │  LLM 选择动作:                                        │
        │   wiki_search(q) ─► search.py 结构化打分 ─► 候选页    │
        │   wiki_read(paths) ─► store.read_many ─► 页面+[[链接]]│
        │   answer(ans, evidence) ─► 结束（要求读过 ≥1 页）     │
        │  连续 3 次空搜索 → 终止                               │
        └───────────────────────────────────────────────────────┘
```

### 删除期（delete，仓库扩展）

论文只定义了增量编译；删除是本仓库对论文的扩展，实现为算法 1 的逆过程：

```
被删文档 stem（文件路径 slugify 或 source-id 前缀）
   │
   ▼
足迹匹配：sources/digests/ + sources/articles/ 中 id == stem 或 stem-<数字> 的文件
（id 全匹配，删除 "notes" 不会误伤 "notes-2-001"）
   │
   ▼
反查引用页：扫描全部知识页的 Related Sources（引用即溯源索引，按需扫描不持久化）
   ├─ 引用 ⊆ 足迹 ──► 孤儿页整页删除 + ErrorBook.close_for_pages
   └─ 另有来源 ─────► 剪掉死引用（schema.rewrite_section 手术式改写）
                      → llm_periodic_fix 用剩余 digest 重验证、裁剪失支撑事实
   │
   ▼
级联清理：prune_dangling_links 全库剪掉指向已消失目标的 bullet → rebuild_all_indices
   │
   ▼
structural_validate 零错误收尾（否则 CLI 退出码非 0）
```

每一步幂等：中途崩溃后重跑 delete 或 `fix` 即可收敛。`--dry-run` 只打印足迹/受影响页/将删除的页，零写入。

### 文件布局（运行时产物）

```
wiki/
  index.md                  # 全局索引（rebuild_global_index 生成）
  concepts/_index.md        # 目录索引（rebuild_directory_index 生成）
  concepts/Error-Book.md    # 知识页（schema.Page 渲染）
  sources/digests/*.md      # 段落摘要（apply_updates 生成）
  sources/articles/*.md     # 原文存档
error_book.yaml             # ErrorBook 持久化
```

---

## 三、关键设计决策

1. **LLM 与代码的职责切分**：判断"对不对"需要语义的（事实有无出处、归因、修复内容）→ LLM；判断"合不合法"可以程序化的（链接存在性、格式、集合包含）→ 代码。这是论文双层修复设计的直接体现，也是成本控制。
2. **索引是派生产物**：`_index.md`/`index.md` 永远可以从磁盘页面重建，所以"索引不一致"错误的修复就是直接重建（`code_fix_wiki`，`compile.py:257`）。
3. **双向链接由系统保证，不靠 LLM 自觉**：LLM 只声明 A→B，回链 B→A 由 `add_backlink` 补。少了整整一类悬空/单边错误。
4. **约束注入是 prompt 工程，不是架构改动**（论文原话）：Error Book 的约束只是追加进 `COMPILE_PAGES_PROMPT` 的文本，防错机制因此可以无限积累而不改一行代码。
5. **Unseen Overwrite 的检查点在编译时**而非校验时：LLM 输出 U 后立刻做集合比对，越权更新在落盘前就被 `code_autofix` 丢弃。
6. **Agent 的策略不写死**：直接检索/链接跟随/浏览聚合都写在系统提示词里由 LLM 自选，代码只保证工具语义和终止条件——这与论文"agent adaptively selects"一致。

## 四、与论文的已知差异

- `wiki_search` 无向量嵌入：论文实现细节未公开，我们按"结构化信号优先"原则用纯文本打分（`search.py` 的权重是自行设定的）；中文查询走 CJK bigram 分词。
- LLM 周期修复触发器：论文说 "every N articles"，默认 N=10（与 §4.4 的 re-validation period 对齐）。
- Agent 工具协议采用 JSON action 而非原生 function calling：为了兼容任意 OpenAI 兼容端点（含不支持 tools 参数的本地模型）。

## 五、扩展点

| 想做什么 | 改哪里 |
|---|---|
| 换 LLM / 本地模型 | 只设环境变量；或传任何有 `chat()` 方法的对象 |
| 加向量检索 | `search.py` 增加一路打分，与现有分数融合 |
| 新错误类型 | `validators.py` 加 check 函数 + 注册进 `structural_validate` |
| 改页面格式 | `schema.py` 的 `REQUIRED_SECTIONS` + `render_page`，校验器会跟随 |
| 接入 Obsidian | 无需改代码——wiki/ 目录直接作为 Obsidian vault 打开 |

## 六、测试地图（65 例）

| 文件 | 覆盖 |
|---|---|
| `test_schema.py` | slugify、链接提取、渲染/解析往返、段落手术式过滤 |
| `test_store.py` | 读写、双向链接幂等、索引重建、增量索引重建 |
| `test_validators.py` | 5 类结构错误各一例 + 干净 Wiki 零误报 |
| `test_consistency.py` | 跨页矛盾检测、页对去重、抽样上限 |
| `test_error_book.py` | 错误合并、归因→注入→关闭全循环、持久化重载 |
| `test_compile.py` | 算法 1 全流程、Unseen Overwrite 入册、约束注入到下一轮 prompt、悬空链接/坏引用落盘前拦截 |
| `test_search.py` | 英文分词、CJK bigram 分词、中文查询命中别名/摘要、结构化信号优先 |
| `test_llm.py` | 瞬时错误重试成功、重试上限、4xx 不重试 |
| `test_robustness.py` | 坏 LLM 输出（数组/缺 path/无 JSON）不崩、批次隔离、更新归一化 |
| `test_agent.py` | 桥接比较的多跳遍历、未读不许答、耐心/预算两种终止 |
| `test_delete.py` | 足迹全匹配防前缀碰撞、孤儿页级联删除、存活页剪引用+LLM 重验证、修复守卫、dry-run 零写入、无 key 中止、幂等 |
