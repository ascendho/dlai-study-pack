> [English version](README.en.md) is also available.

# DeepLearning.AI 课程导出器

![Python](https://img.shields.io/badge/python-%3E%3D3.9-blue)
![Playwright](https://img.shields.io/badge/browser-Playwright-2EAD33)
![Tests](https://img.shields.io/badge/tests-pytest-blueviolet)
![Output](https://img.shields.io/badge/output-Markdown%20%2B%20JSON-lightgrey)
![Config](https://img.shields.io/badge/config-JSON-informational)
![License](https://img.shields.io/badge/license-MIT-green)

非官方个人学习辅助工具，用于把用户已授权访问的 DeepLearning.AI 课程页面整理为本地学习资料。本项目仓库不包含或再分发任何 DeepLearning.AI 或 Coursera 课程材料；用户生成的本地输出由用户自行负责。工具使用 Playwright 控制本地浏览器访问用户已登录且有权访问的页面。

## Legal and Usage Notice / 使用与法律提示

本项目是非官方个人学习辅助工具，不隶属于 DeepLearning.AI 或 Coursera，也未获得其官方认可、赞助或授权。使用者应只整理自己有权访问的内容，并自行遵守 DeepLearning.AI、Coursera、课程平台、实验环境提供方的条款以及适用法律。请勿使用本项目绕过付费墙、登录限制、访问控制或平台使用限制，也不要在没有授权的情况下公开发布、分享、出售或再分发导出的字幕、notebook、lab、quiz、assignment、solution 或其他课程资料。

详细说明请阅读 [NOTICE.md](NOTICE.md)。如果你是权利人，或认为本项目及相关公开内容损害了你的权利，可通过 GitHub Issues 或 [ascendho@outlook.com](mailto:ascendho@outlook.com) 联系维护者及时处理。

## 功能

- 从用户已登录且有权访问的课程主页发现 lesson 列表。
- 为本地个人学习保存 lesson 字幕 Markdown。
- 自动生成完整学习包：`index.md`、`course-overview.md`、`resources.md`、`manifest.json`。
- 在用户显式配置 Jupyter/Lab 链接时，保存可访问的 lesson 代码到本地。
- 可在用户已登录并有权访问的课程 code、project 或 graded 页面范围内使用页面提供的临时 lab 访问凭据。
- 复用 `.auth/deeplearning_ai.json` 中保存的本地 Playwright 登录态。

## 安装

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -e ".[dev]"
python3 -m playwright install chromium
```

## 配置

在项目根目录编辑 `dlai-transcripts.json`：

```json
{
  "course_url": "https://www.deeplearning.ai/courses/building-coding-agents-with-tool-execution",
  "code_url": "https://...lab-aws-production.deeplearning.ai/tree",
  "output_dir": "exports",
  "auth_state": ".auth/deeplearning_ai.json",
  "browser_visibility": "auto",
  "force": false
}
```

`course_url` 是必填项。`code_url` 可以留空；留空时只导出字幕和学习包元数据，不保存 lab 代码。配置中的 `code_url` 会作为 lesson lab 入口处理；课程页面中可见的 project 或 graded lab 也可能被发现。使用前请确认你有权访问并本地保存相关内容。

## 运行

```sh
dlai-transcripts
```

程序会为你已登录且有权访问的可见课程资源生成本地学习包，不需要额外参数。首次需要登录时，命令可能会打开一个浏览器窗口。你在浏览器里完成登录后，程序会自动继续运行。之后再次运行会复用保存的登录态，并默认在后台运行。

## 导出结构

默认导出到项目根目录下的 `exports/`：

```text
exports/<course-slug>/
  index.md
  transcripts/
    01-<lesson-slug>.md
  code/
    lessons/
    project/
  course-overview.md
  resources.md
  manifest.json
```

- `index.md`：lesson 索引、处理状态和本地代码保存摘要。
- `transcripts/`：逐课字幕 Markdown。
- `code/`：在配置 Jupyter/Lab 链接后保存到本地的可访问代码和资料。
- `code/lessons/`：来自配置的 `code_url` 或普通 code lesson 页面的 lesson 代码。
- `code/project/`：来自 project、graded 或 assignment 页面中可见 lab 入口的项目代码；未发现时不会生成内容。
- `course-overview.md`：课程摘要、学习目标、讲师、lesson 类型和时长。
- `resources.md`：代码示例、测验或作业页面，以及页面中可见的资源链接。
- `manifest.json`：结构化课程、lesson、资源和处理结果数据。

`metadata` 表示该课程项没有生成单独的字幕 Markdown 文件，只记录在 `index.md` 和 `manifest.json` 中。代码示例、测验或作业页面如果没有可见字幕，会标记为 `metadata`，而不是 `failed`。

## 登录态

工具不会保存用户名或密码，只保存复用网页登录所需的本地 Playwright 浏览器状态：

```text
.auth/deeplearning_ai.json
```

在用户已登录且有权访问的课程 code、project 或 graded 页面中，工具可能使用页面正常提供的临时 lab 访问凭据来请求相关 Jupyter/Lab 资源。只有你确认自己有权访问相关 lab，且课程页面没有提供可复用的 lab 入口时，才应在项目根目录的 `dlai-transcripts.json` 中添加 `"code_token": "你的 Jupyter token"`，或把 `"browser_visibility"` 改为 `"visible"` 后重新运行一次。不要提交或公开包含 token、登录态或导出课程资料的本地文件。

## 说明

- 本项目面向个人学习工作流。
- 源码位于 `src/study/`，这是标准 Python `src` layout 下的包目录，不是生成输出目录。
- DeepLearning.AI 页面结构可能变化；解析器测试使用本地 HTML fixture 来保持核心行为稳定。
